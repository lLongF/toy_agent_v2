"""Agent 评测工作台与玩具实时对话，复用现有执行引擎。"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
from dataclasses import replace
from datetime import datetime
from html import escape
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from cli import apply_fast_mode
from config import Config, DEFAULT_DEVICE_MAC, load_dialogue_groups_from_xlsx, load_mcp_tools_from_txt
from evaluation_support import filter_groups, load_rules, case_threshold
from safe_output import redact_text, redact_data
from toy_profiles import list_toy_profiles
from ui_jobs import JobManager, ROOT
from ui_request import validate_request
from ui_results import attachments, confined, discover_reports, read_report
from ui_styles import apply_styles, empty_state, metric_cards, page_header
from ui_management import cases_management, tools_management, rules_management, triggers_management
from asset_store import case_issues

LIVE_CHAT_PAGE = "玩具实时对话"
RELEASE_PAGE = "发版自测"
PAGES = ["评测总览", "用例与工具管理", "评测配置与执行", "结果分析", "玩具管理", LIVE_CHAT_PAGE, RELEASE_PAGE]
LIVE_CHAT_URLS = {
    "曦仔": "https://lycheeai-muxi-test.pages.dev/?mac=02:00:00:00:00:0b&productName=%E6%9B%A6%E4%BB%94",
    "粤悦": "https://lycheeai-muxi-test.pages.dev/?mac=02:00:00:00:00:0c&productName=%E5%B9%BF%E7%94%B5-%E7%B2%A4%E6%82%A6",
    "仪宝": "https://lycheeai-muxi-test.pages.dev/?mac=02:00:00:00:00:0f&productName=%E4%BB%AA%E7%94%B5-%E4%BB%AA%E5%AE%9D",
    "闹闹": "https://lycheeai-muxi-test.pages.dev/?mac=02:00:00:00:00:13&productName=%E8%8F%B2%E8%8F%B2%E7%89%B9-%E9%97%B9%E9%97%B9",
    "小智": "local:xiaozhi",
}
COLORS = {"通过": "#5b6dea", "不通过": "#ec7c74", "评分失败": "#efbd65", "未执行": "#b9c3d5"}
st.set_page_config(page_title="Agent 评测工作台", page_icon=":material/experiment:", layout="wide")


@st.cache_resource
def job_manager(implementation_version=2):
    return JobManager()


def navigate(page, report=None):
    st.session_state["navigation"] = page
    if report is not None:
        st.session_state["report_target"] = str(report)


def open_release_report(path):
    report = read_report(Path(path), ROOT / "reports")
    st.session_state["release_report_pending"] = {
        "toy_id": report["protocol"]["toy_id"], "path": str(path)}


def base_config(toy):
    return Config(ota_url="https://voiceagent.lycheeai.com.cn/iot-server/ota/",
                  ws_url="wss://voiceAgent.lycheeai.com.cn/voiceAgent/stream/v1/",
                  device_mac=DEFAULT_DEVICE_MAC, client_id="web_test_client", device_name="text-test-device",
                  token="", tools_source=toy.tools_source, cases_xlsx=toy.cases_xlsx, mcp_tools_file=toy.mcp_tools_file,
                  reports_dir=ROOT / "reports" / toy.id, recv_timeout=3.0, send_interval=0.2,
                  response_collect_timeout=45.0, judge_model="qwen-plus", judge_pass_threshold=0.7,
                  api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
                  deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
                  board_ip="192.168.1.1", board_type="xiaozhi-web-test", toy_id=toy.id, trigger_type="manual_ui")


def short_path(path):
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return path.name


def source_info(path):
    if not path.is_file():
        return f"{short_path(path)} · 文件缺失"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return f"{short_path(path)} · SHA256 {digest}"


def page_threshold(cfg, toy, group, turn):
    """Resolve the threshold shown before a run; dataset sidecar wins over legacy rules."""
    from dataset_thresholds import snapshot_threshold
    sidecar = snapshot_threshold(cfg.dataset_snapshot, group.category, turn.case_id, turn.sheet_row, cfg=cfg)
    if sidecar is not None:
        return sidecar
    return case_threshold(cfg.rules_snapshot, toy.id, group.category, turn.case_id, turn.sheet_row)


def safe_groups(toy):
    try:
        return load_dialogue_groups_from_xlsx(toy.cases_xlsx)
    except (OSError, ValueError, KeyError) as exc:
        st.error(f"用例读取失败：{redact_text(str(exc))}")
        return []


def reports_for(toy):
    reports, errors = discover_reports(ROOT / "reports", toy.id)
    if errors:
        with st.expander(f"{len(errors)} 份报告无法读取"):
            for error in errors:
                st.warning(error)
    return reports


def report_label(report):
    p = report["protocol"]
    stamp = p.get("started_at") or report.get("job", {}).get("started_at") or "时间无记录"
    scene = "小智 AI" if p.get("connection_scene") == "xiaozhi" else "公司玩具"
    return f'{stamp[:19].replace("T", " ")} · {scene} · {report["path"].name}'


def report_meta(report):
    p, job = report["protocol"], report.get("job", {})
    trigger = {"manual_ui": "Web 手动", "manual_cli": "CLI 手动", "scheduled": "定时任务", "event": "事件触发"}.get(p.get("trigger_type"), "触发方式无记录")
    state = {"completed": "执行完成", "failed": "执行失败", "running": "运行中"}.get(job.get("status"), "状态无记录")
    if p.get("run_aborted"):
        state = "执行中断"
    scene_label = "小智 AI" if p.get("connection_scene") == "xiaozhi" else "公司玩具"
    return f'{scene_label} / {p.get("toy_id", "玩具无记录")}  /  {trigger}  /  {state}  /  运行 ID：{p.get("run_id") or job.get("job_id") or "无记录"}'


def category_table(report):
    rows = []
    for metric in report["category_metrics"]:
        relevant = [r for r in report["rows"] if r["category"] == metric["category"]]
        thresholds = sorted({r["threshold"] for r in relevant if r["threshold"] is not None})
        rows.append({"类型": metric["category"], "用例": metric["case_count"], "可判定": metric["evaluated_count"],
                     "通过": metric["passed_count"], "业务失败": metric["failed_count"], "评分失败": metric["judge_error_count"],
                     "未执行": metric["unevaluated_count"],
                     "通过率": f'{metric["pass_rate"]:.1%}' if metric["pass_rate"] is not None else "—",
                     "平均分": f'{metric["average_score"]:.3f}' if metric["average_score"] is not None else "—",
                     "阈值": ", ".join(f"{v:g}" for v in thresholds) if thresholds else "规则判定 / 无记录"})
    st.dataframe(rows, hide_index=True, width="stretch")


def detail_panel(row):
    st.markdown(f'### 用例 {row["case_id"]} · {row["category"]}')
    st.caption(f'源表行 {row["row"]} · {row["status"]}')
    a, b = st.columns(2)
    a.metric("本条综合分", f'{row["score"]:.3f}' if row["score"] is not None else "—")
    b.metric("本条通过阈值", f'{row["threshold"]:g}' if row["threshold"] is not None else "规则判定 / 无记录")
    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("**用户输入**")
        st.text("\n".join(str(m) for m in row["messages"]) or "无记录")
        st.markdown("**预期结果**")
        st.text(row["expected"] or "无记录")
    with b.container(border=True):
        st.markdown("**实际回复**")
        st.text(redact_text(row["actual"]) or "无可见回复记录")
        st.markdown("**最终判定依据**")
        st.text(redact_text(row["reason"]) or "规则判定 / 无记录")
    if row.get("source"):
        source = row["source"]
        st.caption(f'来源：{source.get("dataset_name")} / {source.get("sheet")} / 原行 {source.get("row")}')
    if row["rule_only"]:
        st.info(f'本条使用嵌入式工具规则，规则得分 {row.get("rule_score") if row.get("rule_score") is not None else "无记录"}；通过标准固定 1，不计入语言平均分。模拟设备占位回包，不验证真实硬件电量。')
    for judge in row["judges"]:
        with st.expander(f'{judge.get("judge_name") or judge.get("model") or "裁判"} · 分数 {judge.get("score") if judge.get("judge_available") else "不可用"}'):
            st.text(redact_text(judge.get("reason", "无记录")))
    calls, checks = [], []
    for utterance in row["utterances"]:
        calls.extend(utterance.get("mcp_calls") or [])
        check = utterance.get("mcp_check") or {}
        calls.extend(check.get("mcp_calls") or [])
        if check or utterance.get("mcp_required"):
            checks.append({k: (check or utterance).get(k) for k in ["required", "pass", "reason", "expected_tool", "mcp_reply_confirmed"]})
    with st.expander(f"MCP 调用与规则详情 · {len(calls)} 次调用"):
        if calls or checks:
            st.json(redact_data({"calls": calls, "checks": checks}))
        else:
            st.caption("此报告没有记录工具调用详情。")


def show_report(report):
    metric_cards(report["metrics"])
    st.caption(report_meta(report))
    if report["protocol"].get("run_aborted"):
        st.error("本次执行中断，未执行用例已单列。请查看任务日志。")
    if not report["rows"]:
        empty_state("这份报告尚无用例结果", "可以下载现有文件诊断，或返回执行页检查任务状态。")
    overview, details, files = st.tabs(["分类表现", "用例明细与诊断", "报告与配置快照"])
    with overview:
        with st.container(border=True):
            st.subheader("分类质量概览")
            category_table(report)
        if report.get("dataset_metrics"):
            st.subheader("测试集表现")
            st.dataframe([{"测试集": d["dataset_name"], "用例": d["case_count"], "通过": d["passed_count"],
                           "业务失败": d["failed_count"], "评分失败": d["judge_error_count"], "未执行": d["unevaluated_count"]}
                          for d in report["dataset_metrics"]], hide_index=True, width="stretch")
        if report["rows"]:
            chart_rows = [{"类型": r["category"], "状态": r["status"]} for r in report["rows"]]
            counts = pd.DataFrame(chart_rows).groupby(["类型", "状态"]).size().reset_index(name="用例数")
            chart = px.bar(counts, x="类型", y="用例数", color="状态", barmode="stack", color_discrete_map=COLORS)
            chart.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                                margin=dict(l=0, r=0, t=16, b=0), font=dict(color="#6d7890"), legend=dict(orientation="h", y=1.15))
            st.plotly_chart(chart, width="stretch", key=f'chart-{report["path"]}')
    with details:
        a, b, c = st.columns([1, 1, 1.5])
        category = a.multiselect("用例类型", list(dict.fromkeys(r["category"] for r in report["rows"])), key="result_categories")
        status = b.multiselect("结果状态", ["通过", "不通过", "评分失败", "未执行"], key="result_statuses")
        query = c.text_input("搜索用例编号 / 输入 / 回复", key="result_query")
        dataset_options = {d["dataset_id"]: d["dataset_name"] for d in report.get("dataset_metrics", [])}
        datasets = st.multiselect("测试集筛选", list(dataset_options), format_func=dataset_options.get) if dataset_options else []
        rows = [r for r in report["rows"] if (not datasets or r.get("source", {}).get("dataset_id") in datasets) and (not category or r["category"] in category) and (not status or r["status"] in status)
                and (not query or query.casefold() in (r["case_id"] + " ".join(r["messages"]) + r["actual"] + r["reason"]).casefold())]
        st.caption(f'显示 {len(rows)} / {len(report["rows"])} 条用例')
        table = [{"用例": r["case_id"], "类型": r["category"], "结果": r["status"], "综合分": r["score"], "阈值": r["threshold"],
                  "预期结果": r["expected"][:70], "实际回复": r["actual"][:80], "原因": r["reason"][:80]} for r in rows]
        if rows:
            st.dataframe(table, hide_index=True, width="stretch")
            index = st.selectbox("查看单条诊断", range(len(rows)), format_func=lambda i: f'{rows[i]["case_id"]} · {rows[i]["category"]} · 第 {rows[i]["row"]} 行', key="result_detail")
            detail_panel(rows[index])
        else:
            empty_state("没有匹配的用例", "调整类别、结果或关键词后重试。", "⌕")
    with files:
        st.subheader("下载本次报告")
        available = attachments(report, ROOT / "reports")
        for file in available:
            content = file.read_bytes()
            # 旧 JSON trace 可能含凭据；下载前清理凭据字段，保留实际评测数据。
            if file.suffix == ".json":
                content = json.dumps(redact_data(json.loads(content)), ensure_ascii=False, indent=2).encode()
            st.download_button(f"{file.suffix[1:].upper()} · {file.name}", content, file_name=file.name, key=str(file))
        st.caption("只提供当前运行已存在的文件。JSON 凭据字段会脱敏。")
        st.json({"接入场景": report["protocol"].get("connection_scene", "company"),
                 "测评对象": report["protocol"].get("target_name", report["protocol"].get("toy_id")),
                 "Device-Id": report["protocol"].get("device_mac"),
                 "Client-Id": report["protocol"].get("client_id"),
                 "OTA": report["protocol"].get("ota_url"), "WebSocket": report["protocol"].get("ws_url")})
        snapshot = report["protocol"].get("evaluation_rules")
        if snapshot:
            st.json({k: snapshot.get(k) for k in ["version", "template_sha256", "thresholds", "overrides", "case_thresholds"]})
        else:
            st.info("这份历史报告没有模板或阈值快照，不以当前规则补填。")
        st.caption("用例范围")
        st.json(report["protocol"].get("selection") or {"说明": "此报告没有记录范围快照"})


def dashboard(toy, groups):
    page_header("评测总览", "从最近一次真实评测，了解 Agent 的能力与需要关注的问题。", "Evaluation overview")
    a, b, _ = st.columns([1, 1, 4])
    a.button("＋ 新建评测", type="primary", on_click=navigate, args=(PAGES[2],), width="stretch")
    b.button("刷新数据", width="stretch")
    reports = reports_for(toy)
    if not reports:
        empty_state("让每一次对话，都有可衡量的质量", "暂无评测记录。选择用例范围，发起第一次评测；完成后，这里会展示真实通过率、分类表现和待关注的问题。")
        st.subheader("当前评测资产")
        a, b, c = st.columns(3)
        a.metric("可用测试用例", sum(len(g.turns) for g in groups))
        b.metric("覆盖能力类型", len(groups))
        if toy.tools_source.get("type") == "mcp_http":
            cached = st.session_state.get("remote_tools", {})
            c.metric("远程 MCP 工具", len(cached["tools"]) if cached.get("toy") == toy.id and cached.get("source") == toy.tools_source else "—")
        else:
            c.metric("本地 MCP 工具", len(load_mcp_tools_from_txt(toy.mcp_tools_file)))
        with st.container(border=True):
            st.markdown("### 从用例到结论，四步完成")
            steps = st.columns(4)
            for column, title, note in zip(steps, ["01 选择玩具", "02 确定范围", "03 启动评测", "04 分析结果"],
                                            ["沿用当前设备配置", "检查工具与预期结果", "采集与双裁判独立执行", "下钻诊断并下载报告"]):
                column.markdown(f"**{title}**")
                column.caption(note)
        return
    latest = reports[0]
    st.markdown("### 最近一次评测")
    st.caption(report_label(latest))
    metric_cards(latest["metrics"])
    left, right = st.columns([2.1, 1])
    with left.container(border=True):
        st.subheader("分类表现")
        category_table(latest)
    with right.container(border=True):
        st.subheader("需要关注")
        issues = [r for r in latest["rows"] if r["status"] in {"不通过", "评分失败", "未执行"}]
        if not issues:
            st.success("本次已记录用例全部通过。")
        for row in issues[:4]:
            st.markdown(f'**{row["category"]} · {row["case_id"]}**')
            st.caption(f'{row["status"]} · {row["reason"][:80] or "查看结果了解详情"}')
        st.button("查看完整结果 →", on_click=navigate, args=(PAGES[3], latest["path"]), width="stretch")
    with st.container(border=True):
        st.subheader("最近评测记录")
        for i, report in enumerate(reports[:6]):
            a, b, c = st.columns([4, 2, 1])
            a.caption(report_label(report))
            m = report["metrics"]
            b.write(f'{m["case_count"]} 条 · {m["pass_rate"]:.1%}' if m["pass_rate"] is not None else f'{m["case_count"]} 条 · 无有效结果')
            c.button("查看结果", key=f"recent-{i}", on_click=navigate, args=(PAGES[3], report["path"]))


def case_studio(toy, groups):
    page_header("测试集与工具管理", "公共用例可共享，专属用例按玩具隔离；工具清单仅用于嵌入式调用验证。", "Test assets")
    from ui_catalogs import dataset_management
    from ui_scoring import type_management
    type_management()
    toy, dataset = dataset_management(toy, busy=job_manager().busy(DEFAULT_DEVICE_MAC))
    groups = safe_groups(toy)
    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("**测试用例 · Excel**")
        st.caption(source_info(toy.cases_xlsx))
    with b.container(border=True):
        st.markdown("**工具描述 · " + ("远程 MCP" if toy.tools_source.get("type") == "mcp_http" else "本地配置文件") + "**")
        st.caption(redact_text(toy.tools_source.get("url") or source_info(toy.mcp_tools_file)))
    cases_tab, tools_tab = st.tabs([f"测试用例 · {sum(len(g.turns) for g in groups)}", "MCP 工具清单"])
    with cases_tab:
        try:
            issues = case_issues(toy.cases_xlsx)
            if issues:
                st.error("原表有需要修复的行，下面维护区保留这些行：" + "；".join(issues[:5]))
        except (OSError, ValueError) as exc:
            st.error(redact_text(str(exc)))
        a, b, c = st.columns([1.2, 2, 0.7])
        categories = a.multiselect("用例类型", [g.category for g in groups], key="case_categories")
        query = b.text_input("搜索用例编号 / 消息 / 预期结果", key="case_query")
        c.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        c.button("重置筛选", on_click=lambda: [st.session_state.pop(k, None) for k in ["case_categories", "case_query"]])
        all_rows = [(g.category, t) for g in groups for t in g.turns]
        rows = [(cat, t) for cat, t in all_rows if (not categories or cat in categories) and
                (not query or query.casefold() in (t.case_id + " ".join(t.user_messages) + t.expected_outcome).casefold())]
        st.caption(f"显示 {len(rows)} / {len(all_rows)} 条 · 原始 Excel 只读")
        if rows:
            table = [{"行号": t.sheet_row, "用例": t.case_id, "类型": cat, "用户输入": " / ".join(t.user_messages),
                      "预期结果": t.expected_outcome, "必须调用工具": bool(t.expect_tool_call), "工具名称": t.tool_name or "—"} for cat, t in rows]
            selection = st.dataframe(table, width="stretch", hide_index=True, on_select="rerun", selection_mode="multi-row", key="case_table")
            selected_indices = [i for i in selection.selection.rows if 0 <= i < len(rows)]
            with st.expander("查看完整用例"):
                index = st.selectbox("选择用例", range(len(rows)), format_func=lambda i: f'{rows[i][1].case_id} · {rows[i][0]} · 第 {rows[i][1].sheet_row} 行')
                cat, turn = rows[index]
                st.text("\n".join(turn.user_messages))
                st.markdown("**预期结果**")
                st.text(turn.expected_outcome)
                st.caption(f'工具名称：{turn.tool_name or "无要求"}；参数要求：{turn.tool_parameters or "无要求"}')
            def use_scope():
                chosen = [rows[i] for i in selected_indices] if selected_indices else rows
                st.session_state["scope"] = {"toy": toy.id, "mode": "指定用例", "rows": [t.sheet_row for _, t in chosen],
                                             "datasets": [dataset.id] if dataset else []}
                st.session_state.pop("run_datasets", None)
                st.session_state.pop("scope_mode", None)
                st.session_state.pop("scope_rows", None)
                navigate(PAGES[2])
            st.button(f'使用{len(selected_indices) if selected_indices else len(rows)}条当前范围创建评测 →', type="primary", on_click=use_scope)
        else:
            empty_state("没有匹配的用例", "尝试其他关键词或重置类型筛选。", "⌕")
        duplicate_ids = [f"{cat}/{t.case_id}" for cat, t in all_rows]
        if len(set(duplicate_ids)) != len(duplicate_ids):
            st.warning("存在同类型重复编号，范围选择将按 Excel 行号区分，不会修改源表。")
        if toy.cases_xlsx.is_file():
            try:
                cases_management(toy, busy=job_manager().busy(DEFAULT_DEVICE_MAC))
            except (OSError, ValueError) as exc:
                st.error("用例维护不可用：" + redact_text(str(exc)))
    with tools_tab:
        tools_management(toy)
        tools = load_mcp_tools_from_txt(toy.mcp_tools_file) if toy.tools_source.get("type") != "mcp_http" else []
        if toy.tools_source.get("type") != "mcp_http":
            st.caption(f"来源：本地配置文件 · 当前读取时间 {datetime.now().strftime('%H:%M:%S')} · {len(tools)} 个工具")
        if not tools and toy.tools_source.get("type") != "mcp_http":
            st.warning("工具描述缺失、为空或格式无效。CLI 保留 ping 回退；界面会阻止缺少工具定义的强制 MCP 评测。")
        for tool in tools:
            with st.expander(tool["name"]):
                st.text(tool["description"])
                st.json(tool["inputSchema"])


@st.fragment(run_every="2s")
def live_job(cfg=None):
    job_manager().refresh()
    current = st.session_state.get("current_job")
    if not current:
        empty_state("准备就绪，等待开始", "检查左侧配置与选定范围。任务开始后，这里显示实际运行状态和日志。", "▷")
        return
    try:
        directory = confined(Path(current), ROOT / "reports")
        status = json.loads((directory / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st.warning("任务记录缺失或损坏，请刷新任务列表。")
        return
    previous = st.session_state.get("last_job_status")
    st.session_state["last_job_status"] = status["status"]
    if previous == "running" and status["status"] != "running":
        st.rerun(scope="app")
    label = {"running": "运行中", "completed": "执行完成", "failed": "执行失败", "stopped": "已停止"}.get(status["status"], "状态未知")
    st.subheader(label)
    st.caption(f'{status["job_id"]} · 开始于 {status.get("started_at", "无记录")[:19].replace("T", " ")}')
    if status.get("error"):
        st.error(redact_text(status["error"]))
    log_path = directory / "run.log"
    with st.expander("运行日志", expanded=status["status"] != "completed"):
        if log_path.is_file():
            with log_path.open("rb") as handle:
                handle.seek(max(0, log_path.stat().st_size - 24000))
                st.code(redact_text(handle.read().decode("utf-8", errors="replace")) or "正在启动……", language="text")
        else:
            st.caption("暂无日志。")
    path = status.get("report_paths", {}).get("detail_report")
    if path:
        st.button("进入本次结果分析 →", key="job_report", on_click=navigate, args=(PAGES[3], path), type="primary")
        st.caption("执行完成与用例通过是不同状态，具体结论请看结果分析。")
    if status["status"] != "running" and st.button("按当前页面配置重新运行", disabled=cfg is None):
        try:
            validate_request(cfg)
            st.session_state["current_job"] = str(job_manager().start(cfg))
            st.session_state["last_job_status"] = "running"
            st.rerun(scope="app")
        except (OSError, ValueError) as exc:
            st.error(redact_text(str(exc)))


def execution(toy, groups):
    from dataset_store import list_datasets, prepare_dataset_snapshot
    from evaluation_selection import selected_groups
    from toy_profiles import apply_toy_environment
    import tempfile
    available = [d for d in list_datasets(toy) if d.enabled]
    scope = st.session_state.get("scope", {})
    choices = {d.id: d for d in available}
    defaults = [d for d in scope.get("datasets", []) if d in choices]
    if not defaults and available:
        defaults = [available[0].id]
    dataset_ids = st.multiselect("本次测试集（公共 / 当前玩具专属）", list(choices), default=defaults,
                                format_func=lambda x: choices[x].name + (" · 公共" if choices[x].scope == "public" else " · 专属"), key="run_datasets")
    previous_datasets = st.session_state.get("previous_run_datasets")
    if previous_datasets is not None and previous_datasets != dataset_ids:
        for key in ["scope_rows", "scope_categories", "scope_mode"]:
            st.session_state.pop(key, None)
        st.session_state["scope"] = {"toy": toy.id, "datasets": dataset_ids, "mode": "全部用例"}
    st.session_state["previous_run_datasets"] = list(dataset_ids)
    preview = base_config(toy)
    preview.selected_dataset_ids = dataset_ids
    dataset_error = None
    try:
        if not dataset_ids:
            raise ValueError("请至少选择一份测试集")
        with tempfile.TemporaryDirectory(prefix="toy-ui-preview-") as temporary:
            preview = prepare_dataset_snapshot(preview, temporary)
            st.session_state["preview_dataset_snapshot"] = preview.dataset_snapshot
            groups = selected_groups(preview)
    except (OSError, ValueError) as exc:
        dataset_error = redact_text(str(exc))
        groups = []
    environment = st.selectbox("执行环境", [""] + list(toy.environments), format_func=lambda x: x or "默认环境", key="run_environment")
    page_header("评测配置与执行", "透明的评分配置，真实的执行状态。每次评测生成独立运行记录。", "Run evaluation")
    config_col, live_col = st.columns([1.15, 1])
    with config_col:
        with st.container(border=True):
            st.markdown("### 01 · 目标与范围")
            scope = st.session_state.get("scope", {})
            modes = ["全部用例", "按类别", "指定用例"]
            mode = st.radio("评测范围", modes, index=modes.index(scope.get("mode", "全部用例")), horizontal=True, key="scope_mode")
            categories, selected_rows = [], []
            if mode == "按类别":
                categories = st.multiselect("选择类别", [g.category for g in groups], key="scope_categories")
            if mode == "指定用例":
                options = {t.sheet_row: f'{t.case_id} · {g.category} · 第 {t.sheet_row} 行' for g in groups for t in g.turns}
                defaults = [r for r in scope.get("rows", []) if r in options]
                selected_rows = st.multiselect("选择用例", list(options), default=defaults, format_func=options.get, key="scope_rows")
            selected, range_error = [], None
            try:
                if mode == "按类别" and not categories or mode == "指定用例" and not selected_rows:
                    raise ValueError("请至少选择一个类别或一条用例")
                selected = filter_groups(groups, categories, rows=selected_rows)
            except ValueError as exc:
                range_error = str(exc)
            total = sum(len(g.turns) for g in selected)
            st.caption(f'即将评测 {total} 条 · {" / ".join(g.category for g in selected) or "尚未选择范围"}')
            if mode == "指定用例" and any("记忆" in g.category for g in selected):
                st.warning("指定记忆类用例可能省略前置上下文，请确认所选范围覆盖所需历史。")
            fast = st.toggle("Fast 模式", help="沿用 CLI 已有的握手与收尾等待参数")
        with st.container(border=True):
            st.markdown("### 02 · 评分规则")
            from judge_store import list_judge_models
            catalog = {item["id"]: item for item in list_judge_models() if item["enabled"]}
            chosen_judges = st.multiselect("本次评分裁判模型", list(catalog), default=[key for key in ("qwen", "deepseek") if key in catalog],
                                           format_func=lambda key: catalog[key]["name"] + " · " + catalog[key]["model"], key="selected_judges")
            st.caption("单裁判按其分数判定；多裁判全部成功后取算术平均，任一失败为评分失败。纯嵌入式规则用例不调用裁判。")
            cfg = base_config(toy)
            cfg.selected_judge_ids = chosen_judges
            cfg.selected_dataset_ids = dataset_ids
            cfg.dataset_snapshot = st.session_state.get("preview_dataset_snapshot", {})
            cfg.environment = environment
            cfg.enable_deepseek_judge = "deepseek" in chosen_judges
            rules_error = None
            try:
                cfg.rules_snapshot = load_rules(cfg)
                st.caption(source_info(cfg.judge_template_file))
                st.caption(source_info(cfg.thresholds_file))
                st.dataframe([{"类型": g.category, "阈值": cfg.rules_snapshot["thresholds"].get(g.category),
                               "来源": "分类配置" if g.category in cfg.rules_snapshot["overrides"] else "兼容默认值",
                               "标准": cfg.rules_snapshot["rubrics"].get(g.category, "未定义")} for g in selected], hide_index=True, width="stretch")
            except (OSError, ValueError) as exc:
                rules_error = f"评分规则配置无效：{redact_text(str(exc))}"
            st.caption("已选裁判：" + (" ＋ ".join(catalog[key]["name"] for key in chosen_judges) or "未选择（仅纯规则任务可运行）"))
            if cfg.rules_snapshot:
                with st.expander("本次每条用例的实际通过基准"):
                    st.dataframe([{"用例": t.case_id, "类型": g.category, "源行号": t.sheet_row,
                                   "阈值": page_threshold(cfg, toy, g, t),
                                   "方式": "MCP规则" if t.expect_tool_call and cfg.rules_snapshot.get("category_bases", {}).get(g.category, g.category) == "MCP调用" else "语言评分"}
                                  for g in selected for t in g.turns], hide_index=True, width="stretch")
            with st.expander("凭据（仅本次会话）", expanded=True):
                from connection_scenes import SCENE_LABELS
                scene = st.radio("接入场景", list(SCENE_LABELS), format_func=SCENE_LABELS.get,
                                 horizontal=True, key="connection_scene")
                if st.session_state.get("previous_connection_scene") != scene:
                    st.session_state.pop("run_token", None)
                    st.session_state.pop("current_job", None)
                    st.session_state.pop("last_job_status", None)
                    st.session_state["previous_connection_scene"] = scene
                if scene == "company":
                    st.caption("输入本次公司玩具 Token；留空沿用该玩具的已有凭据。")
                    token = st.text_input("玩具 Token", type="password", key="run_token")
                else:
                    token = ""
                    st.info("小智 AI：复用已在 Apifox 验证成功的设备身份；每轮重新通过 OTA 获取 WS 地址与 Token，无需公司玩具 Token。")
                    from connection_scenes import XIAOZHI_DEVICE_ID, XIAOZHI_CLIENT_ID
                    st.caption(f"Device-Id：{XIAOZHI_DEVICE_ID} · Client-Id：{XIAOZHI_CLIENT_ID}")
                    st.caption("首期请选择基础问答、反向安全、情感交互文本用例；不注入公司设备工具。")
                qwen_key = st.text_input("Qwen API Key", type="password", key="run_qwen")
                ds_key = st.text_input("DeepSeek API Key", type="password", key="run_ds")
            cfg.token = token.strip()
            environment_error = None
            try:
                if scene == "company":
                    cfg = apply_toy_environment(cfg, toy, environment)
                else:
                    from connection_scenes import configure_scene
                    cfg = configure_scene(cfg, scene)
            except ValueError as exc:
                environment_error = redact_text(str(exc))
            cfg.api_key = qwen_key.strip() or cfg.api_key
            cfg.deepseek_api_key = ds_key.strip() or cfg.deepseek_api_key
            judge_error = None
            try:
                from ui_scoring import configure_judges
                cfg = configure_judges(cfg)
            except (OSError, ValueError) as exc:
                judge_error = redact_text(str(exc))
            with st.expander("高级连接参数"):
                if scene == "company":
                    cfg.device_mac = st.text_input("设备 MAC", cfg.device_mac, key="device_mac_" + toy.id + environment)
                    cfg.ota_url = st.text_input("OTA URL", cfg.ota_url, key="ota_" + toy.id + environment)
                else:
                    st.caption("OTA：" + cfg.ota_url)
                    st.caption("WebSocket：使用本次 OTA 响应，不沿用公司地址。设备身份跨运行复用。")
                cfg.response_collect_timeout = st.number_input("单条回复超时（秒）", min_value=1.0, value=45.0)
                cfg.judge_concurrency = int(st.number_input("评分并发", min_value=1, max_value=32, value=6))
            cfg.selected_categories, cfg.selected_case_rows = categories, selected_rows
            if fast:
                apply_fast_mode(cfg)
            blocker = dataset_error or environment_error or range_error or rules_error or judge_error
            if not blocker:
                try:
                    validate_request(cfg)
                except (OSError, ValueError, KeyError) as exc:
                    blocker = redact_text(str(exc))
            busy = job_manager().busy(cfg.device_mac)
            if blocker:
                st.error(blocker)
            elif busy:
                st.info("当前设备已有评测运行，请等待完成。")
            else:
                st.success("启动前检查通过")
            if st.button("▷ 开始评测", type="primary", width="stretch", disabled=bool(blocker or busy)):
                try:
                    st.session_state["current_job"] = str(job_manager().start(cfg))
                    st.session_state["last_job_status"] = "running"
                    st.rerun()
                except (OSError, ValueError) as exc:
                    st.error(redact_text(str(exc)))
    with live_col:
        with st.container(border=True):
            st.markdown("### 03 · 任务与运行状态")
            st.caption("单次界面任务最多运行 4 小时；展示真实状态与日志，不模拟完成百分比。")
            st.button("刷新任务", key="refresh_tasks")
            jobs = []
            for path in sorted((ROOT / "reports" / toy.id).glob("ui_runs/*/job.json"), reverse=True):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    jobs.append((str(path.parent), data))
                except (OSError, ValueError):
                    continue
            if jobs:
                options = [path for path, _ in jobs]
                current = st.session_state.get("current_job", options[0])
                selected_job = st.selectbox("查看任务", options, index=options.index(current) if current in options else 0,
                                            format_func=lambda p: Path(p).name)
                st.session_state["current_job"] = selected_job
            live_job(cfg if not blocker and not busy else None)


def result_page(toy):
    page_header("结果分析", "从整体质量到单条诊断，每个结论都可以回到原始记录。", "Results & diagnostics")
    reports = reports_for(toy)
    if not reports:
        empty_state("还没有可分析的报告", "完成一次评测后，可在这里筛选结果、查看工具调用和裁判依据，并下载报告。")
        st.button("创建评测 →", type="primary", on_click=navigate, args=(PAGES[2],))
        return
    target = st.session_state.get("report_target")
    paths = [str(r["path"]) for r in reports]
    index = paths.index(target) if target in paths else 0
    chosen = st.selectbox("选择评测报告", range(len(reports)), index=index, format_func=lambda i: report_label(reports[i]))
    show_report(reports[chosen])


@st.cache_resource
def xiaozhi_chat_bridge():
    from xiaozhi_chat import ChatBridge
    return ChatBridge()


def live_chat_page(name):
    url = LIVE_CHAT_URLS[name]
    page_header(f"{name} · 实时对话", "唤醒玩具后，即可在下方进行文本或语音对话。" if url else "该玩具的对话入口待配置。", "Toy chat")
    if not url:
        empty_state(f"{name}对话暂未接入", "提供该玩具的对话链接后，即可在这里直接使用。", "◌")
        return
    with st.container(key="live-chat-frame", height="stretch"):
        source = xiaozhi_chat_bridge().html() if name == "小智" else url
        st.iframe(source, height="stretch", alt=f"{name}实时对话")


def chat_demo():
    """Independent presentation route; reuse the exact same chat renderers/bridge."""
    st.set_page_config(page_title="玩具实时对话", initial_sidebar_state="collapsed")
    apply_styles()
    st.markdown("""<style>
        [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
        [data-testid="stHeader"] {display:none;}
        [data-testid="stMainBlockContainer"] {max-width:1440px;}
        </style>""", unsafe_allow_html=True)
    st.markdown('<div class="topline"><strong>玩具实时对话</strong><span class="badge">互动体验</span></div>', unsafe_allow_html=True)
    names = list(LIVE_CHAT_URLS)
    requested = st.query_params.get("toy", names[0])
    name = st.radio("选择玩具", names, index=names.index(requested) if requested in names else 0,
                    horizontal=True, key="demo_chat_toy", label_visibility="collapsed")
    st.query_params["toy"] = name
    live_chat_page(name)


def release_demo():
    """独立演示入口，复用平台自测和报告，不创建另一套执行逻辑。"""
    st.set_page_config(page_title="发版自测", initial_sidebar_state="collapsed")
    apply_styles()
    st.markdown("""<style>
        [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
        [data-testid="stHeader"] {display:none;}
        [data-testid="stMainBlockContainer"] {max-width:1440px;}
        </style>""", unsafe_allow_html=True)
    target = st.session_state.get("release_demo_report")
    if target:
        st.title("发版自测结果")
        if st.button("← 返回发版自测", key="release_demo_back"):
            st.session_state.pop("release_demo_report", None)
            st.rerun()
        try:
            show_report(read_report(Path(target), ROOT / "reports"))
        except (OSError, ValueError, KeyError) as exc:
            st.error("报告读取失败：" + redact_text(str(exc)))
        return
    from ui_release import release_page
    def open_report(path):
        st.session_state["release_demo_report"] = str(path)
    release_page(list_toy_profiles(), base_config, job_manager(), ROOT / "reports", detail_panel, open_report)


def main():
    apply_styles()
    toys = list_toy_profiles()
    pending = st.session_state.pop("release_report_pending", None)
    if pending:
        st.session_state["toy_choice"] = pending["toy_id"]
        st.session_state["navigation"] = PAGES[3]
    with st.sidebar:
        st.markdown('<div class="brand"><div class="brand-icon">a</div><div><div class="brand-title">Agent Eval</div><div class="brand-sub">QUALITY WORKSPACE</div></div></div>', unsafe_allow_html=True)
        if not toys:
            st.error("没有可用玩具配置，请检查 toys/<id>/toy.json。")
            return
        toy_names = {t.id: t.name or t.id for t in toys}
        toy_id = st.selectbox("当前玩具", [t.id for t in toys], key="toy_choice", format_func=toy_names.get)
        if st.session_state.get("previous_toy") != toy_id:
            for key in ["scope", "scope_mode", "scope_rows", "scope_categories", "case_categories", "case_query", "case_table",
                        "current_job", "report_target", "result_categories", "result_statuses", "result_query", "result_detail", "run_token", "run_datasets", "run_environment", "previous_run_datasets"]:
                st.session_state.pop(key, None)
            st.session_state["previous_toy"] = toy_id
        if pending:
            st.session_state["report_target"] = pending["path"]
        page = st.radio("工作空间", PAGES, key="navigation", label_visibility="collapsed",
                        format_func=lambda value: "测试集与工具管理" if value == PAGES[1] else value)
        chat_name = None
        if page == LIVE_CHAT_PAGE:
            with st.container(key="live-chat-submenu"):
                chat_name = st.radio("实时对话玩具", list(LIVE_CHAT_URLS), key="live_chat_toy", label_visibility="collapsed")
        toy = next(t for t in toys if t.id == toy_id)
        healthy = toy.cases_xlsx.is_file() and (toy.tools_source.get("type") == "mcp_http" or toy.mcp_tools_file.is_file())
        st.caption(("● 远程来源已配置（待连接）" if toy.tools_source.get("type") == "mcp_http" else "● 数据源可读取") if healthy else "○ 数据源需要检查")
        with st.expander("环境与版本"):
            st.caption(f"Python {platform.python_version()}")
            for name in ["streamlit", "websockets", "openai", "pandas", "plotly"]:
                st.caption(f"{name}  {importlib.metadata.version(name)}")
        st.markdown('<div class="side-foot">Agent 评测工作台<br>本地文件 · 真实运行 · 可追溯结果</div>', unsafe_allow_html=True)
    display_name = chat_name if page == LIVE_CHAT_PAGE else (toy.name or toy.id)
    badge = "实时对话" if page == LIVE_CHAT_PAGE else "文本评测"
    st.markdown(f'<div class="topline"><span>工作空间 <strong> / {escape(display_name)}</strong></span><span class="badge">{badge}</span></div>', unsafe_allow_html=True)
    groups = safe_groups(toy) if page not in (PAGES[3], LIVE_CHAT_PAGE, RELEASE_PAGE) else []
    if page == PAGES[0]:
        dashboard(toy, groups)
    elif page == PAGES[1]:
        case_studio(toy, groups)
    elif page == PAGES[2]:
        execution(toy, groups)
    elif page == PAGES[3]:
        result_page(toy)
    elif page == PAGES[4]:
        from ui_catalogs import toy_management_page
        toy_management_page(toy, base_config)
    elif page == LIVE_CHAT_PAGE:
        live_chat_page(chat_name)
    elif page == RELEASE_PAGE:
        from ui_release import release_page
        release_page(toys, base_config, job_manager(), ROOT / "reports", detail_panel, open_release_report)
    if page == PAGES[2]:
        from toy_profiles import apply_toy_environment
        triggers_management(toys, lambda item: apply_toy_environment(base_config(item), item))
    if st.session_state.get("asset_notice"):
        st.toast(st.session_state.pop("asset_notice"))


if __name__ == "__main__":
    router = st.navigation([
        st.Page(main, title="Agent 评测工作台", default=True),
        st.Page(chat_demo, title="玩具实时对话", url_path="chat"),
        st.Page(release_demo, title="发版自测", url_path="release"),
    ], position="hidden")
    router.run()
