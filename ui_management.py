"""用例、工具来源、评分规则与本地触发器的受控管理界面。"""
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd
import streamlit as st

from asset_store import digest, editable_cases, restore_cases, save_cases, save_json
from evaluation_support import CATEGORIES, case_threshold, load_rules, validate_case_targets
from safe_output import redact_text
from tools_source import discover_tools, validate_source
from trigger_control import TriggerController
from trigger_run import CONFIG, STATE, validate_event, validate_trigger_config


def notice(text):
    st.session_state["asset_notice"] = text
    st.rerun()


def tools_management(toy):
    source = toy.tools_source
    try:
        validate_source(source, require_credentials=False)
    except ValueError as exc:
        st.error(str(exc))
    st.info("嵌入式工具描述用于通告设备能力和匹配 tools/call。程序模拟设备占位回包，不读取实体电量；天气等服务端工具只评价最终回答。")
    from tools_source import parse_embedded_tools
    from asset_store import atomic_update
    from toy_profiles import list_toy_profiles
    path = toy.mcp_tools_file
    if path.is_file():
        fingerprint = digest(path)
        with st.expander("导入嵌入式工具描述"):
            shared = [t.id for t in list_toy_profiles() if t.mcp_tools_file == path]
            st.caption("当前共享工具文件的玩具：" + "、".join(shared))
            upload = st.file_uploader("上传工具定义（JSON / TXT）", type=["json", "txt"])
            text = st.text_area("或粘贴嵌入式工具定义", height=150)
            raw = upload.getvalue().decode("utf-8-sig") if upload else text
            if st.button("校验并预览工具定义"):
                try:
                    st.json(parse_embedded_tools(raw))
                except ValueError as exc:
                    st.error(str(exc))
            if st.button("保存嵌入式工具描述"):
                try:
                    tools = parse_embedded_tools(raw)
                    def build(original, temporary):
                        temporary.write_text(json.dumps(tools, ensure_ascii=False, indent=2), encoding="utf-8")
                    atomic_update(path, fingerprint, build)
                    notice("工具定义已校验、备份并保存；仅对下一轮生效。")
                except (OSError, ValueError) as exc:
                    st.error(redact_text(str(exc)))
    with st.expander("复用其他玩具的工具描述"):
        profiles = [t for t in list_toy_profiles() if t.mcp_tools_file.is_file()]
        if profiles:
            source_toy = st.selectbox("复用工具来源玩具", profiles, format_func=lambda t: t.name or t.id)
            profile_path = toy.root / "toy.json"
            profile_hash = digest(profile_path)
            if st.button("保存工具复用引用"):
                try:
                    from toy_profiles import PROJECT_ROOT, safe_path
                    safe_path(source_toy.mcp_tools_file, PROJECT_ROOT)
                    value = json.loads(profile_path.read_text(encoding="utf-8"))
                    value["mcp_tools_file"] = str(source_toy.mcp_tools_file)
                    value["tools_source"] = source_toy.tools_source
                    save_json(profile_path, value, profile_hash)
                    notice("已保存工具复用引用；共享修改影响下一轮。")
                except (OSError, ValueError) as exc:
                    st.error(redact_text(str(exc)))
    if source.get("type") == "mcp_http":
        st.info(redact_text(f'MCP Streamable HTTP · {source.get("url", "未配置地址")} · {source.get("protocol_version", "2025-11-25")}'))
        if st.button("从远程 MCP 刷新工具清单", key="discover_remote"):
            try:
                with st.spinner("读取远程工具定义……"):
                    tools, origin = discover_tools(source, toy.mcp_tools_file)
                st.session_state["remote_tools"] = {"toy": toy.id, "source": source, "tools": tools, "origin": origin}
            except Exception as exc:
                st.error(redact_text(str(exc)))
        cached = st.session_state.get("remote_tools", {})
        if cached.get("toy") == toy.id and cached.get("source") == source:
            st.caption(f'最近读取：{cached["origin"]["read_at"]} · {len(cached["tools"])} 个工具 · {cached["origin"]["sha256"][:12]}')
            for tool in cached["tools"]:
                with st.expander(tool["name"]):
                    st.text(tool["description"])
                    st.json(tool["inputSchema"])
        else:
            st.caption("尚未主动获取远程清单。页面加载不会发远程请求；启动评测时会重新发现。")
    with st.expander("高级兼容：配置工具来源"):
        st.caption("修改会备份 toy.json，不保存真实远程鉴权令牌；鉴权只引用环境变量名。")
        path = toy.root / "toy.json"
        fingerprint = digest(path)
        with st.form("tool_source_form"):
            mode = st.selectbox("来源类型", ["local", "mcp_http"], index=int(source.get("type") == "mcp_http"))
            url = st.text_input("MCP 服务地址", value=redact_text(source.get("url", "")), placeholder="填写服务方提供的真实 MCP URL")
            env_name = st.text_input("MCP Bearer Token 环境变量名（可空）", value=source.get("token_env", ""))
            versions = ["2025-11-25", "2026-07-28", "2025-06-18", "2025-03-26"]
            current = source.get("protocol_version", "2025-11-25")
            version = st.selectbox("MCP 协议版本", versions, index=versions.index(current) if current in versions else 0)
            if st.form_submit_button("保存工具来源"):
                try:
                    updated = {"type": mode} if mode == "local" else {"type": mode, "url": url.strip(), "token_env": env_name.strip(), "protocol_version": version}
                    validate_source(updated, require_credentials=False)
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    raw["tools_source"] = updated
                    save_json(path, raw, fingerprint)
                    st.session_state.pop("remote_tools", None)
                    notice("工具来源已保存；下次评测将使用新来源。")
                except (OSError, ValueError) as exc:
                    st.error(redact_text(str(exc)))


def cases_management(toy, busy=False):
    with st.expander("维护测试用例 · 新增 / 修改 / 删除"):
        st.caption("保存前校验必填字段、分类和编号；保存自动备份，若其他人修改过原文件则拒绝覆盖。源行号只用于定位，不能编辑。")
        key = f"case_edit_{toy.id}_{toy.cases_xlsx}"
        if key not in st.session_state:
            st.session_state[key] = {"hash": digest(toy.cases_xlsx), "rows": editable_cases(toy.cases_xlsx)}
        base = st.session_state[key]
        with st.form("case_editor_form"):
            columns = ["源行号", "类型", "用例编号", "消息列表", "预期结果", "是否工具调用", "工具名称", "工具参数文字描述"]
            from config import load_mcp_tools_from_txt
            tool_names = [t["name"] for t in load_mcp_tools_from_txt(toy.mcp_tools_file)]
            frame = st.data_editor(pd.DataFrame(base["rows"], columns=columns), num_rows="dynamic", disabled=["源行号"],
                                   hide_index=True, width="stretch", key=f'edit-grid-{toy.id}-{base["hash"]}',
                                   column_config={"类型": st.column_config.SelectboxColumn(options=[item["name"] for item in __import__("category_store").list_case_types()], required=True),
                                                  "是否工具调用": st.column_config.CheckboxColumn("要求嵌入式工具调用"),
                                                   "工具名称": st.column_config.SelectboxColumn("预期嵌入式工具", options=tool_names)})
            saved = st.form_submit_button("校验并保存用例", disabled=busy)
        if saved:
            try:
                rows = [{k: None if pd.isna(v) else v for k, v in row.items()} for row in frame.to_dict("records")]
                for row in rows:
                    if row.get("源行号") is not None:
                        row["源行号"] = int(row["源行号"])
                from config import DialogueGroup, DialogueTurn
                from tools_source import validate_expected_tools
                checks = [DialogueGroup("editor", str(row.get("类型", "")), [DialogueTurn(
                    int(row.get("源行号") or 2), str(row.get("用例编号", "")), [], str(row.get("预期结果", "")),
                    bool(row.get("是否工具调用")), row.get("工具名称"), row.get("工具参数文字描述"))]) for row in rows]
                if toy.tools_source.get("type") != "mcp_http":
                    validate_expected_tools(checks, load_mcp_tools_from_txt(toy.mcp_tools_file),
                                            {"category_bases": {t["name"]: t["base_category"] for t in __import__("category_store").list_case_types()}})
                backup = save_cases(toy.cases_xlsx, rows, base["hash"])
                st.session_state.pop(key, None)
                for state_key in ["scope", "scope_rows", "scope_mode", "scope_categories", "case_categories", "case_query", "case_table"]:
                    st.session_state.pop(state_key, None)
                notice(f"用例已保存，原文件已备份为 {backup.name}。")
            except (OSError, ValueError) as exc:
                st.error(redact_text(str(exc)))
        if st.button("放弃编辑，重新加载原文件", key="reload_case_editor"):
            st.session_state.pop(key, None)
            notice("已重新加载当前文件。")
        backups = sorted((toy.cases_xlsx.parent / ".backups").glob("*" + toy.cases_xlsx.name), reverse=True)
        if backups:
            selected = st.selectbox("可恢复备份", backups, format_func=lambda p: p.name)
            confirm = st.checkbox("恢复所选备份（当前文件也会先备份）")
            if st.button("恢复用例备份", disabled=not confirm or busy):
                try:
                    restore_cases(toy.cases_xlsx, selected, digest(toy.cases_xlsx))
                    st.session_state.pop(key, None)
                    for state_key in ["scope", "scope_rows", "scope_mode", "scope_categories", "case_categories", "case_query", "case_table"]:
                        st.session_state.pop(state_key, None)
                    notice("用例已从备份恢复。")
                except (OSError, ValueError) as exc:
                    st.error(str(exc))


def rules_management(cfg, groups):
    with st.expander("维护分类与单条用例基准"):
        path = cfg.thresholds_file
        key = "threshold_edit_snapshot"
        if key not in st.session_state:
            st.session_state[key] = {"hash": digest(path), "data": json.loads(path.read_text(encoding="utf-8"))}
        base = st.session_state[key]
        st.caption("优先级：单条指定行 > 单条编号 > 分类配置 > CLI 默认。纯 MCP 规则判定不改成语言评分。保存只影响下一次评测。")
        with st.form("threshold_form"):
            default_map = base["data"].get("thresholds", {})
            st.caption("嵌入式强制调用为规则分 0/1，通过标准固定 1；下列 MCP 分类阈值仅作用于不要求设备调用的文本用例。")
            category_rows = [{"类型": c, "启用分类覆盖": c in default_map, "通过阈值": float(default_map.get(c, cfg.judge_pass_threshold))} for c in cfg.rules_snapshot.get("thresholds", CATEGORIES)]
            categories = st.data_editor(pd.DataFrame(category_rows), hide_index=True, disabled=["类型"],
                                        column_config={"通过阈值": st.column_config.NumberColumn(min_value=0.0, max_value=1.0, step=0.01)},
                                        key="category_threshold_editor-" + base["hash"])
            cases = base["data"].get("case_thresholds", [])
            frame = pd.DataFrame(cases, columns=["toy_id", "dataset_id", "category", "case_id", "sheet_row", "threshold"])
            case_values = st.data_editor(frame, num_rows="dynamic", hide_index=True, key="case_threshold_editor-" + base["hash"],
                                         column_config={"toy_id": st.column_config.TextColumn(required=True),
                                                        "case_id": st.column_config.TextColumn(required=True),
                                                        "sheet_row": st.column_config.NumberColumn(min_value=2, step=1),
                                                        "category": st.column_config.SelectboxColumn(options=[item["name"] for item in __import__("category_store").list_case_types()]),
                                                        "threshold": st.column_config.NumberColumn(min_value=0.0, max_value=1.0, step=0.01)})
            submitted = st.form_submit_button("保存分类与单条阈值")
        if submitted:
            try:
                values = []
                for item in case_values.to_dict("records"):
                    entry = {k: v for k, v in item.items() if not pd.isna(v)}
                    if "sheet_row" in entry:
                        if float(entry["sheet_row"]) != int(entry["sheet_row"]):
                            raise ValueError("行号必须为整数")
                        entry["sheet_row"] = int(entry["sheet_row"])
                    values.append(entry)
                updated = dict(base["data"], thresholds={r["类型"]: float(r["通过阈值"]) for r in categories.to_dict("records") if r["启用分类覆盖"]}, case_thresholds=values)
                def validate(path):
                    rules = load_rules(replace(cfg, thresholds_file=path))
                    # 其它玩具的规则也按其真实数据源校验，避免保存无效编号。
                    from toy_profiles import list_toy_profiles
                    from config import load_dialogue_groups_from_xlsx
                    profiles = {toy.id: toy for toy in list_toy_profiles()}
                    if any(case["toy_id"] not in profiles for case in rules["case_thresholds"]):
                        raise ValueError("单条阈值指定了不存在的玩具")
                    from dataset_store import list_datasets
                    for toy_id in {case["toy_id"] for case in rules["case_thresholds"]}:
                        for entry in [r for r in rules["case_thresholds"] if r["toy_id"] == toy_id]:
                            if entry.get("dataset_id"):
                                available = {d.id: d for d in list_datasets(profiles[toy_id])}
                                if entry["dataset_id"] not in available:
                                    raise ValueError("单条阈值指定的测试集不存在或不属于该玩具")
                                source = available[entry["dataset_id"]].path
                            else:
                                source = profiles[toy_id].cases_xlsx
                            validate_case_targets(dict(rules, case_thresholds=[entry]), toy_id, load_dialogue_groups_from_xlsx(source))
                save_json(path, updated, base["hash"], validator=validate)
                st.session_state.pop(key, None)
                notice("阈值配置已保存，历史结果保持原快照。")
            except (OSError, ValueError, TypeError) as exc:
                st.error(redact_text(str(exc)))
        if st.button("重新加载阈值配置"):
            st.session_state.pop(key, None)
            notice("已重新加载阈值。")


@st.cache_resource
def trigger_controller():
    return TriggerController()


def triggers_management(toys, cfg_factory):
    with st.expander("每日评测与自动事件触发"):
        st.caption("GitLab Schedule 继续使用 daily_run.py。也可显式启动本地触发器；同一部署请选择一种每日调度，避免重复跑测。")
        try:
            config = json.loads(CONFIG.read_text(encoding="utf-8"))
            validate_trigger_config(config)
        except (OSError, ValueError, KeyError) as exc:
            st.error("触发配置不可用：" + redact_text(str(exc)))
            return
        fingerprint = digest(CONFIG)
        with st.form("trigger_config_form"):
            daily = st.checkbox("启用本地每日评测", value=config["daily_enabled"])
            clock = st.text_input("每日时间 HH:MM", value=config["daily_time"])
            chosen = st.multiselect("每日评测玩具", [t.id for t in toys], default=[t for t in config["toy_ids"] if t in {x.id for x in toys}])
            events = st.checkbox("监听文件事件", value=config["watch_events"])
            if st.form_submit_button("保存触发配置"):
                try:
                    updated = dict(config, daily_enabled=daily, daily_time=clock, toy_ids=chosen, watch_events=events)
                    save_json(CONFIG, updated, fingerprint, validator=lambda p: validate_trigger_config(json.loads(p.read_text(encoding="utf-8"))))
                    notice("触发配置已保存。保存不会启动进程或发起评测。")
                except (OSError, ValueError, KeyError) as exc:
                    st.error(str(exc))
        controller = trigger_controller()
        st.caption("本应用启动的触发器：" + ("运行中" if controller.running() else "未运行"))
        st.caption("定时与事件任务使用环境变量 / toy.json，不继承页面临时输入的凭据。")
        if st.button("启动本地触发器", disabled=controller.running()):
            try:
                from ui_request import validate_request
                for toy in toys:
                    if toy.id in config["toy_ids"]:
                        validate_request(cfg_factory(toy))
                controller.start()
                notice("本地触发器已启动。")
            except (OSError, ValueError) as exc:
                st.error(redact_text(str(exc)))
        if st.button("停止本应用启动的触发器", disabled=not controller.running()):
            try:
                controller.stop()
                notice("触发器已停止；历史事件和报告保留。")
            except (OSError, ValueError) as exc:
                st.error(str(exc))
        st.caption("自动事件：将不含凭据的 JSON 放入 .triggers/inbox，或使用下面入口提交。同一 event_id 仅执行一次。")
        event_text = st.text_area("事件 JSON", value='{"event_id":"填写唯一事件编号","toy_id":"default","case_ids":["1"],"notify":false}', key="event_json")
        if st.button("校验并提交事件到收件箱"):
            try:
                event = json.loads(event_text)
                validate_event(event)
                inbox = STATE / "inbox"
                inbox.mkdir(parents=True, exist_ok=True)
                target = inbox / (event["event_id"] + ".json")
                if target.exists():
                    raise ValueError("该事件编号已提交，请换用新编号")
                with target.open("x", encoding="utf-8") as file:
                    json.dump(event, file, ensure_ascii=False)
                st.success("事件已提交；仅在触发器运行时才会执行。")
            except (OSError, ValueError) as exc:
                st.error(redact_text(str(exc)))
        records = []
        for folder in ["events", "daily", "invalid"]:
            for path in sorted((STATE / folder).glob("*.json"), reverse=True)[:20]:
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    records.append({"来源": folder, "记录": path.stem, "状态": item.get("status"), "结束": item.get("finished_at", "—"),
                                    "错误": item.get("error") or item.get("notification_error", "")})
                except (OSError, ValueError):
                    pass
        if records:
            st.dataframe(records, hide_index=True, width="stretch")
