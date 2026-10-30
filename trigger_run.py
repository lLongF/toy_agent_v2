"""文件事件与每日触发器。统一调用现有评测核心，不依赖 Streamlit。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import signal
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cli import parse_args
from safe_output import redact_data, redact_text, SafeLog
from text_input_test import run_framework
from ui_jobs import write_status
from ui_request import validate_request

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".triggers"
CONFIG = ROOT / "settings/triggers.json"


def validate_trigger_config(config):
    if not isinstance(config, dict) or type(config.get("daily_enabled")) is not bool or type(config.get("watch_events")) is not bool:
        raise ValueError("触发配置开关必须为布尔值")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", config.get("daily_time", "")):
        raise ValueError("每日时间应为 HH:MM")
    ZoneInfo(config.get("timezone", ""))
    if not isinstance(config.get("toy_ids"), list) or not config["toy_ids"] or any(not isinstance(x, str) or not re.fullmatch(r"[\w-]+", x) for x in config["toy_ids"]):
        raise ValueError("至少选择一个有效玩具")


def validate_event(event):
    if not isinstance(event, dict) or set(event) - {"event_id", "toy_id", "categories", "case_ids", "notify", "datasets", "environment", "judges"}:
        raise ValueError("事件只接受 event_id、toy_id、categories、case_ids、notify，不允许凭据或任意命令")
    if not isinstance(event.get("event_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", event["event_id"]):
        raise ValueError("event_id 必须为 1–80 位字母、数字、下划线或短横线")
    if not isinstance(event.get("toy_id"), str) or not re.fullmatch(r"[\w-]+", event["toy_id"]):
        raise ValueError("事件必须指定有效 toy_id")
    if event.get("environment") and (not isinstance(event["environment"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", event["environment"])):
        raise ValueError("environment 必须为有效环境名")
    for key in ("categories", "case_ids", "datasets", "judges"):
        if not isinstance(event.get(key, []), list) or any(not isinstance(x, str) or not x.strip() or "," in x for x in event.get(key, [])):
            raise ValueError(f"{key} 必须为非空字符串列表")
    if type(event.get("notify", False)) is not bool:
        raise ValueError("notify 必须为布尔值")


def execute_event(event, state_dir=STATE):
    validate_event(event)
    state_dir = Path(state_dir)
    claims = state_dir / "events"
    claims.mkdir(parents=True, exist_ok=True)
    record_path = claims / (event["event_id"] + ".json")
    try:
        descriptor = os.open(record_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        previous = json.loads(record_path.read_text(encoding="utf-8"))
        if previous.get("event") != event:
            raise ValueError("event_id 已被其他内容使用，请生成新的 ID")
        return dict(previous, duplicate=True)
    record = {"event": event, "status": "running", "started_at": datetime.now().isoformat(), "pid": os.getpid()}
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(record, file, ensure_ascii=False)
    def timed_out():
        write_status(record_path, redact_data(dict(record, status="failed", error="事件评测超过 4 小时上限",
                                                   finished_at=datetime.now().isoformat())))
        os._exit(124)
    timer = threading.Timer(4 * 3600, timed_out)
    timer.daemon = True
    timer.start()
    try:
        arguments = ["--toy", event["toy_id"]]
        if event.get("categories"):
            arguments += ["--categories", ",".join(event["categories"])]
        if event.get("case_ids"):
            arguments += ["--case-ids", ",".join(event["case_ids"])]
        if event.get("datasets"):
            arguments += ["--datasets", ",".join(event["datasets"])]
        if event.get("environment"):
            arguments += ["--environment", event["environment"]]
        if "judges" in event:
            arguments += ["--judges", ",".join(event["judges"])]
        cfg, _ = parse_args(arguments)
        cfg.api_key = cfg.api_key or os.getenv("DASHSCOPE_API_KEY", "")
        cfg.deepseek_api_key = cfg.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")
        if isinstance(sys.stdout, SafeLog):
            sys.stdout.secrets = [cfg.token, cfg.api_key, cfg.deepseek_api_key]
        cfg.trigger_type, cfg.run_id = "event", event["event_id"]
        cfg.reports_dir = cfg.reports_dir / "events" / event["event_id"]
        validate_request(cfg)
        result = asyncio.run(run_framework(cfg))
        record["report_paths"] = result.get("report_paths", {})
        if result.get("error") or result.get("execution_status") == "failed":
            raise RuntimeError("事件评测执行失败，请查看生成报告")
        record["status"] = "completed"
        if event.get("notify"):
            try:
                from notify_wecom import notify
                webhook = os.getenv("WECOM_WEBHOOK_URL", "")
                if not webhook:
                    raise ValueError("缺少 WECOM_WEBHOOK_URL")
                notify(Path(result["report_paths"]["xlsx_export"]), webhook, f'事件 {event["event_id"]}')
                record["notification"] = "sent"
            except Exception as exc:
                record["notification"] = "failed"
                record["notification_error"] = redact_text(str(exc))
    except KeyboardInterrupt:
        record.update(status="failed", error="触发器被停止，评测中断", finished_at=datetime.now().isoformat())
        write_status(record_path, redact_data(record))
        raise
    except Exception as exc:
        record.update(status="failed", error=redact_text(str(exc)))
    finally:
        timer.cancel()
    record["finished_at"] = datetime.now().isoformat()
    write_status(record_path, redact_data(record))
    return record


def scan_events(state_dir=STATE):
    state_dir = Path(state_dir)
    inbox = state_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted(inbox.glob("*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
            records.append(execute_event(event, state_dir))
            (state_dir / "invalid" / path.name).unlink(missing_ok=True)
        except Exception as exc:
            records.append({"status": "failed", "file": path.name, "error": redact_text(str(exc))})
            errors = state_dir / "invalid"
            errors.mkdir(exist_ok=True)
            write_status(errors / path.name, records[-1])
    return records


def run_daily_if_due(config, state_dir=STATE, now=None):
    validate_trigger_config(config)
    now = now or datetime.now(ZoneInfo(config["timezone"]))
    now = now.astimezone(ZoneInfo(config["timezone"]))
    if not config["daily_enabled"] or now.strftime("%H:%M") < config["daily_time"]:
        return None
    directory = Path(state_dir) / "daily"
    directory.mkdir(parents=True, exist_ok=True)
    record_path = directory / (now.strftime("%Y%m%d") + ".json")
    try:
        descriptor = os.open(record_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    record = {"status": "running", "started_at": now.isoformat(), "toy_ids": config["toy_ids"]}
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(record, file)
    try:
        process = subprocess.run([sys.executable, str(ROOT / "daily_run.py")], cwd=ROOT,
                                 env=dict(os.environ, TOY_IDS=",".join(config["toy_ids"])), timeout=4 * 3600)
        record["status"] = "completed" if process.returncode == 0 else "failed"
        record["exit_code"] = process.returncode
    except Exception as exc:
        record.update(status="failed", error=redact_text(str(exc)))
    record["finished_at"] = datetime.now(ZoneInfo(config["timezone"])).isoformat()
    write_status(record_path, record)
    return record


def watch(config_path=CONFIG, state_dir=STATE, once=False):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / "watch.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("已有触发器运行或遗留锁；确认旧进程停止后再清理 watch.lock") from None
    os.close(descriptor)
    try:
        while True:
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            validate_trigger_config(config)
            write_status(state_dir / "heartbeat.json", {"pid": os.getpid(), "updated_at": datetime.now().isoformat(), "status": "running"})
            if config["watch_events"]:
                scan_events(state_dir)
            run_daily_if_due(config, state_dir)
            if once:
                break
            time.sleep(5)
    finally:
        write_status(state_dir / "heartbeat.json", {"pid": os.getpid(), "updated_at": datetime.now().isoformat(), "status": "stopped"})
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="本地每日 / 文件事件触发，共用现有评测主业务")
    parser.add_argument("--event-file", type=Path)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--state-dir", type=Path, default=STATE)
    args = parser.parse_args()
    sys.stdout = SafeLog(sys.stdout)
    def stop_signal(*args):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_signal)
    try:
        if args.event_file:
            result = execute_event(json.loads(args.event_file.read_text(encoding="utf-8")), args.state_dir)
            print(json.dumps(redact_data(result), ensure_ascii=False))
            return 1 if result["status"] == "failed" or result.get("notification") == "failed" else 0
        if args.watch or args.once:
            watch(args.config, args.state_dir, once=args.once)
            return 0
        parser.print_help()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(redact_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
