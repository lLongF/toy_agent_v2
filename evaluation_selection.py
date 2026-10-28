"""多测试集来源、会话边界和能力检查；不改变原 Excel 类型。"""
from dataclasses import replace

from config import DialogueGroup, load_dialogue_groups_from_xlsx
from evaluation_support import filter_groups


def selected_groups(cfg, path=None):
    groups = load_dialogue_groups_from_xlsx(path or cfg.case_snapshot or cfg.cases_xlsx)
    snapshot = cfg.dataset_snapshot
    if snapshot.get("sessions"):
        turns = {t.sheet_row: t for g in groups for t in g.turns}
        groups = [DialogueGroup(group_id=f'{item["dataset_id"]}:{item["category"]}',
                                category=item["category"],
                                turns=[turns[r] for r in item["rows"] if r in turns])
                  for item in snapshot["sessions"]]
    return filter_groups(groups, cfg.selected_categories, cfg.selected_case_ids, cfg.selected_case_rows)


def validate_capabilities(cfg, groups):
    capabilities = cfg.toy_snapshot.get("capabilities", {})
    # 旧档案未声明能力时保持兼容；一旦显式声明则要求所需能力支持。
    if not cfg.toy_snapshot.get("configured", bool(capabilities)):
        return
    required = {"text"}
    for group in groups:
        category = cfg.rules_snapshot.get("category_bases", {}).get(group.category, group.category)
        if category in {"短期记忆", "长期记忆"}:
            required.add("context")
        if category == "知识库":
            required.add("knowledge_base")
        if any(t.expect_tool_call for t in group.turns):
            required.update({"tool_injection", "tool_observation"})
    unsupported = [key for key in sorted(required) if capabilities.get(key) not in (True, "supported")]
    if unsupported:
        raise ValueError("所选用例所需能力不支持或未验证：" + "、".join(unsupported))


def scoped_rules(cfg, rules):
    """把数据集原行规则投影到当次执行行；多集旧规则有歧义时拒绝。"""
    rows = cfg.dataset_snapshot.get("rows", {})
    if not rows:
        return rules
    from config import load_dialogue_groups_from_xlsx
    groups = load_dialogue_groups_from_xlsx(cfg.case_snapshot or cfg.cases_xlsx)
    turns = [(g.category, t) for g in groups for t in g.turns]
    mapped = []
    for rule in rules.get("case_thresholds", []):
        if rule["toy_id"] != cfg.toy_id:
            mapped.append(rule)
            continue
        candidates = [(cat, t, rows.get(str(t.sheet_row), {})) for cat, t in turns
                      if cat == rule["category"] and t.case_id == rule["case_id"]]
        candidates = [(cat, t, origin) for cat, t, origin in candidates
                      if (not rule.get("dataset_id") or origin.get("dataset_id") == rule["dataset_id"])
                      and ("sheet_row" not in rule or origin.get("row") == rule["sheet_row"])]
        if not rule.get("dataset_id") and len({o.get("dataset_id") for _, _, o in candidates}) > 1:
            raise ValueError("多测试集单条阈值有歧义，请指定 dataset_id：" + rule["case_id"])
        # 未选择的测试集规则不影响本轮。
        for _, turn, _ in candidates:
            mapped.append(dict(rule, sheet_row=turn.sheet_row))
    return dict(rules, case_thresholds=mapped)
