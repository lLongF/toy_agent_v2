"""停止任务真实进程验证，不访问设备或模型服务。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ui_jobs import JobManager, stop_job, write_status


class StopJobTests(unittest.TestCase):
    def test_only_selected_process_stops_and_state_survives_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original = {'job_id': 'test-stop', 'status': 'running', 'report_paths': {'detail_report': 'existing.json'}}
            write_status(directory / 'job.json', original)
            (directory / 'run.log').write_text('已有日志')
            code = '''import sys,time
from pathlib import Path
import execution_lock
execution_lock.ROOT=Path(sys.argv[1])
with execution_lock.device_lock('test-device'):
    print('ready',flush=True)
    time.sleep(60)
'''
            process = subprocess.Popen([sys.executable, '-c', code, str(directory / 'locks')], stdout=subprocess.PIPE, text=True)
            other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            manager = JobManager()
            manager._processes = {'selected': (process, directory), 'other': (other, directory / 'other')}
            try:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                self.assertTrue(stop_job(manager, directory))
                self.assertIsNotNone(process.poll())
                self.assertIsNone(other.poll())
                self.assertFalse(manager.busy('selected'))
                import execution_lock
                with patch.object(execution_lock, 'ROOT', directory / 'locks'), execution_lock.device_lock('test-device'):
                    pass  # 证明停止子进程后设备锁已释放。
                data = json.loads((directory / 'job.json').read_text())
                self.assertEqual(data['status'], 'stopped')
                self.assertEqual(data['report_paths'], original['report_paths'])
                self.assertEqual((directory / 'run.log').read_text(), '已有日志')
                manager.refresh()
                self.assertEqual(json.loads((directory / 'job.json').read_text())['status'], 'stopped')
                self.assertFalse(stop_job(manager, directory))
            finally:
                for child in (process, other):
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
                process.stdout.close()

    def test_unknown_job_is_rejected_and_completion_race_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manager = JobManager()
            with self.assertRaises(ValueError):
                stop_job(manager, directory)
            process = Mock()
            process.poll.return_value = None
            write_status(directory / 'job.json', {'status': 'completed'})
            manager._processes = {'device': (process, directory)}
            self.assertFalse(stop_job(manager, directory))
            self.assertEqual(json.loads((directory / 'job.json').read_text())['status'], 'completed')

    def test_unresponsive_process_is_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_status(directory / 'job.json', {'status': 'running'})
            process = Mock()
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired('mock', 5), 0]
            manager = JobManager()
            manager._processes = {'device': (process, directory)}
            self.assertTrue(stop_job(manager, directory))
            process.kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
