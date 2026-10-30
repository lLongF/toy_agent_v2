#!/usr/bin/env python3
"""每日跑测：按玩具执行 text_input_test.py，再把结果 Excel 发到企微。"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from toy_profiles import DEFAULT_TOY_ID, TOYS_DIR, load_toy_profile

ROOT = Path(__file__).resolve().parent


def selected_toy_ids() -> list[str]:
    raw = (os.environ.get("TOY_IDS") or os.environ.get("TOY_ID") or "").strip()
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return [DEFAULT_TOY_ID]


def main() -> int:
    reports_root = ROOT / "reports"
    reports_root.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    toy_ids = selected_toy_ids()
    api_key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    deepseek_api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    fast = os.environ.get("DAILY_FAST", "").strip() in ("1", "true", "yes")

    overall = 0
    generated: list[Path] = []
    extras: list[str] = []
    for toy_id in toy_ids:
        try:
            toy = load_toy_profile(toy_id, TOYS_DIR)
        except FileNotFoundError as exc:
            extras.append(str(exc))
            overall = overall or 2
            continue
        toy_reports = reports_root / toy.id
        toy_reports.mkdir(exist_ok=True)
        xlsx = toy_reports / f"daily_result_{stamp}.xlsx"
        cmd = [
            sys.executable,
            str(ROOT / "text_input_test.py"),
            "--toy",
            toy.id,
            "--run",
            "--export-xlsx",
            str(xlsx),
            "--reports-dir",
            str(toy_reports),
        ]
        token = toy.resolve_token()
        if token:
            cmd += ["--token", token]
        if api_key:
            cmd += ["--api-key", api_key]
        if deepseek_api_key:
            cmd += ["--deepseek-api-key", deepseek_api_key]
        if fast:
            cmd.append("--fast")

        print(f"开始每日测试 [{toy.id}]:", " ".join(cmd[:8]), "...", flush=True)
        proc = subprocess.run(cmd, cwd=str(ROOT))
        if proc.returncode != 0:
            extras.append(f"{toy.id} 退出码 {proc.returncode}")
            overall = overall or proc.returncode
        if xlsx.is_file():
            generated.append(xlsx)
        else:
            extras.append(f"{toy.id} 未生成 Excel")

    extra = "；".join(extras)
    if generated:
        last_code = 0
        for path in generated:
            notify = [
                sys.executable,
                str(ROOT / "notify_wecom.py"),
                str(path),
                "--message",
                extra,
            ]
            n = subprocess.run(notify, cwd=str(ROOT))
            last_code = n.returncode or last_code
        if overall:
            return overall
        return last_code

    notify = [
        sys.executable,
        str(ROOT / "notify_wecom.py"),
        "--message",
        extra or "测试未生成 Excel",
    ]
    n = subprocess.run(notify, cwd=str(ROOT))
    if overall:
        return overall
    return n.returncode


if __name__ == "__main__":
    raise SystemExit(main())
