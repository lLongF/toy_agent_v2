"""四个页面共用的报告契约、统计和受限文件读取。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from evaluation_support import valid_score
from report import ReportWriter


def confined(path: Path, root: Path) -> Path:
    path, root = path.resolve(), root.resolve()
    if not path.is_relative_to(root):
        raise ValueError("文件不在允许的报告目录内")
    return path


def number(value):
    try:
        return valid_score(value)
    except ValueError:
        return None


def normalise_report(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("报告缺少 results 列表或格式不受支持")
    protocol = data.get("protocol_info") or {}
    if not isinstance(protocol, dict):
        raise ValueError("报告协议元信息格式无效")
    groups = list(data["results"])
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("case_spec") or {}, dict):
            raise ValueError("报告的分组结构无效")
    recorded = {int(t["sheet_row"]) for g in groups if isinstance(g, dict)
                for t in (g.get("case_spec") or {}).get("turns", []) if isinstance(t, dict) and t.get("sheet_row") is not None}
    recorded.update(int(t["sheet_row"]) for g in groups if isinstance(g, dict)
                    for t in g.get("turn_results", []) if isinstance(t, dict) and t.get("sheet_row") is not None)
    for spec in protocol.get("selected_case_specs") or []:
        if not isinstance(spec, dict):
            raise ValueError("报告的范围快照结构无效")
        missing = [t for t in spec.get("turns", []) if int(t["sheet_row"]) not in recorded]
        if missing:
            groups.append({"category": spec.get("category"), "case_spec": {"turns": missing},
                           "reason": protocol.get("run_aborted", "采集未完成")})
    rows = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("报告的分组结构无效")
        spec = group.get("case_spec") or {}
        if not isinstance(spec, dict) or not isinstance(group.get("turn_results", []), list):
            raise ValueError("报告的用例结构无效")
        specs = {int(t["sheet_row"]): t for t in spec.get("turns", []) if isinstance(t, dict) and t.get("sheet_row") is not None}
        turns = {int(t["sheet_row"]): t for t in group.get("turn_results", []) if isinstance(t, dict) and t.get("sheet_row") is not None}
        for sheet_row in dict.fromkeys([*specs, *turns]):
            turn, original = turns.get(sheet_row), specs.get(sheet_row, {})
            fields = ReportWriter._turn_export_fields(turn) if turn else {}
            judge = (turn or {}).get("judge_result") or (turn or {}).get("memory_judge") or {}
            if not isinstance(judge, dict):
                judge = {}
            status = fields.get("result_label", "未执行")
            if not turn or judge.get("judge_pending"):
                status = "未执行"
            score = number(judge.get("score")) if judge.get("judge_available") else None
            threshold = number(judge.get("threshold"))
            messages = original.get("user_messages") or [u.get("user_message", "") for u in (turn or {}).get("utterances", [])]
            messages = [str(m) for m in messages] if isinstance(messages, list) else [str(messages)]
            reason = (turn or {}).get("execution_error") or judge.get("reason") or fields.get("qwen_reason") or group.get("reason", "")
            if (turn or {}).get("mcp_pass") is False:
                failed_checks = [c.get("reason", "MCP 硬规则失败") for c in turn.get("mcp_checks", [])
                                 if c.get("required") and not c.get("pass")]
                reason = "MCP 硬规则失败：" + "; ".join(failed_checks) + (" | " + reason if reason else "")
            rows.append({
                "row": sheet_row, "case_id": str((turn or {}).get("case_id") or original.get("case_id") or sheet_row),
                "category": group.get("category") or spec.get("category") or "未记录类型", "status": status,
                "score": score, "threshold": threshold, "messages": messages,
                "expected": (turn or {}).get("expected_outcome") or original.get("expected_outcome") or "",
                "actual": fields.get("actual", ""), "reason": reason,
                "judges": judge.get("judges") or ([judge] if judge else []),
                "utterances": (turn or {}).get("utterances", []),
                "mcp_required": (turn or {}).get("mcp_required", original.get("expect_tool_call")),
                "mcp_pass": (turn or {}).get("mcp_pass"), "has_mcp": fields.get("has_mcp", False),
                "rule_only": bool(turn and not judge),
                "rule_score": (turn or {}).get("rule_score"),
                "execution_error": (turn or {}).get("execution_error", ""),
                "source": (protocol.get("dataset_snapshot") or {}).get("rows", {}).get(str(sheet_row), {}),
            })
    dataset_ids = list(dict.fromkeys(r["source"].get("dataset_id") for r in rows if r["source"].get("dataset_id")))
    return {"rows": rows, "protocol": protocol, "metrics": summarise(rows),
            "dataset_metrics": [{"dataset_id": identity, "dataset_name": next(r["source"].get("dataset_name", identity) for r in rows if r["source"].get("dataset_id") == identity),
                                 **summarise([r for r in rows if r["source"].get("dataset_id") == identity])} for identity in dataset_ids],
            "category_metrics": [{"category": cat, **summarise([r for r in rows if r["category"] == cat])}
                                 for cat in dict.fromkeys(r["category"] for r in rows)]}


def summarise(rows: list[dict]) -> dict:
    counts = {status: sum(r["status"] == status for r in rows) for status in ["通过", "不通过", "评分失败", "未执行"]}
    evaluated = counts["通过"] + counts["不通过"]
    scores = [r["score"] for r in rows if r["status"] in {"通过", "不通过"} and r["score"] is not None]
    return {"case_count": len(rows), "passed_count": counts["通过"], "failed_count": counts["不通过"],
            "judge_error_count": counts["评分失败"], "unevaluated_count": counts["未执行"], "evaluated_count": evaluated,
            "pass_rate": counts["通过"] / evaluated if evaluated else None,
            "average_score": sum(scores) / len(scores) if scores else None, "scored_count": len(scores)}


def read_report(path: Path, root: Path) -> dict:
    path = confined(path, root)
    data = json.loads(path.read_text(encoding="utf-8"))
    result = normalise_report(data)
    result["path"] = path
    result["job"] = {}
    job_path = path.parent / "job.json"
    if job_path.is_file():
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
            if confined(Path(job.get("report_paths", {}).get("detail_report", "")), root) == path:
                result["job"] = job
        except (ValueError, OSError, TypeError):
            pass
    return result


def discover_reports(root: Path, toy_id: str) -> tuple[list[dict], list[str]]:
    reports, errors = [], []
    for path in sorted(root.rglob("*text_result_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            report = read_report(path, root)
            toy = report["protocol"].get("toy_id")
            if toy == toy_id or (not toy and path.is_relative_to(root / toy_id)):
                reports.append(report)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            errors.append(f"{path.name}：报告不可读取（{type(exc).__name__}）")
    return reports, errors


def attachments(report: dict, root: Path) -> list[Path]:
    """优先用 job 显式路径；旧报告按共同时间戳匹配，绝不枚举整目录下载。"""
    path = confined(report["path"], root)
    candidates = [path]
    for value in {**report["protocol"].get("report_files", {}), **report.get("job", {}).get("report_paths", {})}.values():
        try:
            candidate = confined(Path(value), root)
            if candidate.parent == path.parent:
                candidates.append(candidate)
        except (ValueError, TypeError):
            continue
    candidates.append(path.with_name(path.stem + "_export.xlsx"))
    candidates.append(path.with_name(path.stem.replace("text_result_", "text_summary_", 1) + ".md"))
    # 原每日入口的导出文件自带时间戳，可以与报告一一对应。
    match = re.search(r"text_result_(\d{8}_\d{6})$", path.stem)
    if match:
        candidates.append(path.with_name(f"daily_result_{match[1]}.xlsx"))
    found = []
    for candidate in candidates:
        try:
            candidate = confined(candidate, root)
            if candidate.is_file() and candidate.suffix in {".json", ".md", ".xlsx"} and candidate not in found:
                found.append(candidate)
        except ValueError:
            continue
    return found
