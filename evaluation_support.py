"""共用的范围选择、规则快照与安全模板渲染。"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent
CATEGORIES = ("基础问答", "反向安全", "情感交互", "短期记忆", "长期记忆", "知识库", "MCP调用")


def valid_score(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("评分必须为 0–1 的数值")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError("评分必须为 0–1 的有限数值")
    return number


def load_rules(cfg, root=None) -> dict:
    from category_store import list_case_types, registry_path, case_types_hash

    # Explicit root permits isolated callers; normal runs use the project registry.
    types = list_case_types(root)
    categories = {entry["name"] for entry in types}
    template_path = Path(cfg.judge_template_file)
    threshold_path = Path(cfg.thresholds_file)
    text = template_path.read_text(encoding="utf-8")
    parts = text.split("<!-- rubrics -->")
    if len(parts) != 2:
        raise ValueError("评分模板缺少分类规则区段")
    template, rubrics = parts[0].strip(), json.loads(parts[1])
    required = {"category", "category_rubric", "input_data", "output_schema"}
    identifiers = set(Template(template).get_identifiers())
    if not Template(template).is_valid() or identifiers != required:
        raise ValueError("评分模板变量必须包含 category、category_rubric、input_data、output_schema")
    for category in CATEGORIES:
        if not isinstance(rubrics.get(category), str) or not rubrics[category].strip():
            raise ValueError(f"评分模板缺少分类标准：{category}")
    for entry in types:
        if not entry["builtin"]:
            rubrics[entry["name"]] = entry["rubric"]
    raw = json.loads(threshold_path.read_text(encoding="utf-8"))
    overrides = raw.get("thresholds")
    if not isinstance(overrides, dict) or set(overrides) - categories:
        raise ValueError("分类阈值配置包含未知类别或格式错误")
    default = valid_score(cfg.judge_pass_threshold)
    thresholds = {entry["name"]: valid_score(overrides.get(entry["name"],
                  default if entry["builtin"] else entry["threshold"])) for entry in types}
    cases = raw.get("case_thresholds", [])
    if not isinstance(cases, list):
        raise ValueError("case_thresholds 必须为列表")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not all(isinstance(case.get(k), str) and case[k].strip() for k in ("toy_id", "category", "case_id")):
            raise ValueError("单条阈值必须提供 toy_id、category、case_id")
        if case["category"] not in categories:
            raise ValueError("单条阈值包含未知类别")
        valid_score(case.get("threshold"))
        if "sheet_row" in case and (type(case["sheet_row"]) is not int or case["sheet_row"] < 2):
            raise ValueError("sheet_row 必须为不小于 2 的整数")
        if "dataset_id" in case and (not isinstance(case["dataset_id"], str) or not case["dataset_id"].strip()):
            raise ValueError("dataset_id 必须为非空测试集标识")
        identity = (case["toy_id"], case.get("dataset_id"), case["category"], case["case_id"], case.get("sheet_row"))
        if identity in seen:
            raise ValueError("单条阈值重复")
        seen.add(identity)
    return {"version": "1", "template_path": str(template_path), "template": template,
            "template_sha256": hashlib.sha256(text.encode()).hexdigest(), "rubrics": rubrics,
            "thresholds_path": str(threshold_path), "thresholds": thresholds,
            "overrides": list(overrides), "default_threshold": default, "case_thresholds": cases,
            "category_handlers": {e["name"]: e["handler"] for e in types},
            "category_bases": {e["name"]: e["base_category"] for e in types},
            "case_types": types, "case_types_path": str(registry_path(root)),
            "case_types_sha256": case_types_hash(root), "mcp_rule_threshold": 1}


def case_threshold(rules, toy_id, category, case_id, sheet_row=None, root=None, cfg=None):
    # Preserve legacy (rules, toy_id, category, case_id, row) callers while
    # accepting the dataset-scoped API (dataset, category, case_id, row, root, cfg).
    if not isinstance(rules, dict) or "thresholds" not in rules:
        from dataset_thresholds import case_threshold as dataset_case_threshold
        return dataset_case_threshold(rules, toy_id, category, case_id, root=root, cfg=cfg)
    threshold = rules["thresholds"].get(category, rules["default_threshold"])
    matches = [r for r in rules.get("case_thresholds", []) if r["toy_id"] == toy_id and r["category"] == category
               and r["case_id"] == str(case_id) and ("sheet_row" not in r or r["sheet_row"] == sheet_row)]
    if matches:
        threshold = max(matches, key=lambda r: "sheet_row" in r)["threshold"]
    return threshold


def validate_case_targets(rules, toy_id, groups):
    for case in rules.get("case_thresholds", []):
        if case["toy_id"] != toy_id:
            continue
        matches = [t for g in groups if g.category == case["category"] for t in g.turns
                   if t.case_id == case["case_id"] and ("sheet_row" not in case or t.sheet_row == case["sheet_row"])]
        if not matches:
            raise ValueError(f'单条阈值对应的用例不存在：{case["category"]}/{case["case_id"]}')


def render_prompt(rules: dict, category: str, inputs: dict, schema: str) -> str:
    return Template(rules["template"]).substitute(
        category=category, category_rubric=rules["rubrics"][category],
        input_data=json.dumps(inputs, ensure_ascii=False, indent=2), output_schema=schema,
    )


def filter_groups(groups, categories=(), case_ids=(), rows=()):
    """按原表顺序过滤；使用行号可区分同类型中重复的用例编号。"""
    from dataclasses import replace

    categories, case_ids, rows = set(categories), set(case_ids), set(rows)
    if categories - {g.category for g in groups}:
        raise ValueError("所选类别不在当前用例表中")
    if case_ids - {t.case_id for g in groups for t in g.turns}:
        raise ValueError("所选用例编号不在当前用例表中")
    if rows - {t.sheet_row for g in groups for t in g.turns}:
        raise ValueError("所选用例行号不在当前用例表中")
    selected = []
    for group in groups:
        if categories and group.category not in categories:
            continue
        turns = [t for t in group.turns if (not case_ids or t.case_id in case_ids)
                 and (not rows or t.sheet_row in rows)]
        if turns:
            selected.append(replace(group, turns=turns))
    if not selected:
        raise ValueError("当前评测范围为空，请重新选择")
    return selected
