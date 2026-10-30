"""Optional lightweight case types; reuse fixed collectors, never import configured code.

The registry stores custom entries only. No registry means the seven built-ins.
Names are immutable and deletion is deliberately not exposed (assets may reference them).
MCP rule results always require 1; ``threshold`` controls language judging only,
including MCP cases that do not require a tool call.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from toy_management import atomic_json, file_hash
from toy_profiles import safe_path

ROOT = Path(__file__).resolve().parent
BUILTIN_HANDLERS = {
    "基础问答": "basic_qa", "反向安全": "basic_qa", "情感交互": "emotion",
    "短期记忆": "short_term_memory", "长期记忆": "long_term_memory",
    "知识库": "knowledge_base", "MCP调用": "mcp_call",
}
HANDLER_BASES = {handler: name for name, handler in BUILTIN_HANDLERS.items() if name != "反向安全"}


def registry_path(root=None):
    root = Path(root or ROOT).resolve()
    return safe_path(root / "settings" / "case_types.json", root)


def case_types_hash(root=None):
    """CAS token; an absent registry has the empty-string token."""
    return file_hash(registry_path(root))


def _entry(name, base_category, rubric, threshold):
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError("类型名称必须为非空且无首尾空白的字符串")
    if any(ord(c) < 32 for c in name):
        raise ValueError("类型名称不能包含控制字符")
    if name.casefold() in {n.casefold() for n in (*BUILTIN_HANDLERS, *HANDLER_BASES)}:
        raise ValueError("类型名称不能与内置类型或执行方式重名")
    if not isinstance(base_category, str):
        raise ValueError("基础类别必须为已有执行方式或中文内置类别")
    base = HANDLER_BASES.get(base_category, base_category)
    if base not in BUILTIN_HANDLERS:
        raise ValueError("基础类别必须为已有执行方式或中文内置类别")
    if not isinstance(rubric, str) or not rubric.strip():
        raise ValueError("分类评分标准不能为空")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("默认阈值必须为 0–1 的有限数值")
    return dict(name=name, base_category=base, handler=BUILTIN_HANDLERS[base],
                rubric=rubric.strip(), threshold=float(threshold), builtin=False)


def _custom_entries(root=None):
    path = registry_path(root)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1 or not isinstance(raw.get("case_types"), list):
        raise ValueError("自定义类型注册表格式无效")
    entries, names = [], set()
    for item in raw["case_types"]:
        if not isinstance(item, dict) or set(item) - {"name", "base_category", "rubric", "threshold"}:
            raise ValueError("自定义类型字段无效")
        try:
            entry = _entry(item["name"], item["base_category"], item["rubric"], item.get("threshold", .7))
        except KeyError as exc:
            raise ValueError("自定义类型缺少必填字段") from exc
        if entry["name"].casefold() in names:
            raise ValueError("类型名称重复")
        names.add(entry["name"].casefold())
        entries.append(entry)
    return entries


def list_case_types(root=None) -> list[dict]:
    """Return built-ins followed by registered types; reads never create files."""
    template = (ROOT / "templates" / "template.md").read_text(encoding="utf-8")
    rubrics = json.loads(template.split("<!-- rubrics -->")[1])
    builtins = [dict(name=name, base_category=name, handler=handler, rubric=rubrics[name],
                     threshold=.7, builtin=True) for name, handler in BUILTIN_HANDLERS.items()]
    return builtins + _custom_entries(root)


def _save(entries, expected_hash, root):
    fields = ("name", "base_category", "rubric", "threshold")
    return atomic_json(registry_path(root), {"version": 1, "case_types": [
        {key: entry[key] for key in fields} for entry in entries]}, expected_hash, root)


def create_case_type(name, base_category, rubric, threshold=.7, *, expected_hash=None, root=None):
    """Register a type using locked CAS. Pass a previously read hash to reject stale UI edits."""
    root = Path(root or ROOT).resolve()
    current_hash = case_types_hash(root)
    if expected_hash is not None and expected_hash != current_hash:
        raise ValueError("配置已修改，请重新加载后保存")
    entries = _custom_entries(root)
    entry = _entry(name, base_category, rubric, threshold)
    if any(e["name"].casefold() == name.casefold() for e in entries):
        raise ValueError("类型名称重复")
    _save(entries + [entry], current_hash, root)
    return entry


def update_case_type(name, *, expected_hash, base_category=None, rubric=None, threshold=None, root=None):
    """Update custom fields, preserving the name and all existing references."""
    root = Path(root or ROOT).resolve()
    entries = _custom_entries(root)
    for index, old in enumerate(entries):
        if old["name"] == name:
            entry = _entry(name, old["base_category"] if base_category is None else base_category,
                           old["rubric"] if rubric is None else rubric,
                           old["threshold"] if threshold is None else threshold)
            entries[index] = entry
            _save(entries, expected_hash, root)
            return entry
    raise ValueError("自定义类型不存在或内置类型不可修改")
