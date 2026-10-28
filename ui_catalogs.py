"""玩具与测试集管理页面；网络检查只在明确点击时执行。"""
import asyncio
import json
from dataclasses import replace

import streamlit as st

from asset_store import digest
from dataset_store import create_dataset, list_datasets, load_registry, registry_hash, save_registry
from safe_output import redact_text
from toy_management import create_toy, update_toy, file_hash
from toy_profiles import apply_toy_environment
from ui_styles import page_header

CAPABILITIES = {"text": "文本交互", "context": "会话上下文", "knowledge_base": "知识库适用",
                "tool_injection": "可注入嵌入式工具定义", "tool_observation": "可观察设备工具调用"}


def _dismiss_connect():
    st.session_state["toy_connect_open"] = False


def _validate_reference(value):
    import re
    value = value.strip()
    if value and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("凭据引用应填写环境变量名，例如 TOY_AGENT_TOKEN_YIBAO；不能以数字开头，也不要填写真实 Token。")
    return value


def _dismiss_edit():
    st.session_state.pop("toy_detail_id", None)


@st.dialog("编辑玩具", on_dismiss=_dismiss_edit)
def edit_toy_dialog(toy):
    st.caption("内部标识（只读）：" + toy.id)
    st.caption("填写玩具真实 Token，用于 OTA 激活。本机加密保存，不回显；留空保留已有 Token。")
    fingerprint = file_hash(toy.root / "toy.json")
    with st.form("simple_edit_toy_" + toy.id):
        name = st.text_input("玩具名称", toy.name or toy.id)
        token = st.text_input("玩具 Token", type="password", placeholder="留空保留已有 Token")
        if st.form_submit_button("保存修改", type="primary"):
            try:
                update_toy(toy, expected_hash=fingerprint, name=name.strip(), token=token)
                st.session_state["catalog_notice"] = "玩具配置已更新"
                _dismiss_edit()
                st.rerun()
            except (OSError, ValueError) as exc:
                st.error(redact_text(str(exc)))


@st.dialog("接入玩具", on_dismiss=_dismiss_connect)
def connect_toy_dialog():
    st.caption("默认复用已有用例与工具。填写玩具真实 Token 用于 OTA 激活，Token 仅在本机加密保存。")
    with st.form("new_toy"):
        name = st.text_input("玩具名称")
        token = st.text_input("玩具 Token", type="password", placeholder="输入用于 OTA 激活的 Token")
        if st.form_submit_button("接入并保存", type="primary"):
            try:
                created = create_toy(name.strip(), token=token, capabilities={"text": "supported"})
                st.session_state["catalog_notice"] = "已接入 " + (created.name or created.id)
                st.session_state["toy_connect_open"] = False
                st.rerun()
            except (OSError, ValueError) as exc:
                st.error(redact_text(str(exc)))


def _open_evaluation(identity):
    st.session_state["toy_choice"] = identity
    st.session_state["navigation"] = "评测配置与执行"


def toy_management_page(toy, cfg_factory):
    from toy_profiles import list_toy_profiles, PROJECT_ROOT
    from ui_results import discover_reports
    from html import escape
    profiles = list_toy_profiles()
    header, action = st.columns([5, 1])
    with header:
        page_header("玩具管理", "统一管理不同玩具的 Agent 接入、能力与运行环境。", "Toy registry")
    if action.button("＋ 接入玩具", type="primary", width="stretch"):
        st.session_state["toy_connect_open"] = True
    if st.session_state.get("toy_connect_open"):
        connect_toy_dialog()
    if st.session_state.get("catalog_notice"):
        st.success(st.session_state.pop("catalog_notice"))
    reports = {}
    report_errors = []
    for item in profiles:
        found, errors = discover_reports(PROJECT_ROOT / "reports", item.id)
        reports[item.id] = found[0] if found else None
        report_errors.extend(errors)
    latest = max((r for r in reports.values() if r), key=lambda r: r["path"].stat().st_mtime, default=None)
    capability_count = sum(sum(v in (True, "supported") for v in item.capabilities.values()) if isinstance(item.capabilities, dict)
                           else len(item.capabilities or []) for item in profiles)
    columns = st.columns(4)
    columns[0].metric("接入玩具", len(profiles))
    columns[1].metric("声明支持的能力项", capability_count)
    rate = latest["metrics"]["pass_rate"] if latest else None
    columns[2].metric("最近评测通过率", f"{rate:.1%}" if rate is not None else "—")
    problems = sum(sum(r["metrics"][key] for key in ("failed_count", "judge_error_count", "unevaluated_count")) for r in reports.values() if r)
    columns[3].metric("最近报告关注用例", problems)
    st.caption("指标来自实际档案与各玩具最近报告；能力为配置声明，不代表实测认证。")
    query = st.text_input("搜索玩具名称或标识", key="toy_catalog_search").strip().casefold()
    matching = [item for item in profiles if not query or query in ((item.name or "") + " " + item.id).casefold()]
    grid = st.columns(3)
    for index, item in enumerate(matching):
        report = reports.get(item.id)
        with grid[index % 3].container(border=True):
            st.markdown(f"### 🧸 {escape(item.name or item.id)}")
            st.caption("已启用" if item.enabled else "已禁用")
            st.markdown("**运行环境**")
            st.caption("、".join(item.environments) or "默认环境")
            st.markdown("**玩具 Token**")
            st.caption("已本机加密保存" if item.credential_ref.startswith("local:") else ("已配置环境变量引用" if item.credential_ref else "兼容环境变量 / 本次临时输入"))
            st.markdown("**最近评测**")
            st.caption(str(report["protocol"].get("started_at", "时间无记录"))[:19].replace("T", " ") if report else "尚未运行")
            caps = item.capabilities or {}
            supported = [CAPABILITIES.get(key, key) for key, value in caps.items() if value in (True, "supported")] if isinstance(caps, dict) else caps
            st.caption(" · ".join(supported) or "能力未声明")
            st.divider()
            left, right = st.columns(2)
            if left.button("配置详情 →", key="toy_detail_" + item.id):
                st.session_state["toy_detail_id"] = item.id
            right.button("发起评测", key="toy_run_" + item.id, disabled=not item.enabled,
                         on_click=_open_evaluation, args=(item.id,))
    if not matching:
        st.info("没有匹配的玩具。")
    st.info("接入流程：填写名称与玩具 Token → 默认复用已有用例与工具 → 选择测试集发起评测。Token 不在页面回显。")
    identity = st.session_state.get("toy_detail_id")
    selected = next((item for item in profiles if item.id == identity), None)
    if selected and not st.session_state.get("toy_connect_open"):
        edit_toy_dialog(selected)
    if report_errors:
        with st.expander("部分历史报告不可读取"):
            st.caption("；".join(dict.fromkeys(report_errors)))


def _toy_detail_editor(toy, cfg_factory):
    path = toy.root / "toy.json"
    fingerprint = file_hash(path)
    st.subheader(toy.name or toy.id)
    st.caption("稳定 ID：" + toy.id)
    with st.form("edit_toy_" + toy.id):
        name = st.text_input("显示名称", toy.name or toy.id)
        enabled = st.checkbox("启用玩具", value=toy.enabled)
        ref = st.text_input("凭据引用环境变量名", toy.credential_ref)
        st.caption("旧档案未声明能力时保持兼容；保存后按所选状态执行能力检查。长期记忆不代表已验证跨会话持久记忆。")
        caps = toy.capabilities or {}
        if isinstance(caps, list):
            caps = {key: "supported" for key in caps}
        states = ["unverified", "supported", "unsupported"]
        selected = {}
        for key, label in CAPABILITIES.items():
            current = caps.get(key, "unverified")
            if isinstance(current, bool):
                current = "supported" if current else "unsupported"
            selected[key] = st.selectbox(label, states, index=states.index(current) if current in states else 0,
                                         key="cap_" + key)
        st.caption('环境 JSON：环境名对应 ota_url、ws_url、device_mac、client_id、device_name、board_ip、board_type、firmware_version、credential_ref、enabled。只保存引用，不填写真实凭据。')
        environments = st.text_area("接口与环境配置 JSON", json.dumps(toy.environments, ensure_ascii=False, indent=2), height=220)
        if st.form_submit_button("保存玩具配置"):
            try:
                update_toy(toy, expected_hash=fingerprint, name=name, enabled=enabled,
                           credential_ref=ref, capabilities=selected, environments=json.loads(environments))
                st.success("配置已保存并备份，下一轮生效。刷新页面以重新加载。")
            except (OSError, ValueError, TypeError) as exc:
                st.error(redact_text(str(exc)))
    environment = st.selectbox("检查环境", [""] + list(toy.environments), format_func=lambda x: x or "默认环境")
    if st.button("校验接口与凭据引用（不联网）"):
        try:
            cfg = apply_toy_environment(cfg_factory(toy), toy, environment)
            if not cfg.token:
                raise ValueError("缺少玩具凭据")
            st.success("配置与凭据引用可用；尚未验证网络或能力。")
        except (OSError, ValueError) as exc:
            st.error(redact_text(str(exc)))
    st.caption("连接检查会联系当前环境的 OTA 服务并握手，不发送用户用例，不调用裁判。")
    if st.button("检查连接（明确联网）"):
        try:
            from collection import MessageRouter, TransportClient
            from config import RuntimeState, load_mcp_tools_from_txt
            from execution_lock import device_lock
            cfg = apply_toy_environment(cfg_factory(toy), toy, environment)
            if not cfg.token:
                raise ValueError("缺少玩具凭据")
            async def check():
                state = RuntimeState(mcp_tools=load_mcp_tools_from_txt(cfg.mcp_tools_file))
                transport = TransportClient(cfg, state, MessageRouter(state))
                with device_lock(cfg.device_mac):
                    try:
                        result = await transport.send_ota_request()
                        url, token = transport.extract_websocket_from_ota(result.get("data"))
                        if not result.get("ok") or not url:
                            raise ValueError("OTA 检查失败（不展示原始响应凭据）")
                        if not await transport.connect_websocket(url, token or cfg.token):
                            raise ValueError("WebSocket 连接失败")
                        if not await transport.send_hello_message():
                            raise ValueError("hello 握手失败")
                    finally:
                        if state.websocket:
                            await state.websocket.close()
            with st.spinner("检查设备协议连接……"):
                asyncio.run(check())
            st.success("OTA、WebSocket 与 hello 检查通过；未自动验证业务能力。")
        except Exception as exc:
            st.error(redact_text(str(exc)))


def dataset_management(toy, busy=False):
    st.subheader("公共与专属测试集")
    entries = list_datasets(toy)
    for item in entries:
        st.caption(f'{item.name} · {"公共" if item.scope == "public" else "专属 " + item.owner_toy_id} · {"启用" if item.enabled else "禁用"} · {item.id}')
    with st.expander("新建或复制测试集"):
        fingerprint = registry_hash()
        with st.form("create_dataset"):
            name = st.text_input("测试集名称")
            scope = st.selectbox("测试集归属", ["public", "toy"], format_func=lambda x: "公共" if x == "public" else "当前玩具专属")
            sources = {d.id: d for d in entries}
            source = st.selectbox("复制来源", [""] + list(sources), format_func=lambda x: sources[x].name if x else "新建空测试集")
            if st.form_submit_button("创建测试集", disabled=busy):
                try:
                    create_dataset(name, scope, toy.id if scope == "toy" else "",
                                   sources[source].path if source else None, expected_hash=fingerprint)
                    st.success("测试集已创建。刷新后可维护用例和选择执行；空测试集不能执行。")
                except (OSError, ValueError) as exc:
                    st.error(redact_text(str(exc)))
    registered = [d for d in entries if not d.id.startswith("legacy-")]
    if registered:
        with st.expander("启用或禁用测试集"):
            target = st.selectbox("登记测试集", registered, format_func=lambda d: d.name)
            enabled = st.checkbox("测试集启用", value=target.enabled, key="dataset_enabled_" + target.id)
            fingerprint = registry_hash()
            if st.button("保存测试集状态", disabled=busy):
                try:
                    save_registry([replace(d, enabled=enabled) if d.id == target.id else d for d in load_registry()], fingerprint)
                    st.success("状态已保存，下次执行生效。")
                except (OSError, ValueError) as exc:
                    st.error(redact_text(str(exc)))
    st.caption("公共测试集修改会影响复用它的玩具下一轮评测；本次运行使用独立快照。复制出的测试集是独立副本。")
    if entries:
        chosen = st.selectbox("维护测试集", entries, format_func=lambda d: d.name + (" · 公共" if d.scope == "public" else " · 专属"))
        return replace(toy, cases_xlsx=chosen.path), chosen
    return toy, None
