"""轻量用例类型和裁判模型配置界面。"""
import json
import streamlit as st
from safe_output import redact_text


def type_management():
    from category_store import list_case_types, create_case_type, update_case_type, case_types_hash
    items = list_case_types()
    with st.expander("用例类型管理 · 自定义名称映射已有执行方式"):
        st.caption("自定义类型复用既有采集方式，配置独立评分标准；不执行自定义代码。已有七类保留，不提供删除。")
        st.dataframe([{"类型": t["name"], "执行方式": t["base_category"], "默认阈值": t["threshold"],
                       "来源": "内置" if t["builtin"] else "自定义"} for t in items], hide_index=True, width="stretch")
        custom = {t["name"]: t for t in items if not t["builtin"]}
        selected = st.selectbox("编辑类型", [""] + list(custom), format_func=lambda x: x or "新增类型")
        current = custom.get(selected, {})
        key = "type_" + (selected or "new")
        fingerprint = case_types_hash()
        with st.form(key):
            name = st.text_input("类型名称", current.get("name", ""), disabled=bool(selected))
            bases = [t["name"] for t in items if t["builtin"]]
            base = st.selectbox("基础执行方式", bases, index=bases.index(current.get("base_category", "基础问答")))
            rubric = st.text_area("分类评分标准", current.get("rubric", ""))
            threshold = st.number_input("默认文本通过阈值", min_value=0.0, max_value=1.0,
                                        value=float(current.get("threshold", .7)), step=.01)
            st.caption("嵌入式强制调用仍使用程序规则分0/1、通过标准1；此阈值不覆盖工具硬规则。")
            if st.form_submit_button("保存用例类型"):
                try:
                    if selected:
                        update_case_type(selected, expected_hash=fingerprint, base_category=base, rubric=rubric, threshold=threshold)
                    else:
                        create_case_type(name, base, rubric, threshold, expected_hash=fingerprint)
                    st.success("类型已保存；重新进入用例维护区可选择，下轮评测生效。")
                except (OSError, ValueError) as exc:
                    st.error(redact_text(str(exc)))


def judge_management():
    from judge_store import list_judge_models, create_judge_model, update_judge_model, registry_hash
    items = list_judge_models()
    with st.expander("裁判模型管理 · 新增 / 编辑 / 禁用"):
        st.caption("只支持 OpenAI Chat Completions 兼容协议。保存不调用模型；Key 本机加密保存，留空则使用环境变量引用。")
        options = {t["id"]: t for t in items}
        selected = st.selectbox("编辑裁判模型", [""] + list(options), format_func=lambda x: options[x]["name"] if x else "新增裁判")
        current = options.get(selected, {})
        fingerprint = registry_hash()
        with st.form("judge_edit_" + (selected or "new")):
            name = st.text_input("裁判名称", current.get("name", ""))
            model = st.text_input("模型 ID", current.get("model", ""))
            url = st.text_input("兼容接口 Base URL", current.get("base_url", ""), placeholder="https://service.example/v1")
            reference = current.get("credential_ref", "")
            ref = st.text_input("API Key 环境变量引用（可选）", "" if reference.startswith("local:") else reference)
            token = st.text_input("API Key（本机加密保存）", type="password", help="编辑时不回显旧Key，留空保留。")
            enabled = st.checkbox("启用裁判", current.get("enabled", True))
            extra = st.text_area("模型兼容参数 JSON", json.dumps(current.get("extra_body", {}), ensure_ascii=False),
                                 help="仅支持受控 thinking / enable_thinking / reasoning_effort 参数")
            if st.form_submit_button("保存裁判模型"):
                try:
                    kwargs = dict(name=name, model=model, base_url=url, enabled=enabled, extra_body=json.loads(extra), expected_hash=fingerprint)
                    if selected:
                        if token.strip():
                            kwargs["token"] = token
                        elif ref.strip() or not reference.startswith("local:"):
                            kwargs["credential_ref"] = ref.strip()
                        update_judge_model(selected, **kwargs)
                    else:
                        create_judge_model(credential_ref=ref.strip(), token=token or None, **kwargs)
                    st.success("模型配置已保存并备份；重新进入执行页选择，下轮生效。")
                except (OSError, ValueError, TypeError) as exc:
                    st.error(redact_text(str(exc)))


def configure_judges(cfg):
    """固定运行端点与凭据；返回已填cfg，密钥只在内存/stdin中传递。"""
    from judge_store import snapshot_judge_models
    from judge import build_judge_endpoints
    if cfg.selected_judge_ids is not None:
        cfg.judge_endpoints_snapshot = snapshot_judge_models(cfg.selected_judge_ids, cfg=cfg)
        endpoints = build_judge_endpoints(cfg)
        cfg.judge_credentials = {endpoint.name: endpoint.api_key for endpoint in endpoints}
    return cfg
