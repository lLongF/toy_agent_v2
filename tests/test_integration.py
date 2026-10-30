"""离线验证协议、七类采集、双裁判、导出和界面；不调用真实服务。"""
from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import openpyxl

from cli import apply_fast_mode, parse_args
from config import load_dialogue_groups_from_xlsx
from judge import LLMJudge, merge_judge_results
from notify_wecom import xlsx_stats
from text_input_test import run_framework
from ui_jobs import JobManager, ROOT, report_rows


def config():
    with patch.object(sys, "argv", ["text_input_test.py"]):
        return parse_args()[0]


class FakeSocket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    async def send(self, raw):
        msg = json.loads(raw)
        if msg["type"] == "hello":
            for item in [
                {"type": "hello", "session_id": "test-session"},
                {"type": "mcp", "payload": {"id": 1, "method": "initialize"}},
                {"type": "mcp", "payload": {"id": 2, "method": "tools/list"}},
            ]:
                self.queue.put_nowait(json.dumps(item))
        elif msg["type"] == "listen":
            if msg["text"] == "工具":
                self.queue.put_nowait(json.dumps({"type": "mcp", "payload": {
                    "id": 3, "method": "tools/call", "params": {"name": "ping", "arguments": {}}}}))
            self.queue.put_nowait(json.dumps({"type": "tts", "state": "stop", "text": "测试回答"}))

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        self.closed = True


class EngineTests(unittest.TestCase):
    def test_same_device_cannot_start_twice(self):
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config()
            cfg.reports_dir = Path(tmp)
            cfg.token = "PRIVATE_TEST_TOKEN"
            process = Mock()
            process.poll.return_value = None
            # retain getvalue after the adapter closes its stdin
            class Input(io.StringIO):
                def close(self):
                    pass
            process.stdin = Input()
            with patch("ui_jobs.subprocess.Popen", return_value=process) as popen:
                manager = JobManager()
                directory = manager.start(cfg)
                with self.assertRaisesRegex(ValueError, "已有"):
                    manager.start(cfg)
            self.assertEqual(popen.call_count, 1)
            self.assertNotIn(cfg.token, str(popen.call_args.args))
            self.assertIn(cfg.token, process.stdin.getvalue())
            self.assertNotIn(cfg.token, (directory / "job.json").read_text())

    def test_installed_websockets_client_handshake(self):
        """真实使用锁定 websockets 客户端/服务端，仅连接本机。"""
        from websockets.asyncio.server import serve
        from collection import MessageRouter, TransportClient
        from config import RuntimeState

        async def exercise():
            async def handler(socket):
                hello = json.loads(await socket.recv())
                self.assertEqual(hello["type"], "hello")
                await socket.send(json.dumps({"type": "hello", "session_id": "wire-test"}))
                await socket.send(json.dumps({"type": "mcp", "payload": {"id": 1, "method": "tools/list"}}))
                reply = json.loads(await socket.recv())
                self.assertEqual(reply["payload"]["result"]["tools"][0]["name"], "ping")
                await socket.wait_closed()

            cfg = config()
            cfg.hello_drain_per_wait = 0.01
            cfg.hello_drain_idle_rounds = 1
            state = RuntimeState(mcp_tools=[{"name": "ping", "inputSchema": {"type": "object"}}])
            transport = TransportClient(cfg, state, MessageRouter(state))
            async with serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                # 此测试只检查直连兼容性，不依赖机器的代理环境。
                with patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                    self.assertTrue(await transport.connect_websocket(f"ws://127.0.0.1:{port}", "test"))
                    try:
                        self.assertTrue(await transport.send_hello_message())
                        self.assertEqual(state.session_id, "wire-test")
                        self.assertEqual(state.mcp_stats["tools_list_requests"], 1)
                    finally:
                        await state.websocket.close()

        asyncio.run(exercise())

    def test_real_cases_and_fast_cli_unchanged(self):
        cfg = config()
        groups = load_dialogue_groups_from_xlsx(cfg.cases_xlsx)
        self.assertEqual(sum(len(g.turns) for g in groups), 98)
        self.assertEqual(len(groups), 7)
        apply_fast_mode(cfg)
        with patch.object(sys, "argv", ["text_input_test.py", "--fast"]):
            self.assertEqual(cfg, parse_args()[0])

    def test_all_categories_through_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config()
            cfg.cases_xlsx = Path(tmp) / "cases.xlsx"
            cfg.reports_dir = Path(tmp) / "reports"
            cfg.mcp_tools_file = Path(tmp) / "missing.txt"
            cfg.send_interval = 0
            cfg.response_collect_timeout = 0.2
            cfg.hello_drain_per_wait = 0.001
            cfg.hello_drain_idle_rounds = 1
            cfg.collect_recv_timeout = 0.001
            cfg.collect_quiet_seconds = 0.002
            cfg.mcp_completion_quiet_seconds = 0.002
            cfg.mcp_post_stop_drain_seconds = 0.002
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Test case"
            ws.append(["类型", "用例编号", "消息列表", "预期结果", "是否工具调用", "工具名称", "工具参数文字描述"])
            categories = ["基础问答", "反向安全", "情感交互", "短期记忆", "长期记忆", "知识库", "MCP调用"]
            for i, category in enumerate(categories, 1):
                required = category == "MCP调用"
                ws.append([category, i, "工具" if required else "你好", "正确回答", "是" if required else "否", "ping" if required else "", ""])
            wb.save(cfg.cases_xlsx)
            wb.close()
            sockets = []

            async def connect(*args, **kwargs):
                socket = FakeSocket()
                sockets.append(socket)
                return socket

            def chat(*args, **kwargs):
                content = json.dumps({"score": 0.9, "pass": True, "reason": "符合预期",
                                      "is_off_topic": False, "is_emotion_mismatch": False,
                                      "is_context_consistent": True, "has_memory_gap": False,
                                      "is_redundant_questioning": False, "memory_recall_correct": True})
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

            response = SimpleNamespace(ok=True, status_code=200,
                                       json=lambda: {"websocket": {"url": "ws://test.invalid", "token": "test"}})
            with patch("collection.requests.post", return_value=response), patch("collection.websockets.connect", side_effect=connect), \
                 patch.object(LLMJudge, "_create_client", return_value=(object(), None)), patch.object(LLMJudge, "_chat", side_effect=chat):
                result = asyncio.run(run_framework(cfg))
            self.assertNotIn("error", result)
            self.assertEqual(len(sockets), 7)
            self.assertTrue(all(s.closed for s in sockets))
            data = json.loads(Path(result["report_paths"]["detail_report"]).read_text())
            self.assertTrue(all(g["group_pass"] for g in data["results"]))
            rows = report_rows(data)
            self.assertEqual(len(rows), 7)
            self.assertTrue(all(r["结果"] == "通过" for r in rows))
            self.assertEqual(xlsx_stats(Path(result["report_paths"]["xlsx_export"])), (7, 7, 0, 0))
            self.assertTrue(Path(result["report_paths"]["summary_report"]).is_file())

    def test_judge_failure_not_business_failure(self):
        cfg = config()
        merged = merge_judge_results([
            {"judge_name": "qwen", "judge_available": True, "score": 0.9},
            {"judge_name": "deepseek", "judge_available": False, "score": 0},
        ], cfg)
        row = report_rows({"results": [{"category": "基础问答", "turn_results": [
            {"sheet_row": 2, "judge_result": merged, "pass": False}]}]})[0]
        self.assertEqual(row["结果"], "评分失败")
        self.assertFalse(merged["pass"])

    def test_installed_openai_sdk_with_both_judges(self):
        import httpx
        from openai import OpenAI
        from judge import build_judge_endpoints

        cfg = config()
        for endpoint in build_judge_endpoints(cfg):
            def respond(request):
                body = json.loads(request.content)
                self.assertEqual(body["model"], endpoint.model)
                if endpoint.name == "deepseek":
                    self.assertEqual(body["thinking"], {"type": "disabled"})
                return httpx.Response(200, json={
                    "id": "test", "object": "chat.completion", "created": 0, "model": endpoint.model,
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
                        "content": json.dumps({"score": 0.9, "pass": True, "is_off_topic": False, "reason": "符合"})}}],
                })
            with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                client = OpenAI(api_key="test-only", base_url=endpoint.base_url, http_client=http)
                judge = LLMJudge(cfg, endpoint)
                judge._client = client
                judge._client_ready = True
                result = judge.basic_qa("你好", "正常打招呼", "你好呀")
                self.assertTrue(result["judge_available"], result)
                self.assertTrue(result["pass"], result)
                client.close()

    def test_worker_failure_is_recorded_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config()
            cfg.reports_dir = Path(tmp)
            cfg.cases_xlsx = Path(tmp) / "missing.xlsx"
            cfg.token = "PRIVATE_TEST_TOKEN"
            manager = JobManager()
            directory = manager.start(cfg)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                manager.refresh()
                status = json.loads((directory / "job.json").read_text())
                if status["status"] != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(status["status"], "failed")
            self.assertNotIn(cfg.token, (directory / "job.json").read_text())
            self.assertFalse((directory / "config.json").exists())
            # Reap the child before TemporaryDirectory cleanup on Windows.
            for process, _ in manager._processes.values():
                process.wait(timeout=5)

    def test_cli_entrypoints_offline(self):
        for args in [[], ["--list-toys"], ["--help"]]:
            result = subprocess.run([sys.executable, str(ROOT / "text_input_test.py"), *args],
                                    cwd=ROOT, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


class UITests(unittest.TestCase):
    def test_history_renders_mcp_and_judge_rows(self):
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reports" / "default"
            path.mkdir(parents=True)
            data = {"results": [{"category": "MCP调用", "turn_results": [
                {"sheet_row": 2, "pass": True, "combined_server_text": "已完成",
                 "utterances": [{"mcp_calls": [{"tool_name": "ping", "tool_args": {}}]}]},
                {"sheet_row": 3, "pass": False, "judge_result": {"judge_available": False,
                 "judges": [{"judge_name": "qwen", "score": 0.0, "reason": "评分失败"}]}}
            ]}]}
            (path / "default_text_result_test.json").write_text(json.dumps(data), encoding="utf-8")
            with patch("ui_jobs.ROOT", Path(tmp)):
                app = AppTest.from_file(str(ROOT / "streamlit_app.py")).run(timeout=60)
                app.sidebar.radio[0].set_value("结果分析").run(timeout=20)
            self.assertFalse(app.exception)
            self.assertEqual(app.metric[0].value, "2")
            self.assertEqual(app.metric[1].value, "1")
            self.assertEqual(app.metric[4].value, "1")

    def test_navigation_and_form_validation(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(ROOT / "streamlit_app.py")).run(timeout=60)
        self.assertFalse(app.exception)
        for page in ["用例与工具管理", "结果分析", "评测配置与执行"]:
            app.sidebar.radio[0].set_value(page).run(timeout=20)
            self.assertFalse(app.exception, page)
        # 不提供密钥时不能意外启动真实评测。
        with patch.dict("os.environ", {"TOY_AGENT_TOKEN": "", "TOY_AGENT_TOKEN_DEFAULT": "", "DASHSCOPE_API_KEY": "", "DEEPSEEK_API_KEY": ""}):
            app.run(timeout=20)
            self.assertTrue(next(b for b in app.button if "开始评测" in b.label).disabled)
        self.assertFalse(app.exception)
        self.assertTrue(app.error)


if __name__ == "__main__":
    unittest.main()
