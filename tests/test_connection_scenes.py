"""Fixed scene selection, verified identity, protocol and isolated local end-to-end tests."""
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

import openpyxl

from config import Config, RuntimeState, DialogueGroup, DialogueTurn
from collection import MessageRouter, TextInteractionPipeline
from connection_scenes import (configure_scene, prepare_scene_run, validate_scene_cases,
                               XIAOZHI_OTA_URL, XIAOZHI_DEVICE_ID, XIAOZHI_CLIENT_ID)
from xiaozhi_connection import XiaozhiTransportClient
from ui_jobs import JobManager
from ui_request import validate_request

PROJECT = Path(__file__).resolve().parents[1]


def config(root):
    root = Path(root)
    cases = root / 'cases.xlsx'
    wb = openpyxl.Workbook()
    wb.active.title = 'Test case'
    wb.active.append(['类型','用例编号','消息列表','预期结果','是否工具调用','工具名称'])
    wb.active.append(['基础问答','1','你好','问好','否',''])
    wb.active.append(['情感交互','2','我难过','安抚','否',''])
    wb.save(cases)
    wb.close()
    tools = root / 'tools.txt'
    tools.write_text('[]')
    return Config('https://company.invalid/ota', 'wss://company.invalid/ws', 'company-mac',
                  'company-client','test','company-secret',cases,tools,root/'reports', .1,0,.5,'mock',.7,
                  api_key='local-judge-key',enable_deepseek_judge=False,collect_quiet_seconds=.01)


class ConnectionSceneTests(unittest.TestCase):
    def test_verified_identity_stable_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = config(tmp)
            preview = configure_scene(company, 'xiaozhi')
            self.assertFalse(preview.scene_identity_generated)
            self.assertEqual(preview.ota_url,XIAOZHI_OTA_URL)
            self.assertEqual(preview.token,'')
            first,second=prepare_scene_run(preview),prepare_scene_run(preview)
            self.assertEqual(first.device_mac,second.device_mac)
            self.assertEqual(first.device_mac,XIAOZHI_DEVICE_ID)
            self.assertFalse(first.device_mac_random)
            self.assertEqual(first.client_id,second.client_id)
            self.assertEqual(first.client_id,XIAOZHI_CLIENT_ID)
            self.assertEqual(UUID(first.client_id).version,4)
            octet=int(first.device_mac.split(':')[0],16)
            self.assertEqual(octet & 3,2)
            self.assertIs(prepare_scene_run(first),first)
            self.assertEqual(company.token,'company-secret')
            validate_request(first)

    def test_unsupported_cases_rejected_and_company_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=config(tmp)
            turn=DialogueTurn(2,'1',['工具'],'调用',True,'battery','')
            group=DialogueGroup('g','MCP调用',[turn])
            validate_scene_cases(cfg,[group],{})
            with self.assertRaisesRegex(ValueError,'首期'):
                validate_scene_cases(configure_scene(cfg,'xiaozhi'),[group],{})

    def test_ota_consistent_identity_no_company_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=prepare_scene_run(configure_scene(config(tmp),'xiaozhi'))
            transport=XiaozhiTransportClient(cfg,RuntimeState(),None)
            response=SimpleNamespace(ok=True,status_code=200,json=lambda:{'websocket':{'url':'wss://mock.invalid/ws','token':'mock'}})
            with patch('xiaozhi_connection.requests.post',return_value=response) as post:
                result=asyncio.run(transport.send_ota_request())
            self.assertTrue(result['ok'])
            data=post.call_args.kwargs
            self.assertEqual(data['headers']['Device-Id'],data['json']['mac_address'])
            self.assertEqual(data['json']['board']['mac'],cfg.device_mac)
            self.assertEqual(data['json']['uuid'],cfg.client_id)
            self.assertEqual(data['headers']['Client-Id'],cfg.client_id)
            self.assertNotIn('token',data['json'])
            self.assertNotIn('company-secret',str(data))


class ConnectionWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_headers_hello_and_no_company_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=prepare_scene_run(configure_scene(config(tmp),'xiaozhi'))
            state=RuntimeState()
            transport=XiaozhiTransportClient(cfg,state,MessageRouter(state))
            socket=SimpleNamespace(send=AsyncMock(),recv=AsyncMock(return_value=json.dumps({'type':'hello','transport':'websocket','session_id':'test-session'})))
            with patch('collection.websockets.connect',new_callable=AsyncMock,return_value=socket) as connect:
                self.assertTrue(await transport.connect_websocket('wss://mock.invalid/ws?route=one','returned-ws-key'))
            url=connect.call_args.args[0]
            self.assertEqual(url,'wss://mock.invalid/ws?route=one')
            headers=connect.call_args.kwargs['additional_headers']
            self.assertEqual(headers['Authorization'],'Bearer returned-ws-key')
            self.assertEqual(headers['Device-Id'],cfg.device_mac)
            self.assertTrue(await transport.send_hello_message())
            hello=json.loads(socket.send.call_args.args[0])
            self.assertEqual(hello['transport'],'websocket')
            self.assertFalse(hello['features']['mcp'])
            self.assertNotIn('token',hello)
            await TextInteractionPipeline(state,cfg).send_text_message('任意文本')
            self.assertEqual(json.loads(socket.send.call_args.args[0])['session_id'],'test-session')

    async def test_abort_empty_and_close_after_complete(self):
        from websockets.exceptions import ConnectionClosedOK
        with tempfile.TemporaryDirectory() as tmp:
            cfg=configure_scene(config(tmp),'xiaozhi')
            for kind in ('abort','empty','complete-close'):
                with self.subTest(kind=kind):
                    state=RuntimeState(websocket=SimpleNamespace(send=AsyncMock()))
                    router=MessageRouter(state)
                    pipeline=TextInteractionPipeline(state,cfg)
                    await pipeline.send_text_message('测试')
                    count=0
                    async def recv(timeout):
                        nonlocal count
                        count+=1
                        if count==1:
                            if kind=='complete-close':
                                router.handle_text_message({'type':'tts','state':'sentence_start','text':'完整回答'})
                            message={'type':'abort'} if kind=='abort' else {'type':'tts','state':'stop'}
                            router.handle_text_message(message)
                            return message
                        if kind=='complete-close':
                            raise ConnectionClosedOK(None,None)
                        await asyncio.sleep(timeout)
                    transport=SimpleNamespace(recv_loop_once=recv)
                    if kind=='complete-close':
                        text,complete=await pipeline.collect_server_response_after_send(transport,.05)
                        self.assertTrue(complete)
                        self.assertEqual(text,'完整回答')
                    else:
                        with self.assertRaises((TimeoutError,ValueError)):
                            await pipeline.collect_server_response_after_send(transport,.05)

    async def test_incomplete_answer_does_not_get_normal_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=configure_scene(config(tmp),'xiaozhi')
            cfg.collect_recv_timeout=.001
            state=RuntimeState()
            async def receive(timeout):
                await asyncio.sleep(timeout)
            with self.assertRaises(TimeoutError):
                await TextInteractionPipeline(state,cfg).collect_server_response_after_send(SimpleNamespace(recv_loop_once=receive),.01)


class LocalSceneWorkerTests(unittest.TestCase):
    def test_real_worker_ota_ws_judge_and_three_reports(self):
        from websockets.asyncio.server import serve
        loop=asyncio.new_event_loop()
        ready=threading.Event()
        observed={'ota':[], 'headers':[], 'messages':[], 'errors':[]}
        async def handler(socket):
            try:
                observed['headers'].append(dict(socket.request.headers))
                hello=json.loads(await socket.recv())
                observed['messages'].append(hello)
                await socket.send(json.dumps({'type':'hello','transport':'websocket','session_id':'local-session'}))
                async for raw in socket:
                    message=json.loads(raw)
                    observed['messages'].append(message)
                    if message['type']=='listen':
                        await socket.send(json.dumps({'type':'llm','text':'🙂','emotion':'happy'}))
                        await socket.send(json.dumps({'type':'tts','state':'sentence_start','text':'你好，我在。'}))
                        await socket.send(json.dumps({'type':'tts','state':'sentence_end','text':'你好，我在。'}))
                        await socket.send(json.dumps({'type':'tts','state':'stop'}))
            except Exception as exc:
                observed['errors'].append(str(exc))
        address={}
        async def start():
            address['server']=await serve(handler,'127.0.0.1',0)
            address['port']=address['server'].sockets[0].getsockname()[1]
            ready.set()
        def host():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(start())
            loop.run_forever()
        thread=threading.Thread(target=host,daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5))
        class HttpHandler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path=='/ota':
                    observed['ota'].append((dict(self.headers),payload))
                    data={'websocket':{'url':f"ws://127.0.0.1:{address['port']}/ws",'token':'local-ws-secret','version':1},
                          'activation':{'code':'unused'},'mqtt':{'password':'unused-mqtt-secret'}}
                else:
                    data={'id':'local','object':'chat.completion','created':0,'model':'mock',
                          'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps({'score':.9,'reason':'符合','is_off_topic':False,'is_emotion_mismatch':False})}}]}
                raw=json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        http=ThreadingHTTPServer(('127.0.0.1',0),HttpHandler)
        http_thread=threading.Thread(target=http.serve_forever,daemon=True)
        http_thread.start()
        manager=JobManager()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg=configure_scene(config(tmp),'xiaozhi')
                cfg.ota_url=f'http://127.0.0.1:{http.server_port}/ota'
                cfg.judge_base_url=f'http://127.0.0.1:{http.server_port}/v1'
                with patch.dict('os.environ',{'NO_PROXY':'127.0.0.1','no_proxy':'127.0.0.1'}):
                    directory=manager.start(cfg)
                process=next(iter(manager._processes.values()))[0]
                process.wait(timeout=30)
                status=json.loads((directory/'job.json').read_text())
                self.assertEqual(status['status'],'completed',status)
                self.assertEqual(len(observed['ota']),1)
                headers,body=observed['ota'][0]
                self.assertEqual(len(observed['headers']),2)
                for ws_headers in observed['headers']:
                    self.assertEqual(ws_headers['device-id'],headers['Device-Id'])
                    self.assertEqual(ws_headers['client-id'],headers['Client-Id'])
                    self.assertEqual(ws_headers['authorization'],'Bearer local-ws-secret')
                self.assertFalse(observed['errors'],observed)
                listens=[m for m in observed['messages'] if m['type']=='listen']
                self.assertEqual(len(listens),2)
                for message in listens:
                    self.assertEqual(message['session_id'],'local-session')
                data=json.loads(Path(status['report_paths']['detail_report']).read_text())
                self.assertEqual(data['protocol_info']['connection_scene'],'xiaozhi')
                self.assertEqual(data['protocol_info']['device_mac'],headers['Device-Id'])
                self.assertTrue(all(g['group_pass'] for g in data['results']))
                self.assertTrue(all(t['combined_server_text']=='你好，我在。' for g in data['results'] for t in g['turn_results']))
                for report in status['report_paths'].values():
                    self.assertTrue(Path(report).is_file())
                book=openpyxl.load_workbook(status['report_paths']['xlsx_export'])
                try:
                    rows=list(book.active.values)
                    col=list(rows[0]).index('接入场景')
                    self.assertEqual([r[col] for r in rows[1:]],['xiaozhi','xiaozhi'])
                finally: book.close()
                for path in directory.rglob('*'):
                    if path.suffix in {'.json','.log','.md'}:
                        content=path.read_text()
                        self.assertNotIn('local-ws-secret',content)
                        self.assertNotIn('unused-mqtt-secret',content)
        finally:
            for process,_ in manager._processes.values():
                if process.poll() is None:
                    process.terminate();process.wait(timeout=5)
            http.shutdown();http.server_close();http_thread.join()
            async def stop():
                address['server'].close()
                await address['server'].wait_closed()
            asyncio.run_coroutine_threadsafe(stop(),loop).result(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
            thread.join();loop.close()


class SceneUITests(unittest.TestCase):
    def test_switch_and_submit_uses_scene_without_company_credentials(self):
        from streamlit.testing.v1 import AppTest
        def widget(widgets,label):
            return next(w for w in widgets if w.label==label)
        captured=[]
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'job'
            directory.mkdir()
            (directory/'job.json').write_text(json.dumps({'job_id':'ui-fixture','status':'completed'}))
            def start(manager,cfg):
                captured.append(cfg)
                return directory
            # Only in-memory/mock keys; no real task or service calls.
            with patch.dict('os.environ',{'DASHSCOPE_API_KEY':'','DEEPSEEK_API_KEY':''}), \
                 patch('local_credentials.read_token', side_effect=ValueError('fixture missing token')), \
                 patch.object(JobManager,'busy',return_value=False), \
                 patch.object(JobManager,'refresh',return_value=None), \
                 patch.object(JobManager,'start',start), \
                 patch('ui_results.confined',side_effect=lambda path,root:path):
                # streamlit_app imports confined on load, so the fixture task can stay temporary.
                app=AppTest.from_file(str(PROJECT/'streamlit_app.py'),default_timeout=60).run()
                app.sidebar.radio[0].set_value('评测配置与执行').run()
                self.assertFalse(app.exception)
                widget(app.text_input,'玩具 Token').set_value('company-session-fixture')
                widget(app.text_input,'Qwen API Key').set_value('qwen-fixture')
                widget(app.text_input,'DeepSeek API Key').set_value('ds-fixture')
                app.run()
                self.assertFalse(widget(app.button,'▷ 开始评测').disabled,[e.value for e in app.error])
                widget(app.radio,'接入场景').set_value('xiaozhi').run()
                self.assertFalse(app.exception)
                self.assertFalse(any(w.label=='玩具 Token' for w in app.text_input))
                self.assertTrue(widget(app.button,'▷ 开始评测').disabled)
                widget(app.radio,'评测范围').set_value('按类别').run()
                widget(app.multiselect,'选择类别').set_value(['基础问答']).run()
                self.assertFalse(widget(app.button,'▷ 开始评测').disabled,[e.value for e in app.error])
                widget(app.button,'▷ 开始评测').click().run()
                self.assertFalse(app.exception,[e.message for e in app.exception])
                self.assertEqual(len(captured),1)
                cfg=captured[0]
                self.assertEqual(cfg.connection_scene,'xiaozhi')
                self.assertEqual(cfg.ota_url,XIAOZHI_OTA_URL)
                self.assertEqual(cfg.token,'')
                self.assertEqual(cfg.ws_url,'')
                self.assertEqual(cfg.selected_categories,['基础问答'])
                self.assertEqual(cfg.api_key,'qwen-fixture')
                widget(app.radio,'接入场景').set_value('company').run()
                self.assertFalse(app.exception)
                self.assertEqual(widget(app.text_input,'玩具 Token').value,'')
                self.assertTrue(widget(app.button,'▷ 开始评测').disabled)
                self.assertEqual(widget(app.text_input,'Qwen API Key').value,'qwen-fixture')
