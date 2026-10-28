"""跨入口设备互斥；OS 文件锁随进程退出释放，不需要数据库。"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import os
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent / ".runtime/devices"


@contextmanager
def device_lock(device_mac):
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT / (hashlib.sha256(device_mac.lower().encode()).hexdigest() + ".lock")
    handle = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b" ")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("该设备已有 CLI、界面或触发任务运行，请等待完成") from None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("该设备已有 CLI、界面或触发任务运行，请等待完成") from None
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def exclusive_device(function):
    @wraps(function)
    async def wrapped(cfg):
        with device_lock(cfg.device_mac):
            # 保持整轮采集和最终 Excel 使用同一份数据，不受运行中资产编辑影响。
            with tempfile.TemporaryDirectory(prefix="toy-eval-source-") as temporary:
                if cfg.selected_dataset_ids:
                    from dataset_store import prepare_dataset_snapshot
                    prepared = prepare_dataset_snapshot(cfg, temporary)
                else:
                    snapshot = Path(temporary) / "cases.xlsx"
                    shutil.copyfile(cfg.cases_xlsx, snapshot)
                    prepared = replace(cfg, case_snapshot=snapshot)
                # 工具定义与用例一样冻结，避免运行中编辑改变后续会话。
                if cfg.mcp_tools_file.is_file():
                    tools_snapshot = Path(temporary) / "tools.txt"
                    shutil.copyfile(cfg.mcp_tools_file, tools_snapshot)
                    prepared = replace(prepared, mcp_tools_file=tools_snapshot)
                return await function(prepared)
    return wrapped
