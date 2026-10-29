"""独立自测演示入口，与平台页面共享服务，不启动真实评测。"""
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseRouteTests(unittest.TestCase):
    def test_standalone_renders_selftest_without_platform_navigation(self):
        app = AppTest.from_string('from streamlit_app import release_demo\nrelease_demo()', default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertTrue(any(x.value == '发版自测' for x in app.title))
        self.assertTrue(any(x.key == 'release_start' for x in app.button))
        self.assertFalse(any(x.key == 'navigation' for x in app.radio))
        self.assertFalse(any(x.key == 'toy_choice' for x in app.selectbox))
        self.assertFalse(any(x.key == 'release_environment' for x in app.radio))

    def test_report_stays_in_standalone_route_and_back_restores_form(self):
        with patch('streamlit_app.show_report') as show, patch('streamlit_app.read_report', return_value={'test': True}):
            app = AppTest.from_string('from streamlit_app import release_demo\nrelease_demo()', default_timeout=30)
            app.session_state['release_demo_report'] = str(ROOT / 'reports' / 'mock.json')
            app.run()
            self.assertFalse(app.exception)
            show.assert_called_once_with({'test': True})
            self.assertTrue(any(x.value == '发版自测结果' for x in app.title))
            app.button(key='release_demo_back').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any(x.key == 'release_start' for x in app.button))

    def test_original_menu_entry_still_renders(self):
        app = AppTest.from_file(str(ROOT / 'streamlit_app.py'), default_timeout=30).run()
        app.radio(key='navigation').set_value('发版自测').run()
        self.assertFalse(app.exception)
        self.assertTrue(any(x.key == 'release_start' for x in app.button))
        app.radio(key='navigation').set_value('评测总览').run()
        self.assertFalse(app.exception)


if __name__ == '__main__':
    unittest.main()
