"""Streamlit 的进程任务适配器；不改变现有 CLI 和评测引擎。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from config import Config

ROOT = Path(__file__).resolve().parent


def write_status(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def stop_job(manager, directory: Path) -> bool:
    """只终止管理器持有的指定任务；退出后再保存停止状态，避免与 worker 写入竞争。"""
    directory = Path(directory).resolve()
    with manager._lock:
        owned = next(((process, path) for process, path in manager._processes.values()
                      if path.resolve() == directory), None)
        if owned is None:
            raise ValueError("当前服务未持有该任务进程，无法安全停止；请勿重复启动该设备任务。")
        process, _ = owned
        if process.poll() is not None:
            return False
        try:
            process.terminate()
        except ProcessLookupError:
            return False
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        status_path = directory / "job.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status["status"] != "running":
            return False
        status.update(status="stopped", finished_at=datetime.now().isoformat(), stop_reason="用户主动停止")
        status.pop("error", None)
        write_status(status_path, status)
        return True


class JobManager:
    """只共享进程句柄，设备级互斥；不共享协议状态或用户密钥。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, tuple[subprocess.Popen, Path]] = {}

    def start(self, cfg: Config) -> Path:
        from connection_scenes import prepare_scene_run
        cfg = prepare_scene_run(cfg)
        with self._lock:
            previous = self._processes.get(cfg.device_mac)
            if previous and previous[0].poll() is None:
                raise ValueError("该设备已有界面评测任务正在运行，请等待任务结束。")
            job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
            directory = cfg.reports_dir / "ui_runs" / job_id
            directory.mkdir(parents=True)
            # 每次运行隔离输出；不修改调用方配置。固定注册配置与凭据后经stdin发送。
            from dataclasses import replace
            prepared = replace(cfg, judge_credentials=dict(cfg.judge_credentials), judge_endpoints_snapshot=list(cfg.judge_endpoints_snapshot))
            if prepared.selected_judge_ids is not None and not prepared.judge_endpoints_snapshot:
                from judge_store import snapshot_judge_models
                from judge import build_judge_endpoints
                prepared.judge_endpoints_snapshot = snapshot_judge_models(prepared.selected_judge_ids, cfg=prepared)
                prepared.judge_credentials = {ep.name: ep.api_key for ep in build_judge_endpoints(prepared)}
            if not prepared.rules_snapshot:
                from evaluation_support import load_rules
                prepared.rules_snapshot = load_rules(prepared)
            payload = asdict(prepared)
            payload["case_snapshot"] = None  # 快照由每个工作进程独立创建。
            payload["run_id"] = job_id
            payload["reports_dir"] = str(directory)
            payload["export_xlsx"] = str(directory / "result.xlsx")
            for key in ("cases_xlsx", "mcp_tools_file", "judge_template_file", "thresholds_file"):
                payload[key] = str(payload[key])
            status = {"job_id": job_id, "toy_id": cfg.toy_id, "status": "running",
                      "started_at": datetime.now().isoformat(), "device_mac": cfg.device_mac,
                      "connection_scene": cfg.connection_scene, "client_id": cfg.client_id,
                      "trigger_type": cfg.trigger_type, "release_environment": cfg.release_environment,
                      "target_name": cfg.toy_snapshot.get("name", cfg.toy_id)}
            write_status(directory / "job.json", status)
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            try:
                with (directory / "run.log").open("w", encoding="utf-8") as log:
                    process = subprocess.Popen(
                        [sys.executable, str(ROOT / "ui_worker.py"), str(directory)],
                        cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=log,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    )
                self._processes[cfg.device_mac] = (process, directory)
                # 凭据经 stdin 传入，不写任务配置文件或命令行参数。
                assert process.stdin is not None
                process.stdin.write(json.dumps(payload, ensure_ascii=False))
                process.stdin.close()
            except Exception:
                if "process" in locals() and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                status.update(status="failed", error="评测进程启动失败，请查看日志。")
                write_status(directory / "job.json", status)
                raise
            return directory

    def busy(self, device_mac: str) -> bool:
        with self._lock:
            previous = self._processes.get(device_mac)
            return bool(previous and previous[0].poll() is None)

    def active_job(self, device_mac: str):
        with self._lock:
            previous = self._processes.get(device_mac)
            return previous[1] if previous and previous[0].poll() is None else None

    def refresh(self) -> None:
        with self._lock:
            for process, directory in self._processes.values():
                code = process.poll()
                if code is None:
                    continue
                path = directory / "job.json"
                status = json.loads(path.read_text(encoding="utf-8"))
                if status["status"] == "running":
                    status.update(status="failed", error=f"评测进程意外退出（退出码 {code}）。")
                    write_status(path, status)


def report_rows(data: dict) -> list[dict]:
    """沿用原报告的状态和分数提取规则，避免界面另算一套判定。"""
    from report import ReportWriter

    rows = []
    writer = ReportWriter(ROOT / "reports")
    for group in data.get("results", []):
        mapped = writer._build_sheet_row_export_map([group])
        specs = {int(t["sheet_row"]): t for t in (group.get("case_spec") or {}).get("turns", [])}
        for row, fields in mapped.items():
            spec = specs.get(row, {})
            rows.append({
                "类型": group.get("category", ""), "用例编号": str(spec.get("case_id", row)),
                "结果": fields["result_label"], "实际返回内容": fields["actual"],
                "是否有MCP调用": "是" if fields["has_mcp"] else "否",
                "Qwen评分": fields["qwen_score"], "Qwen原因": fields["qwen_reason"],
                "DeepSeek评分": fields["deepseek_score"], "DeepSeek原因": fields["deepseek_reason"],
                "综合评分": fields["combined_score"],
            })
    return rows
