"""Temporary-project UI actions and offline scoring/report/worker integration."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import openpyxl

import category_store
import judge_store
from judge import LLMJudge
from test_integration import config, FakeSocket
from text_input_test import run_framework
from ui_jobs import JobManager


class ScoringUITests(unittest.TestCase):
    @staticmethod
    def template(root):
        destination = Path(root) / "templates"
        destination.mkdir(exist_ok=True)
        shutil.copy2(Path(__file__).resolve().parents[1] / "templates/template.md", destination / "template.md")

    @staticmethod
    def widget(widgets, label):
        return next(w for w in widgets if w.label == label)

    def test_type_management_creates_and_edits_real_registry(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as tmp, patch.object(category_store, "ROOT", Path(tmp)):
            self.template(tmp)
            app = AppTest.from_string("from ui_scoring import type_management\ntype_management()", default_timeout=30).run()
            self.assertFalse(app.exception)
            self.widget(app.text_input, "类型名称").set_value("页面自定义")
            self.widget(app.selectbox, "基础执行方式").set_value("情感交互")
            self.widget(app.text_area, "分类评分标准").set_value("必须安抚并尊重用户")
            self.widget(app.number_input, "默认文本通过阈值").set_value(.81)
            self.widget(app.button, "保存用例类型").click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            item = next(i for i in category_store.list_case_types() if i["name"] == "页面自定义")
            self.assertEqual(item["handler"], "emotion")
            self.assertEqual(item["threshold"], .81)
            app.run()  # Refresh the options computed before the form save.
            self.widget(app.selectbox, "编辑类型").set_value("页面自定义").run()
            self.widget(app.text_area, "分类评分标准").set_value("修改后独立评分标准")
            self.widget(app.number_input, "默认文本通过阈值").set_value(.88)
            self.widget(app.button, "保存用例类型").click().run()
            self.assertFalse(app.exception)
            stored = json.loads((Path(tmp) / "settings/case_types.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["case_types"][0]["rubric"], "修改后独立评分标准")
            self.assertEqual(stored["case_types"][0]["threshold"], .88)
            self.assertTrue(list((Path(tmp) / "settings/.backups").glob("*_case_types.json")))

    def test_judge_management_creates_edits_and_encrypts_real_temp_key(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as tmp, patch.object(judge_store, "PROJECT_ROOT", Path(tmp)):
            app = AppTest.from_string("from ui_scoring import judge_management\njudge_management()", default_timeout=30).run()
            self.assertFalse(app.exception)
            for label, value in [("裁判名称", "页面裁判"), ("模型 ID", "mock-model"),
                                 ("兼容接口 Base URL", "https://judge.invalid/v1"),
                                 ("API Key（本机加密保存）", "temporary-ui-mock-secret")]:
                self.widget(app.text_input, label).set_value(value)
            self.widget(app.text_area, "模型兼容参数 JSON").set_value('{"enable_thinking":false}')
            self.widget(app.button, "保存裁判模型").click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            item = next(i for i in judge_store.list_judge_models() if i["name"] == "页面裁判")
            self.assertTrue(item["credential_ref"].startswith("local:"))
            self.assertEqual(judge_store.resolve_judge_credential(item), "temporary-ui-mock-secret")
            app.run()  # Saved models become selectable on the next render.
            self.widget(app.selectbox, "编辑裁判模型").set_value(item["id"]).run()
            self.assertEqual(self.widget(app.text_input, "API Key（本机加密保存）").value, "")
            self.widget(app.text_input, "裁判名称").set_value("改名裁判")
            self.widget(app.checkbox, "启用裁判").uncheck()
            self.widget(app.button, "保存裁判模型").click().run()
            self.assertFalse(app.exception)
            updated = next(i for i in judge_store.list_judge_models() if i["id"] == item["id"])
            self.assertFalse(updated["enabled"])
            self.assertEqual(updated["name"], "改名裁判")
            self.assertEqual(updated["credential_ref"], item["credential_ref"])
            for path in (Path(tmp) / "settings").rglob("*.json"):
                self.assertNotIn("temporary-ui-mock-secret", path.read_text(encoding="utf-8"))

    def test_configure_judges_single_dual_empty_selection(self):
        from ui_scoring import configure_judges
        with tempfile.TemporaryDirectory() as tmp, patch.object(judge_store, "PROJECT_ROOT", Path(tmp)), \
             patch.dict("os.environ", {"DASHSCOPE_API_KEY": "mock-qwen", "DEEPSEEK_API_KEY": "mock-deepseek"}):
            for identities in [["qwen"], ["qwen", "deepseek"], []]:
                with self.subTest(identities=identities):
                    cfg = config()
                    cfg.api_key = cfg.deepseek_api_key = ""
                    cfg.selected_judge_ids = identities
                    configure_judges(cfg)
                    self.assertEqual([m["id"] for m in cfg.judge_endpoints_snapshot], identities)
                    self.assertEqual(list(cfg.judge_credentials), identities)
                    self.assertNotIn("mock-qwen", str(cfg.judge_endpoints_snapshot))
                    self.assertNotIn("mock-deepseek", str(cfg.judge_endpoints_snapshot))

    def test_worker_payload_keys_only_stdin_never_status_log_or_command(self):
        class Input(io.StringIO):
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config()
            cfg.reports_dir = Path(tmp)
            cfg.selected_judge_ids = ["custom"]
            cfg.judge_endpoints_snapshot = [dict(id="custom", name="Custom", model="mock", base_url="https://judge.invalid",
                                               credential_ref="MOCK_KEY", enabled=True, extra_body={})]
            cfg.judge_credentials = {"custom": "worker-memory-mock-secret"}
            process = Mock()
            process.poll.return_value = None
            process.stdin = Input()
            with patch("ui_jobs.subprocess.Popen", return_value=process) as popen:
                directory = JobManager().start(cfg)
            payload = json.loads(process.stdin.getvalue())
            self.assertEqual(payload["judge_credentials"], cfg.judge_credentials)
            self.assertNotIn("worker-memory-mock-secret", str(popen.call_args.args))
            for path in directory.rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"worker-memory-mock-secret", path.read_bytes())
            self.assertEqual(sorted(p.name for p in directory.iterdir()), ["job.json", "run.log"])

    def test_custom_category_dynamic_judges_through_real_report_and_rules_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(category_store, "ROOT", Path(tmp)), \
             patch.object(judge_store, "PROJECT_ROOT", Path(tmp)):
            root = Path(tmp)
            self.template(root)
            category_store.create_case_type("专业共情", "情感交互", "CUSTOM EXPECT EMPATHY", .83)
            for identity in ["alpha", "beta"]:
                judge_store.create_judge_model(identity, "mock-model", "https://judge.invalid/v1", judge_id=identity)
            for identities, category, required in [(["alpha"], "专业共情", False), (["alpha", "beta"], "专业共情", False), ([], "MCP调用", True)]:
                with self.subTest(identities=identities):
                    cfg = config()
                    cfg.cases_xlsx = root / ("cases" + str(len(identities)) + ".xlsx")
                    cfg.reports_dir = root / ("reports" + str(len(identities)))
                    cfg.mcp_tools_file = root / "missing-tools.txt"
                    cfg.selected_judge_ids = identities
                    cfg.judge_endpoints_snapshot = judge_store.snapshot_judge_models(identities)
                    cfg.judge_credentials = {i: "integration-memory-mock-secret" for i in identities}
                    cfg.send_interval = 0
                    cfg.response_collect_timeout = .1
                    cfg.hello_drain_per_wait = .001
                    cfg.hello_drain_idle_rounds = 1
                    cfg.collect_recv_timeout = .001
                    cfg.collect_quiet_seconds = .002
                    cfg.mcp_completion_quiet_seconds = .002
                    cfg.mcp_post_stop_drain_seconds = .002
                    wb = openpyxl.Workbook()
                    wb.active.title = "Test case"
                    wb.active.append(["类型", "用例编号", "消息列表", "预期结果", "是否工具调用", "工具名称"])
                    wb.active.append([category, "1", "工具" if required else "你好", "安抚", "是" if required else "否", "ping" if required else ""])
                    wb.save(cfg.cases_xlsx)
                    wb.close()
                    prompts = []
                    async def connect(*args, **kwargs):
                        return FakeSocket()
                    def chat(judge, client, prompt):
                        prompts.append(prompt)
                        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(dict(score=.9, reason="mock response"))))])
                    response = SimpleNamespace(ok=True, status_code=200, json=lambda: {"websocket": {"url": "ws://test.invalid"}})
                    with patch("collection.requests.post", return_value=response), patch("collection.websockets.connect", side_effect=connect), \
                         patch.object(LLMJudge, "_create_client", return_value=(object(), None)) as initialize, patch.object(LLMJudge, "_chat", chat):
                        result = asyncio.run(run_framework(cfg))
                    self.assertNotIn("error", result)
                    report = Path(result["report_paths"]["detail_report"])
                    raw = report.read_text(encoding="utf-8")
                    self.assertNotIn("integration-memory-mock-secret", raw)
                    data = json.loads(raw)
                    self.assertEqual(data["results"][0]["category"], category)
                    self.assertTrue(data["results"][0]["group_pass"])
                    if required:
                        initialize.assert_not_called()
                    else:
                        self.assertEqual(len(prompts), len(identities))
                        self.assertIn("专业共情", prompts[0])
                        self.assertIn("CUSTOM EXPECT EMPATHY", prompts[0])
                        book = openpyxl.load_workbook(result["report_paths"]["xlsx_export"])
                        try:
                            values = list(book.active.values)
                            headers = list(values[0])
                            self.assertIn("Qwen评分", headers)
                            self.assertIn("DeepSeek评分", headers)
                            for identity in identities:
                                self.assertEqual(values[1][headers.index(identity + "评分")], .9)
                                self.assertEqual(values[1][headers.index(identity + "原因")], "mock response")
                        finally:
                            book.close()
                    for path in cfg.reports_dir.rglob("*.json"):
                        self.assertNotIn("integration-memory-mock-secret", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
