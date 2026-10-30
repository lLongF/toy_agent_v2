"""发版自测的直连、隔离、校验与页面回归，不调用外部服务。"""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock

from streamlit.testing.v1 import AppTest
from connection_scenes import validate_connection_mode
from test_integration import config as integration_config, FakeSocket
from text_input_test import run_framework
from ui_release import release_config, release_jobs, result_status

ROOT = Path(__file__).resolve().parents[1]


def config():
    with patch("toy_profiles.ToyProfile.resolve_token", return_value="test-only-token"):
        return replace(integration_config(), device_mac="02:00:00:fe:ed:aa")


class ReleaseTests(unittest.TestCase):
    def test_new_toys_default_to_all_capabilities_and_allow_explicit_limits(self):
        from toy_management import create_toy
        from toy_profiles import ALL_CAPABILITIES, apply_toy_environment
        from evaluation_selection import validate_capabilities
        from config import DialogueGroup, DialogueTurn
        with tempfile.TemporaryDirectory() as tmp:
            toy = create_toy("新玩具", root=Path(tmp), toy_id="new-toy")
            self.assertEqual(toy.capabilities, ALL_CAPABILITIES)
            cfg = apply_toy_environment(replace(config(), token="test-only"), toy)
            groups = [DialogueGroup("memory", "长期记忆", []), DialogueGroup("kb", "知识库", []),
                      DialogueGroup("mcp", "MCP调用", [DialogueTurn(2, "1", ["工具"], "调用", True, "ping", "")])]
            validate_capabilities(cfg, groups)
            limited = create_toy("受限玩具", root=Path(tmp), toy_id="limited", capabilities={"text": "supported"})
            with self.assertRaises(ValueError):
                validate_capabilities(apply_toy_environment(replace(config(), token="test-only"), limited), groups)

    def test_manual_judge_keys_override_and_blank_uses_environment(self):
        from toy_profiles import list_toy_profiles
        from judge_store import default_judge_models
        toy = next(t for t in list_toy_profiles() if t.enabled)
        base = replace(config(), api_key="", deepseek_api_key="")
        with patch("judge_store.snapshot_judge_models", return_value=default_judge_models(base)), \
             patch.dict("os.environ", {"DASHSCOPE_API_KEY": "env-qwen", "DEEPSEEK_API_KEY": "env-deepseek"}):
            cfg = release_config(base, toy, url="wss://release.invalid", token="toy-test",
                                 datasets=["test"], judges=["qwen", "deepseek"], mac=base.device_mac, client=base.client_id,
                                 judge_keys={"qwen": " manual-qwen ", "deepseek": " ", "unselected": "unused"})
        self.assertEqual(cfg.judge_credentials, {"qwen": "manual-qwen", "deepseek": "env-deepseek"})
        self.assertNotIn("manual-qwen", json.dumps(cfg.judge_endpoints_snapshot))
        self.assertEqual(base.judge_credentials, {})

    def test_manual_key_field_passes_to_job_and_clears_after_start(self):
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30).run()
        app.radio(key="navigation").set_value("发版自测").run()
        self.assertFalse(app.exception)
        self.assertFalse(any(x.label == "运行环境" for x in app.radio))
        app.text_input(key="release_url").set_value("wss://release.invalid").run()
        next(x for x in app.text_input if x.key.startswith("release_token_")).set_value("test-token").run()
        identity = app.multiselect(key="release_judges").value[0]
        field = "release_api_key_" + identity
        from streamlit.proto.TextInput_pb2 import TextInput
        self.assertEqual(app.text_input(key=field).proto.type, TextInput.PASSWORD)
        app.text_input(key=field).set_value("temporary-test-key").run()
        with patch("ui_release.validate_request"), patch("ui_jobs.JobManager.start", return_value=ROOT / "reports" / "test-only") as start:
            app.button(key="release_start").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(start.call_args.args[0].judge_credentials[identity], "temporary-test-key")
            self.assertEqual(app.text_input(key=field).value, "")
        app.radio(key="navigation").set_value("评测配置与执行").run()
        self.assertFalse(app.exception)
        self.assertFalse(any("裁判模型管理" in item.label for item in app.expander))
        self.assertTrue(any(item.label == "本次评分裁判模型" for item in app.multiselect))

    def test_direct_uses_supplied_url_and_token_without_ota(self):
        from judge import LLMJudge
        with tempfile.TemporaryDirectory() as tmp:
            cfg = replace(config(), connection_mode="direct", ws_url="ws://release.invalid/test",
                          token="private-release-token", trigger_type="release_selftest", release_environment="正式环境",
                          reports_dir=Path(tmp), selected_case_ids=["1"], response_collect_timeout=.1,
                          collect_quiet_seconds=.001, hello_drain_per_wait=.001, hello_drain_idle_rounds=1,
                          send_interval=0)
            answer = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"score":0.9,"reason":"符合","is_off_topic":false}'))])
            with patch("collection.requests.post", side_effect=AssertionError("must not call OTA")) as ota, \
                 patch("collection.websockets.connect", new_callable=AsyncMock, return_value=FakeSocket()) as connect, \
                 patch.object(LLMJudge, "_create_client", return_value=(object(), None)), \
                 patch.object(LLMJudge, "_chat", return_value=answer):
                result = asyncio.run(run_framework(cfg))
            ota.assert_not_called()
            url = connect.call_args.args[0]
            self.assertTrue(url.startswith("ws://release.invalid/test?"))
            self.assertIn("private-release-token", url)
            detail = Path(result["report_paths"]["detail_report"])
            data = json.loads(detail.read_text())
            pi = data["protocol_info"]
            self.assertTrue(pi["ota_skipped"])
            self.assertFalse(pi["ota_called"])
            self.assertEqual(pi["ws_url"], cfg.ws_url)
            self.assertEqual(pi["release_environment"], "正式环境")
            self.assertEqual(pi["execution_status"], "completed")
            self.assertNotIn(cfg.token, detail.read_text())
            self.assertTrue(pi["evaluation_rules"])

    def test_direct_url_validation_and_legacy_default(self):
        self.assertEqual(config().connection_mode, "ota")
        for url in ["https://example.com", "ws://", "ws://u:p@example.com", "wss://example.com?token=secret",
                    "ws://example.com#fragment", "ws://example.com:bad", "ws://exa mple.com"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_connection_mode(replace(config(), connection_mode="direct", ws_url=url, token="test"))
        validate_connection_mode(replace(config(), connection_mode="direct", ws_url="ws://127.0.0.1:8502/test", token="test"))

    def test_release_config_does_not_mutate_original(self):
        from toy_profiles import list_toy_profiles
        toy = next(t for t in list_toy_profiles() if t.enabled)
        base = config()
        with patch("ui_release.configure_judges", side_effect=lambda cfg: cfg):
            cfg = release_config(base, toy, url="wss://release.invalid", token="temporary",
                                 datasets=["chosen"], judges=["qwen"], mac="02:00:00:00:00:01", client="release-client")
        self.assertEqual(cfg.trigger_type, "release_selftest")
        self.assertEqual(cfg.selected_dataset_ids, ["chosen"])
        self.assertEqual(cfg.selected_judge_ids, ["qwen"])
        self.assertEqual(base.connection_mode, "ota")
        self.assertNotEqual(base.token, "temporary")
        for field in ["datasets", "judges", "token"]:
            args = dict(url="wss://release.invalid", token="temporary",
                        datasets=["chosen"], judges=["qwen"], mac=base.device_mac, client=base.client_id)
            args[field] = "" if field == "token" else []
            with self.subTest(field=field), self.assertRaises(ValueError):
                release_config(base, toy, **args)

    def test_history_only_lists_release_jobs_and_preserves_execution_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, trigger in [("release", "release_selftest"), ("legacy", "manual_ui")]:
                path = Path(tmp) / "toy" / "ui_runs" / name
                path.mkdir(parents=True)
                (path / "job.json").write_text(json.dumps({"trigger_type": trigger, "started_at": name}))
            self.assertEqual(len(release_jobs(Path(tmp))), 1)
        self.assertEqual(result_status({"status": "未执行", "execution_error": "timeout"}), "执行异常")
        self.assertEqual(result_status({"status": "评分失败"}), "评分失败")

    def test_legacy_job_source_restored_from_report_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "toy" / "ui_runs" / "old"
            directory.mkdir(parents=True)
            report = directory / "result.json"
            report.write_text(json.dumps({"protocol_info": {"trigger_type": "release_selftest", "target_name": "仪宝"}}))
            status = directory / "job.json"
            status.write_text(json.dumps({"status": "failed", "report_paths": {"detail_report": str(report)}}))
            original = status.read_bytes()
            jobs = release_jobs(Path(tmp))
            self.assertEqual(jobs[0][1]["target_name"], "仪宝")
            self.assertEqual(original, status.read_bytes())

    def test_progress_only_records_release_phase_and_counts(self):
        from run_progress import update_progress
        with tempfile.TemporaryDirectory() as tmp:
            cfg = replace(config(), reports_dir=Path(tmp))
            update_progress(cfg, "裁判评分", 1, 2)
            self.assertFalse((Path(tmp) / "progress.json").exists())
            cfg.trigger_type = "release_selftest"
            update_progress(cfg, "裁判评分", 1, 2)
            data = json.loads((Path(tmp) / "progress.json").read_text())
            self.assertEqual((data["stage"], data["completed"], data["total"]), ("裁判评分", 1, 2))
            self.assertNotIn(cfg.token, json.dumps(data))

    def test_page_selection_validation_and_existing_navigation(self):
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30).run()
        app.radio(key="navigation").set_value("发版自测").run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key="release_start").disabled)
        self.assertFalse(any(e.label == "查看本次评分标准" for e in app.expander))
        app.text_input(key="release_url").set_value("https://invalid.example").run()
        token = next(x for x in app.text_input if x.key.startswith("release_token_"))
        token.set_value("test-only").run()
        if not app.multiselect(key="release_judges").value:
            app.multiselect(key="release_judges").set_value([app.multiselect(key="release_judges").options[0]]).run()
        with patch("ui_jobs.JobManager.start") as start:
            app.button(key="release_start").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("WebSocket 地址" in e.value for e in app.error))
            start.assert_not_called()
        app.radio(key="navigation").set_value("结果分析").run()
        self.assertFalse(app.exception)
        app.radio(key="navigation").set_value("玩具实时对话").run()
        self.assertFalse(app.exception)

    def test_history_results_filter_download_and_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "toy" / "ui_runs" / "selftest"
            directory.mkdir(parents=True)
            detail = directory / "toy_text_result_test.json"
            specs = [{"sheet_row": i, "case_id": str(i), "user_messages": ["你好"], "expected_outcome": "问好"}
                     for i in (2, 3, 4)]
            detail.write_text(json.dumps({"protocol_info": {"toy_id": "toy", "judge_models": [{"name": "Judge", "model": "test-model"}]},
                "results": [{"category": "基础问答", "case_spec": {"turns": specs}, "turn_results": [
                    {"sheet_row": 2, "pass": False, "combined_server_text": "答非所问", "judge_result": {"judge_available": True, "score": .2, "reason": "不符合"}},
                    {"sheet_row": 3, "execution_error": "timeout", "pass": False}]}]}))
            xlsx = directory / "result.xlsx"
            xlsx.write_bytes(b"download-test")
            status_path = directory / "job.json"
            status = {"trigger_type": "release_selftest", "status": "completed", "started_at": "2026-10-29T12:00:00",
                      "target_name": "测试玩具", "release_environment": "测试环境", "report_paths": {"detail_report": str(detail), "xlsx_export": str(xlsx)}}
            status_path.write_text(json.dumps(status))
            script = f'''from pathlib import Path
from types import SimpleNamespace
import streamlit as st
from ui_release import release_history
def show(row): st.text(row['expected'])
def open_report(path): st.session_state['opened_report'] = str(path)
release_history(SimpleNamespace(refresh=lambda: None), Path({tmp!r}), show, open_report)
'''
            app = AppTest.from_string(script, default_timeout=20).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.get("progress")[0].proto.value, 100)
            self.assertEqual(len(app.dataframe[0].value), 3)
            self.assertTrue(any(x.label == "下载 Excel 报告" for x in app.get("download_button")))
            app.radio(key="release_filter").set_value("异常 / 未执行").run()
            self.assertFalse(app.exception)
            self.assertEqual(set(app.dataframe[0].value["状态"]), {"执行异常", "未执行"})
            app.radio(key="release_filter").set_value("未通过").run()
            self.assertEqual(len(app.dataframe[0].value), 1)
            app.button(key="release_full_report").click().run()
            self.assertEqual(Path(app.session_state["opened_report"]), detail.resolve())
            status.update(status="running", report_paths={})
            status_path.write_text(json.dumps(status))
            (directory / "progress.json").write_text(json.dumps({"stage": "裁判评分", "completed": 1, "total": 2}))
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue(any("运行中" in x.value for x in app.info))
            self.assertEqual(app.get("progress")[0].proto.value, 50)
            def stop(manager, directory):
                status["status"] = "stopped"
                status_path.write_text(json.dumps(status))
                return True
            with patch("ui_jobs.stop_job", side_effect=stop) as stopped:
                app.button(key="release_stop_selftest").click().run()
                self.assertFalse(app.exception)
                stopped.assert_called_once()
                self.assertTrue(any("用户停止" in x.value for x in app.warning))
                self.assertFalse(any(x.label == "停止运行" for x in app.button))
                self.assertFalse(app.get("progress"))
            status["status"] = "failed"
            status_path.write_text(json.dumps(status))
            app.run()
            self.assertFalse(app.get("progress"))
            self.assertTrue(any("最后阶段：裁判评分" in x.value for x in app.caption))

    def test_direct_worker_with_local_websocket_and_judge(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from websockets.asyncio.server import serve
        from test_connection_scenes import config as local_config
        from ui_jobs import JobManager
        observed = []
        loop = asyncio.new_event_loop()
        async def handler(socket):
            observed.append(socket.request.path)
            hello = json.loads(await socket.recv())
            observed.append(hello)
            await socket.send(json.dumps({"type": "hello", "session_id": "local-session"}))
            await socket.send(json.dumps({"type": "mcp", "payload": {"id": 1, "method": "tools/list"}}))
            async for raw in socket:
                message = json.loads(raw)
                if message["type"] == "listen":
                    await socket.send(json.dumps({"type": "tts", "state": "stop", "text": "你好，我在。"}))
        async def start():
            return await serve(handler, "127.0.0.1", 0)
        server = loop.run_until_complete(start())
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        class JudgeHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return
            def do_POST(self):
                observed.append(self.path)
                self.rfile.read(int(self.headers["Content-Length"]))
                response = {"id": "local", "object": "chat.completion", "created": 0, "model": "mock",
                            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"score":0.9,"reason":"符合"}'}}]}
                raw = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        http = ThreadingHTTPServer(("127.0.0.1", 0), JudgeHandler)
        http_thread = threading.Thread(target=http.serve_forever, daemon=True)
        http_thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = replace(local_config(tmp), connection_mode="direct", trigger_type="release_selftest",
                              release_environment="测试环境", ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/release",
                              judge_base_url=f"http://127.0.0.1:{http.server_port}/v1",
                              hello_drain_per_wait=.001, hello_drain_idle_rounds=1)
                manager = JobManager()
                with patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                    directory = manager.start(cfg)
                process = next(iter(manager._processes.values()))[0]
                process.wait(timeout=30)
                status = json.loads((directory / "job.json").read_text())
                self.assertEqual(status["status"], "completed", status)
                self.assertEqual(status["trigger_type"], "release_selftest")
                self.assertEqual(status["release_environment"], "测试环境")
                self.assertEqual(json.loads((directory / "progress.json").read_text())["stage"], "生成报告")
                self.assertTrue(any(isinstance(x, str) and x.startswith("/release?") for x in observed))
                self.assertFalse(any(x == "/ota" for x in observed))
                self.assertTrue(any(x == "/v1/chat/completions" for x in observed))
                for report in status["report_paths"].values():
                    self.assertTrue(Path(report).is_file())
                for file in [directory / "job.json", directory / "run.log", Path(status["report_paths"]["detail_report"])]:
                    self.assertNotIn(cfg.token, file.read_text())
        finally:
            http.shutdown()
            http.server_close()
            http_thread.join(5)
            async def stop():
                server.close()
                await server.wait_closed()
            asyncio.run_coroutine_threadsafe(stop(), loop).result(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()


if __name__ == "__main__":
    unittest.main()
