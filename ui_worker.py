"""界面启动的独立评测进程，通过 stdin 接收配置。"""
from __future__ import annotations

import asyncio
import json
import sys
import os
import threading
from datetime import datetime
from pathlib import Path

from config import Config
from text_input_test import run_framework
from ui_jobs import write_status
from safe_output import redact_text, SafeLog
from ui_request import validate_request


def main() -> int:
    directory = Path(sys.argv[1]).resolve()
    status_path = directory / "job.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    secrets: list[str] = []
    timer = None
    try:
        payload = json.load(sys.stdin)
        secrets = [str(payload.get(k) or "") for k in ("token", "api_key", "deepseek_api_key")]
        secrets.extend(str(value) for value in payload.get("judge_credentials", {}).values() if value)
        sys.stdout = SafeLog(sys.stdout, secrets)
        for key in ("cases_xlsx", "mcp_tools_file", "reports_dir", "export_xlsx", "judge_template_file", "thresholds_file"):
            if payload.get(key) is not None:
                payload[key] = Path(payload[key])
        cfg = Config(**payload)
        validate_request(cfg)
        def timed_out():
            timeout_status = dict(status, status="failed", error="评测超过 4 小时运行上限，任务已终止。",
                                  finished_at=datetime.now().isoformat())
            write_status(status_path, timeout_status)
            os._exit(124)

        timer = threading.Timer(4 * 60 * 60, timed_out)
        timer.daemon = True
        timer.start()
        result = asyncio.run(run_framework(cfg))
        problem = result.get("error") or ("执行异常，请查看报告" if result.get("execution_status") == "failed" else None)
        if not problem:
            detail = json.loads(Path(result["report_paths"]["detail_report"]).read_text(encoding="utf-8"))
            if not detail["results"] or any(g.get("status") != "completed" for g in detail["results"]):
                problem = "部分类型采集未完成，请检查报告和连接日志。"
        status.update(status="failed" if problem else "completed", report_paths=result.get("report_paths", {}))
        if problem:
            status["error"] = str(problem)
    except Exception as exc:
        status.update(status="failed", error=str(exc))
    finally:
        if timer is not None:
            timer.cancel()
    for secret in secrets:
        if secret and "error" in status:
            status["error"] = status["error"].replace(secret, "[REDACTED]")
    if "error" in status:
        status["error"] = redact_text(status["error"], secrets)
    status["finished_at"] = datetime.now().isoformat()
    write_status(status_path, status)
    return 1 if status["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
