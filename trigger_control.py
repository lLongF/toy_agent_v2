"""Streamlit 仅负责显式启动触发器进程；调度逻辑位于独立 CLI。"""
import os
import subprocess
import sys
import threading
from pathlib import Path

from trigger_run import ROOT, STATE


class TriggerController:
    def __init__(self):
        self.process = None
        self.lock = threading.Lock()

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        with self.lock:
            if self.running() or (STATE / "watch.lock").exists():
                raise ValueError("已有本地触发器运行，或存在待检查的遗留锁")
            STATE.mkdir(exist_ok=True)
            with (STATE / "watch.log").open("a", encoding="utf-8") as log:
                self.process = subprocess.Popen([sys.executable, str(ROOT / "trigger_run.py"), "--watch"],
                                                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                                env=dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1"))

    def stop(self):
        with self.lock:
            if not self.running():
                return
            # 只停止本控制器创建的触发器，不操作外部 CLI 或评测进程。
            self.process.terminate()
            self.process.wait(timeout=15)
            (STATE / "watch.lock").unlink(missing_ok=True)
