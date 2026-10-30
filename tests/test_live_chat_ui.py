"""实时对话导航与嵌入回归；AppTest 不加载远程 iframe 或发送对话。"""
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


PROJECT = Path(__file__).resolve().parents[1]
CHAT_URLS = {
    "曦仔": "https://lycheeai-muxi-test.pages.dev/?mac=02:00:00:00:00:0b&productName=%E6%9B%A6%E4%BB%94",
    "粤悦": "https://lycheeai-muxi-test.pages.dev/?mac=02:00:00:00:00:0c&productName=%E5%B9%BF%E7%94%B5-%E7%B2%A4%E6%82%A6",
    "仪宝": "https://lycheeai-muxi-test.pages.dev/?mac=02:00:00:00:00:0f&productName=%E4%BB%AA%E7%94%B5-%E4%BB%AA%E5%AE%9D",
    "闹闹": "https://lycheeai-muxi-test.pages.dev/?mac=02:00:00:00:00:13&productName=%E8%8F%B2%E8%8F%B2%E7%89%B9-%E9%97%B9%E9%97%B9",
}


class LiveChatUITests(unittest.TestCase):
    def test_demo_is_independent_and_reuses_all_chat_targets(self):
        with patch("toy_profiles.list_toy_profiles", side_effect=AssertionError("演示页不能依赖评测档案")), \
             patch("collection.requests.post", side_effect=AssertionError("演示页不能自动请求 OTA")):
            app = AppTest.from_string("from streamlit_app import chat_demo\nchat_demo()", default_timeout=30)
            app.query_params["toy"] = "小智"
            app.run()
            self.assertFalse(app.exception)
            self.assertFalse(app.sidebar.radio)
            self.assertFalse(app.sidebar.selectbox)
            self.assertEqual(app.radio(key="demo_chat_toy").value, "小智")
            self.assertEqual(app.get("iframe")[0].proto.alt, "小智实时对话")
            for name, url in CHAT_URLS.items():
                app.radio(key="demo_chat_toy").set_value(name).run()
                self.assertFalse(app.exception)
                self.assertEqual(app.get("iframe")[0].proto.src, url)
                self.assertIn(name, app.query_params["toy"])

    def test_submenus_switch_to_distinct_device_and_product(self):
        with patch("ui_jobs.JobManager.start", side_effect=AssertionError("对话菜单不能启动评测")), \
             patch("collection.requests.post", side_effect=AssertionError("菜单渲染不能请求 OTA")):
            app = AppTest.from_file(str(PROJECT / "streamlit_app.py"), default_timeout=30).run()
            menu = app.radio(key="navigation")
            self.assertIn("玩具实时对话", menu.options)
            menu.set_value("玩具实时对话").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.radio(key="live_chat_toy").options, ["曦仔", "粤悦", "仪宝", "闹闹", "小智"])
            self.assertEqual(app.get("iframe")[0].proto.src, CHAT_URLS["曦仔"])
            for name, url in CHAT_URLS.items():
                with self.subTest(toy=name):
                    app.radio(key="live_chat_toy").set_value(name).run()
                    self.assertFalse(app.exception)
                    self.assertEqual(len(app.get("iframe")), 1)
                    self.assertEqual(app.get("iframe")[0].proto.src, url)
                    self.assertEqual(app.get("iframe")[0].proto.alt, name + "实时对话")
                    self.assertFalse(any("在新窗口打开对话" in widget.label for widget in app.get("link_button")))
                    self.assertFalse(any("暂未接入" in item.value for item in app.markdown))
            app.radio(key="live_chat_toy").set_value("小智").run()
            self.assertFalse(app.exception)
            self.assertIn('请唤醒', app.get("iframe")[0].proto.srcdoc)
            self.assertIn('ws://127.0.0.1:', app.get("iframe")[0].proto.srcdoc)
            self.assertEqual(app.get("iframe")[0].proto.alt, "小智实时对话")
            app.radio(key="live_chat_toy").set_value("曦仔").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.get("iframe")[0].proto.src, CHAT_URLS["曦仔"])

    def test_chat_navigation_keeps_evaluation_toy_and_existing_pages(self):
        app = AppTest.from_file(str(PROJECT / "streamlit_app.py"), default_timeout=30).run()
        evaluation_toy = app.session_state["toy_choice"]
        app.radio(key="navigation").set_value("玩具实时对话").run()
        app.radio(key="live_chat_toy").set_value("仪宝").run()
        app.run()
        self.assertEqual(app.radio(key="live_chat_toy").value, "仪宝")
        self.assertEqual(app.session_state["toy_choice"], evaluation_toy)
        app.radio(key="navigation").set_value("结果分析").run()
        self.assertFalse(app.exception)
        self.assertFalse(app.get("iframe"))
        self.assertFalse(any(widget.key == "live_chat_toy" for widget in app.radio))
        app.radio(key="navigation").set_value("玩具管理").run()
        self.assertFalse(app.exception)
        self.assertTrue(any(widget.label == "＋ 接入玩具" for widget in app.button))
        app.radio(key="navigation").set_value("玩具实时对话").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["toy_choice"], evaluation_toy)


if __name__ == "__main__":
    unittest.main()
