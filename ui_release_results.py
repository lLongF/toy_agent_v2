"""独立发版页的报告展示；只读取报告快照，不重新评分。"""
from collections import Counter
from hashlib import sha256
from html import escape

import streamlit as st

from safe_output import redact_text
from ui_results import attachments, summarise


def display_status(row):
    return "执行异常" if row.get("execution_error") else row["status"]


def badge(status):
    tone = {"通过": "pass", "不通过": "fail", "评分失败": "warning"}.get(status, "neutral")
    return f'<span class="release-status {tone}">{escape("未通过" if status == "不通过" else status)}</span>'


def result_summary(report, root, open_report, job_status):
    rows = report["rows"]
    counts = Counter(display_status(row) for row in rows)
    metrics = summarise(rows)
    titles = {"running": "本次自测运行中", "failed": "本次自测执行失败", "stopped": "本次自测已停止"}
    title = titles.get(job_status, "本次自测已完成" if job_status == "completed" else "自测结果快照")
    incomplete = job_status != "completed"
    issues = sum(counts[s] for s in ("不通过", "评分失败", "执行异常", "未执行"))
    label = "结果尚不完整" if incomplete else ("发现待关注项" if issues else ("全部用例通过" if rows else "暂无用例结果"))
    tone = "warning" if incomplete or issues or not rows else "pass"
    notes = [f'{counts[s]} 条{label}' for s, label in (("不通过", "业务未通过"), ("评分失败", "评分失败"),
             ("执行异常", "执行异常"), ("未执行", "未执行")) if counts[s]]
    note = "，".join(notes) or ("所有已记录用例均通过。" if rows else "报告尚未记录用例结果。")
    if incomplete:
        note = "仅展示当前已生成的结果；" + note
    protocol = report["protocol"]
    models = " ＋ ".join(f'{m.get("name", "裁判")} · {m.get("model", "")}' for m in protocol.get("judge_models", []))
    with st.container(key="release-result-banner"):
        summary, actions = st.columns([2, 1], gap="medium")
        with summary:
            st.markdown(f'<div class="release-result-title"><span class="release-result-symbol {tone}">'
                        f'{"!" if tone == "warning" else "✓"}</span><div><h3>{title}</h3>'
                        f'<span class="release-status {tone}">{label}</span></div></div>', unsafe_allow_html=True)
            st.markdown(f'<div class="release-result-note">{escape(note)}</div>', unsafe_allow_html=True)
            st.caption("玩具：" + str(protocol.get("target_name") or protocol.get("toy_id") or "未记录") +
                       " · 裁判：" + (models or "未记录语言裁判；详见用例诊断"))
        with actions:
            for file in attachments(report, root):
                if file.suffix == ".xlsx":
                    st.download_button("下载 Excel 报告", file.read_bytes(), file_name=file.name,
                                       type="primary", width="stretch", key="release_download_" + str(file))
            if st.button("查看完整报告", key="release_full_report", width="stretch"):
                open_report(report["path"])
                st.rerun(scope="app")
    cards = [("用例总数", len(rows), "neutral"), ("通过", counts["通过"], "pass"),
             ("未通过", counts["不通过"], "fail"), ("评分失败", counts["评分失败"], "warning"),
             ("执行异常", counts["执行异常"], "neutral"), ("未执行", counts["未执行"], "neutral"),
             ("业务通过率", f'{metrics["pass_rate"]:.1%}' if metrics["pass_rate"] is not None else "—", "pass")]
    markup = "".join(f'<div class="release-result-metric {tone}"><span>{label}</span><strong>{value}</strong></div>'
                     for label, value, tone in cards)
    st.markdown('<div class="release-result-metrics">' + markup + '</div>', unsafe_allow_html=True)
    st.caption(f'业务通过率：{metrics["passed_count"]} / {metrics["evaluated_count"]} 条可判定用例；'
               '不含评分失败、执行异常与未执行。工具规则沿用原标准，规则得分不混入语言平均分。')


def result_diagnostic(row, detail_panel):
    st.markdown("### 用例诊断")
    st.markdown(f'<div class="release-diagnostic-title"><strong>{escape(row["case_id"])}</strong>'
                f'{badge(display_status(row))}</div>', unsafe_allow_html=True)
    st.caption(f'{row["category"]} · 源表第 {row["row"]} 行')
    for label, value in (("用户输入", "\n".join(row["messages"])), ("预期结果", row["expected"]),
                         ("实际回答", row["actual"]), ("判定依据", row["reason"])):
        st.markdown(f'<div class="release-diagnostic-label">{label}</div>'
                    f'<div class="release-diagnostic-text">{escape(redact_text(str(value or "无记录")))}</div>',
                    unsafe_allow_html=True)
    left, right = st.columns(2)
    if row.get("rule_only") and row.get("rule_score") is not None:
        left.metric("工具规则得分", row.get("rule_score") if row.get("rule_score") is not None else "—")
        right.metric("规则通过标准", "1")
    else:
        left.metric("综合评分", f'{row["score"]:.2f}' if row["score"] is not None else "—")
        right.metric("通过阈值", f'{row["threshold"]:.2f}' if row["threshold"] is not None else "无记录")
    with st.expander("完整诊断与工具调用"):
        detail_panel(row)


def release_presentation_results(report, root, detail_panel, open_report, job_status="completed"):
    result_summary(report, root, open_report, job_status)
    listing, diagnostic = st.columns([1.6, 1], gap="medium")
    with listing, st.container(key="release-result-list"):
        st.markdown("### 用例结果")
        choice = st.radio("结果筛选", ["全部", "未通过", "异常 / 未执行"], horizontal=True, key="release_filter")
        query = st.text_input("查询用例", placeholder="搜索编号、类型、输入或结果", key="release_result_query").strip().casefold()
        visible = [row for row in report["rows"] if
                   (choice == "全部" or choice == "未通过" and display_status(row) == "不通过" or
                    choice == "异常 / 未执行" and display_status(row) in {"评分失败", "执行异常", "未执行"}) and
                   (not query or query in " ".join([row["case_id"], row["category"], *row["messages"], row["actual"], row["reason"]]).casefold())]
        if visible:
            # 筛选或报告变化时更换表格状态，避免选中的行号指向另一条用例。
            identity = sha256((str(report["path"]) + choice + query + str([(r["row"], r["case_id"]) for r in visible])).encode()).hexdigest()[:16]
            key = "release_result_table_" + identity
            event = st.dataframe([{"用例": r["case_id"], "类型": r["category"], "实际结果": redact_text(r["actual"]),
                                   "状态": "未通过" if display_status(r) == "不通过" else display_status(r)} for r in visible],
                                 hide_index=True, width="stretch", key=key, on_select="rerun", selection_mode="single-row",
                                 row_height=48)
            selected = event.selection.rows
            select_key = "release_detail_" + identity
            if selected and st.session_state.get(key + "_seen") != list(selected):
                st.session_state[select_key] = selected[0]
            st.session_state[key + "_seen"] = list(selected)
            index = st.selectbox("查看用例详情", range(len(visible)), key=select_key,
                                 format_func=lambda i: f'{visible[i]["case_id"]} · {visible[i]["category"]} · 第 {visible[i]["row"]} 行')
            st.caption(f"当前显示 {len(visible)} 条用例，点击表格行或选择编号查看诊断。")
        else:
            st.info("没有匹配的用例。")
    with diagnostic, st.container(key="release-result-diagnostic"):
        if visible:
            result_diagnostic(visible[index], detail_panel)
        else:
            st.markdown("### 用例诊断")
            st.caption("调整筛选条件后，可在这里查看用例详情。")
