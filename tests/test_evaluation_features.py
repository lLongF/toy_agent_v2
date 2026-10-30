import json
import asyncio
import hashlib
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_integration import config
from test_integration import FakeSocket
from config import load_dialogue_groups_from_xlsx
from evaluation_support import filter_groups, load_rules, render_prompt, valid_score
from judge import LLMJudge, merge_judge_results
from safe_output import redact_data, redact_text
from ui_results import attachments, confined, discover_reports, normalise_report, read_report


class FeatureTests(unittest.TestCase):
    def test_empty_corrupt_and_missing_field_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(discover_reports(root, "default"), ([], []))
            (root / "text_result_bad.json").write_text("{broken")
            reports, errors = discover_reports(root, "default")
            self.assertFalse(reports)
            self.assertEqual(len(errors), 1)
            self.assertEqual(normalise_report({"results": []})["rows"], [])
            with self.assertRaises(ValueError):
                normalise_report({"results": [], "protocol_info": [] or [1]})

    def test_selected_scope_and_category_threshold_are_used_by_engine(self):
        from text_input_test import run_framework

        cfg = config()
        source_hash = hashlib.sha256(cfg.cases_xlsx.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            cfg.reports_dir = Path(tmp)
            cfg.selected_categories = ["基础问答"]
            cfg.selected_case_ids = ["1"]
            cfg.response_collect_timeout = 0.1
            cfg.send_interval = 0
            cfg.collect_quiet_seconds = 0.001
            cfg.hello_drain_per_wait = 0.001
            cfg.hello_drain_idle_rounds = 1
            rules_path = Path(tmp) / "thresholds.json"
            rules_path.write_text(json.dumps({"thresholds": {"基础问答": 0.95}}))
            cfg.thresholds_file = rules_path

            async def connect(*args, **kwargs):
                return FakeSocket()

            response = SimpleNamespace(ok=True, status_code=200, json=lambda: {"websocket": {"url": "ws://test.invalid"}})
            content = json.dumps({"score": 0.9, "pass": True, "reason": "符合", "is_off_topic": False})
            answer = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
            with patch("collection.requests.post", return_value=response), patch("collection.websockets.connect", side_effect=connect), \
                 patch.object(LLMJudge, "_create_client", return_value=(object(), None)), patch.object(LLMJudge, "_chat", return_value=answer):
                result = asyncio.run(run_framework(cfg))
            data = json.loads(Path(result["report_paths"]["detail_report"]).read_text())
            self.assertEqual(len(data["results"]), 1)
            turn = data["results"][0]["turn_results"][0]
            self.assertEqual(turn["case_id"], "1")
            self.assertEqual(turn["judge_result"]["threshold"], 0.95)
            self.assertFalse(turn["pass"])
            self.assertEqual(data["protocol_info"]["selection"]["case_ids"], ["1"])
            self.assertEqual(hashlib.sha256(cfg.cases_xlsx.read_bytes()).hexdigest(), source_hash)

    def test_score_contract(self):
        for score in [0, 0.7, 1]:
            self.assertEqual(valid_score(score), score)
        for score in [-0.01, 1.01, math.nan, math.inf, None, True, "0.7"]:
            with self.assertRaises(ValueError):
                valid_score(score)
        merged = merge_judge_results([{"judge_available": True, "score": math.nan}], config())
        self.assertFalse(merged["judge_available"])
        self.assertNotIn("score", merged)

    def test_thresholds_and_template_snapshot(self):
        cfg = config()
        rules = load_rules(cfg)
        self.assertEqual(set(rules["thresholds"].values()), {0.7})
        self.assertIn("${evil}", render_prompt(rules, "反向安全", {"input": "${evil}"}, "score"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "thresholds.json"
            path.write_text(json.dumps({"thresholds": {"反向安全": 0.9}}))
            updated = load_rules(replace(cfg, thresholds_file=path))
            self.assertEqual(updated["thresholds"]["反向安全"], 0.9)
            self.assertEqual(updated["thresholds"]["基础问答"], 0.7)
            self.assertEqual(rules["thresholds"]["反向安全"], 0.7)
            with self.assertRaises(FileNotFoundError):
                load_rules(replace(cfg, judge_template_file=Path(tmp) / "missing.md"))

    def test_range_and_original_rows(self):
        groups = load_dialogue_groups_from_xlsx(config().cases_xlsx)
        chosen = filter_groups(groups, ["短期记忆"])
        self.assertEqual(len(chosen), 1)
        self.assertEqual(len(chosen[0].turns), 7)
        row = chosen[0].turns[0].sheet_row
        self.assertEqual(len(filter_groups(groups, rows=[row])[0].turns), 1)
        with self.assertRaises(ValueError):
            filter_groups(groups, ["不存在"])
        self.assertEqual(sum(len(g.turns) for g in groups), 98)

    def test_metrics_rule_only_errors_and_unexecuted(self):
        data = {"results": [{"category": "MCP调用", "turn_results": [
            {"sheet_row": 2, "pass": True},
            {"sheet_row": 3, "pass": False},
            {"sheet_row": 4, "pass": False, "judge_result": {"judge_available": False, "score": 0}},
        ], "case_spec": {"turns": [{"sheet_row": 5}]}}]}
        result = normalise_report(data)
        metrics = result["metrics"]
        self.assertEqual(metrics["case_count"], 4)
        self.assertEqual(metrics["pass_rate"], 0.5)
        self.assertEqual(metrics["unevaluated_count"], 1)
        self.assertEqual(metrics["judge_error_count"], 1)
        self.assertIsNone(metrics["average_score"])
        self.assertIsNone(normalise_report({"results": []})["metrics"]["pass_rate"])
        aborted = normalise_report({"results": [], "protocol_info": {"run_aborted": "OTA failed",
            "selected_case_specs": [{"category": "基础问答", "turns": [{"sheet_row": 2, "case_id": "1"}]}]}})
        self.assertEqual(aborted["metrics"]["case_count"], 1)
        self.assertEqual(aborted["metrics"]["unevaluated_count"], 1)
        self.assertIsNone(aborted["metrics"]["pass_rate"])

    def test_attachments_do_not_mix_runs_or_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "reports"
            root.mkdir()
            path = root / "default_text_result_20261027_120000.json"
            path.write_text('{"results": []}')
            export = path.with_name(path.stem + "_export.xlsx")
            export.write_bytes(b"test")
            (root / "other.xlsx").write_bytes(b"other")
            report = read_report(path, root)
            self.assertEqual(set(attachments(report, root)), {path.resolve(), export.resolve()})
            with self.assertRaises(ValueError):
                confined(root.parent / "private.txt", root)

    def test_output_redaction(self):
        data = redact_data({"token": "private", "url": "wss://test/?authorization=secret", "other": "okay"})
        self.assertNotIn("private", json.dumps(data))
        self.assertNotIn("=secret", json.dumps(data))
        self.assertEqual(redact_text("my-key", ["my-key"]), "[REDACTED]")


class UIWorkflowTests(unittest.TestCase):
    def test_filters_empty_results_and_range_navigation(self):
        from streamlit.testing.v1 import AppTest
        from ui_jobs import ROOT

        source = config().cases_xlsx
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        app = AppTest.from_file(str(ROOT / "streamlit_app.py")).run(timeout=60)
        app.sidebar.radio[0].set_value("用例与工具管理").run()
        next(m for m in app.multiselect if m.label == "用例类型").set_value(["短期记忆"]).run()
        self.assertTrue(any("显示 7 / 98" in c.value for c in app.caption))
        search = next(t for t in app.text_input if "搜索用例编号" in t.label)
        search.set_value("不存在的关键词123456").run()
        self.assertTrue(any("显示 0 / 98" in c.value for c in app.caption))
        next(t for t in app.text_input if "搜索用例编号" in t.label).set_value("").run()
        next(b for b in app.button if b.label.startswith("使用7条")).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.sidebar.radio[0].value, "评测配置与执行")
        self.assertEqual(len(app.session_state["scope"]["rows"]), 7)
        self.assertTrue(any("即将评测 7 条" in c.value for c in app.caption))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_toy_switch_invalidates_scope(self):
        from streamlit.testing.v1 import AppTest
        from ui_jobs import ROOT

        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            for toy in ["alpha", "beta"]:
                directory = Path(tmp) / toy
                directory.mkdir()
                (directory / "toy.json").write_text(json.dumps({"token": "", "cases_xlsx": str(cfg.cases_xlsx),
                                                                "mcp_tools_file": str(cfg.mcp_tools_file)}))
            with patch("toy_profiles.TOYS_DIR", Path(tmp)):
                app = AppTest.from_file(str(ROOT / "streamlit_app.py")).run(timeout=60)
                app.session_state["scope"] = {"toy": "alpha", "mode": "指定用例", "rows": [2]}
                app.sidebar.selectbox[0].set_value("beta").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["previous_toy"], "beta")
            with self.assertRaises(KeyError):
                _ = app.session_state["scope"]


if __name__ == "__main__":
    unittest.main()
