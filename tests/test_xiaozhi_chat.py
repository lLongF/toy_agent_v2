"""Exercise the real loopback bridge with a fake upstream, without external calls."""
import asyncio
from contextlib import nullcontext
import json
import os
import unittest
from unittest.mock import patch
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from xiaozhi_chat import ChatBridge, normalise_origin


class ChatBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_text_audio_and_rejected_ticket(self):
        await self.exercise_bridge(False)

    async def test_reverse_proxy_origin_path_text_audio_and_official_upstream(self):
        await self.exercise_bridge(True)

    def test_public_origin_validation(self):
        self.assertEqual(normalise_origin('https://Demo.Example:443/'), 'https://demo.example')
        for value in ['*', 'null', 'wss://demo.example', 'https://user:password@demo.example',
                      'https://demo.example/path', 'https://demo.example?key=value', 'https://demo.example:bad']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalise_origin(value)
        with patch.dict(os.environ, {'XIAOZHI_CHAT_PORT': '65536'}), self.assertRaises(ValueError):
            ChatBridge()

    async def exercise_bridge(self, public):
        calls = []
        upstream_connections = []

        class Socket:
            def __init__(self):
                self.queue = asyncio.Queue()

            async def send(self, data):
                calls.append(data)
                if isinstance(data, bytes):
                    await self.queue.put(data)
                else:
                    item = json.loads(data)
                    await self.queue.put(json.dumps({'type': 'hello', 'session_id': 'test-session'}
                                                    if item['type'] == 'hello' else {'type': 'tts', 'text': item['text']}))

            async def recv(self):
                return await self.queue.get()

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.recv()

            async def close(self):
                pass

        class Transport:
            def __init__(self, cfg, state, router):
                self.state = state

            async def send_ota_request(self):
                calls.append('ota')
                return {'ok': True, 'data': {}}

            def extract_websocket_from_ota(self, data):
                return 'wss://example.invalid', 'secret-test-token'

            async def connect_websocket(self, url, token):
                upstream_connections.append((url, token))
                self.state.websocket = Socket()
                return True

            def hello_payload(self):
                return {'type': 'hello'}

        env = {'XIAOZHI_CHAT_PUBLIC_ORIGIN': 'https://demo.example' if public else '', 'XIAOZHI_CHAT_PORT': '0'}
        with patch.dict(os.environ, env), patch('xiaozhi_chat.XiaozhiTransportClient', Transport), patch('xiaozhi_chat.device_lock', return_value=nullcontext()):
            bridge = ChatBridge()
            self.assertEqual(calls, [])
            html = bridge.html()
            self.assertNotIn('secret-test-token', html)
            self.assertIn('wss://demo.example/xiaozhi-ws/' if public else 'ws://127.0.0.1:', html)
            if public:
                self.assertNotIn(f'ws://127.0.0.1:{bridge.port}', html)
            async with connect(f'ws://127.0.0.1:{bridge.port}/invalid', origin='http://127.0.0.1:8501') as ws:
                await ws.wait_closed()
                self.assertEqual(ws.close_code, 1008)
            self.assertEqual(calls, [])
            for bad_origin in ['https://demo.example.evil.test', 'https://evil.test', 'null']:
                async with connect(f'ws://127.0.0.1:{bridge.port}/{bridge.ticket}', origin=bad_origin) as ws:
                    await ws.wait_closed()
                    self.assertEqual(ws.close_code, 1008)
            self.assertEqual(calls, [])

            async def proxy(browser):
                # Same prefix removal and Origin preservation as the Nginx example.
                path = browser.request.path.removeprefix('/xiaozhi-ws')
                async with connect(f'ws://127.0.0.1:{bridge.port}{path}', origin=browser.request.headers['Origin']) as upstream:
                    async def forward(source, target):
                        async for packet in source:
                            await target.send(packet)
                    tasks = [asyncio.create_task(forward(browser, upstream)), asyncio.create_task(forward(upstream, browser))]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

            async with serve(proxy, '127.0.0.1', 0) as server:
                port = server.sockets[0].getsockname()[1] if public else bridge.port
                path = '/xiaozhi-ws/' if public else '/'
                origin = 'https://demo.example' if public else 'http://127.0.0.1:8501'
                await self.exchange(f'ws://127.0.0.1:{port}{path}{bridge.ticket}', origin, calls)
                self.assertEqual(upstream_connections, [('wss://example.invalid', 'secret-test-token')])

    async def exchange(self, url, origin, calls):
        async with connect(url, origin=origin) as ws:
            await ws.send('{"type":"hello"}')
            self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(), 3))['session_id'], 'test-session')
            await ws.send('{"type":"listen","state":"detect","text":"你好"}')
            self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(), 3))['text'], '你好')
            sent = json.loads(calls[-1])
            self.assertEqual(sent['session_id'], 'test-session')
            self.assertEqual(sent['mode'], 'manual')
            await ws.send(b'test-opus-packet')
            self.assertEqual(await asyncio.wait_for(ws.recv(), 3), b'test-opus-packet')


if __name__ == '__main__':
    unittest.main()
