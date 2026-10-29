"""界面与任务进程共用的启动前检查。"""
from urllib.parse import urlparse
import tempfile

from config import load_dialogue_groups_from_xlsx, load_mcp_tools_from_txt
from evaluation_support import CATEGORIES, load_rules, validate_case_targets
from evaluation_selection import selected_groups, validate_capabilities, scoped_rules
from tools_source import validate_source, validate_expected_tools
from asset_store import case_issues


def validate_request(cfg):
    if cfg.selected_dataset_ids and not cfg.case_snapshot:
        from dataset_store import prepare_dataset_snapshot
        with tempfile.TemporaryDirectory(prefix="toy-precheck-") as directory:
            return validate_request(prepare_dataset_snapshot(cfg, directory))
    path = cfg.case_snapshot or cfg.cases_xlsx
    issues = case_issues(path)
    if issues:
        # 合并测试集允许跨集同编号，但每个源文件必须独立校验。
        issues = [i for i in issues if not (cfg.dataset_snapshot and "编号重复" in i)]
        if issues:
            raise ValueError("用例数据需要修复：" + "；".join(issues[:3]))
    groups = selected_groups(cfg, path)
    rules = scoped_rules(cfg, cfg.rules_snapshot or load_rules(cfg))
    from category_store import list_case_types
    allowed_categories = set(rules.get("rubrics", {})) | {item["name"] for item in list_case_types() if item["builtin"]}
    if any(g.category not in allowed_categories for g in groups):
        raise ValueError("所选范围包含当前引擎不支持的类别，请检查用例表")
    if any(not t.user_messages or not t.expected_outcome for g in groups for t in g.turns):
        raise ValueError("所选用例有空消息或缺少预期结果，请检查 Excel")
    from dataclasses import replace
    from connection_scenes import validate_scene_cases
    validate_scene_cases(cfg, groups, rules)
    validate_capabilities(replace(cfg, rules_snapshot=rules), groups)
    tools = load_mcp_tools_from_txt(cfg.mcp_tools_file)
    validate_source(cfg.tools_source)
    if cfg.tools_source.get("type") != "mcp_http":
        validate_expected_tools(groups, tools, rules)
    if cfg.connection_scene == "company" and not cfg.token.strip():
        raise ValueError("缺少玩具 Token：填写凭据或配置凭据引用环境变量")
    needs_judge = any(rules.get("category_bases", {}).get(g.category, g.category) != "MCP调用" or any(not t.expect_tool_call for t in g.turns) for g in groups)
    if needs_judge:
        from judge import build_judge_endpoints
        endpoints = build_judge_endpoints(cfg)
        if not endpoints:
            raise ValueError("文本评分至少选择一个裁判模型")
        if any(not endpoint.api_key.strip() for endpoint in endpoints):
            raise ValueError("缺少所选裁判凭据，请配置 API Key 或环境变量引用")
    if not cfg.device_mac.strip() or (needs_judge and any(not endpoint.model.strip() for endpoint in endpoints)):
        raise ValueError("设备 MAC 和启用的裁判模型不能为空")
    from connection_scenes import validate_connection_mode
    validate_connection_mode(cfg)
    if cfg.connection_mode == "ota" and (urlparse(cfg.ota_url).scheme not in {"http", "https"} or not urlparse(cfg.ota_url).hostname):
        raise ValueError("OTA 地址必须为有效的 HTTP(S) URL")
    if cfg.response_collect_timeout <= 0:
        raise ValueError("回复采集超时必须大于 0")
    rules = scoped_rules(cfg, cfg.rules_snapshot or load_rules(cfg))
    validate_case_targets(rules, cfg.toy_id, load_dialogue_groups_from_xlsx(path))
    return groups, rules
