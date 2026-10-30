"""独立自测结果展示：临时报告、真实筛选与报告快照，无外部调用。"""
import json
from pathlib import Path
import tempfile
import unittest

from streamlit.testing.v1 import AppTest


class ReleasePresentationTests(unittest.TestCase):
    def make_app(self, root, status="completed"):
        folder = root / "toy" / "ui_runs" / "presentation"
        folder.mkdir(parents=True)
        specs = [{"sheet_row": i, "case_id": f"QA-{i}", "user_messages": [f"问题{i}"],
                  "expected_outcome": f"预期{i}"} for i in range(2, 7)]
        result = {"protocol_info": {"toy_id": "toy", "trigger_type": "release_selftest"}, "results": [{
            "category": "基础问答", "case_spec": {"turns": specs}, "turn_results": [
                {"sheet_row": 2, "pass": False, "combined_server_text": "不符合", "judge_result": {
                    "judge_available": True, "score": .2, "threshold": .7, "reason": "不符合预期"}},
                {"sheet_row": 3, "pass": True, "combined_server_text": "符合", "judge_result": {
                    "judge_available": True, "score": .8, "threshold": .7, "reason": "符合预期"}},
                {"sheet_row": 4, "pass": False, "judge_result": {"judge_available": False, "reason": "超时"}},
                {"sheet_row": 5, "pass": False, "execution_error": "连接失败"}]}]}
        report = folder / "result.json"
        report.write_text(json.dumps(result), encoding="utf-8")
        (folder / "result.xlsx").write_bytes(b"download-fixture")
        (folder / "job.json").write_text(json.dumps({"trigger_type": "release_selftest", "status": status,
            "report_paths": {"detail_report": str(report), "xlsx_export": str(folder / "result.xlsx")}}), encoding="utf-8")
        script = f'''from pathlib import Path
from types import SimpleNamespace
import streamlit as st
from ui_release import release_history
from ui_styles import apply_release_demo_styles
apply_release_demo_styles()
def detail(row): st.text(row['expected'])
def open_report(path): st.session_state['opened_report'] = str(path)
release_history(SimpleNamespace(refresh=lambda: None), Path({str(root)!r}), detail, open_report, presentation=True)
'''
        return AppTest.from_string(script, default_timeout=30).run()

    def test_counts_filters_search_selection_and_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            self.assertFalse(app.exception)
            markup = "\n".join(m.value for m in app.markdown)
            self.assertIn("50.0%", markup)
            for label in ("未执行", "执行异常", "评分失败", "用例总数"):
                self.assertIn(label, markup)
            self.assertEqual(len(app.dataframe[0].value), 5)
            self.assertTrue(any(d.label == "下载 Excel 报告" for d in app.get("download_button")))
            detail = next(s for s in app.selectbox if s.label == "查看用例详情")
            detail.set_value(1).run()
            self.assertTrue(any("预期3" in m.value for m in app.markdown))
            app.radio(key="release_filter").set_value("异常 / 未执行").run()
            self.assertEqual(set(app.dataframe[0].value["状态"]), {"评分失败", "执行异常", "未执行"})
            app.text_input(key="release_result_query").set_value("QA-5").run()
            self.assertEqual(len(app.dataframe[0].value), 1)
            self.assertTrue(any("连接失败" in m.value for m in app.markdown))
            app.text_input(key="release_result_query").set_value("无匹配").run()
            self.assertFalse(app.dataframe)
            self.assertTrue(any("没有匹配" in m.value for m in app.info))
            app.button(key="release_full_report").click().run()
            self.assertTrue(app.session_state["opened_report"].endswith("result.json"))

    def test_incomplete_task_never_claims_completed(self):
        for status in ("failed", "stopped"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                app = self.make_app(Path(temporary), status)
                self.assertFalse(app.exception)
                markup = "\n".join(m.value for m in app.markdown)
                self.assertIn("结果尚不完整", markup)
                self.assertNotIn("本次自测已完成", markup)
                self.assertFalse(app.get("progress"))


if __name__ == "__main__":
    unittest.main()
