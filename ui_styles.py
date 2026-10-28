"""共享视觉组件；所有动态文本 HTML 转义，交互仍使用原生 Streamlit。"""
from html import escape

import streamlit as st


def apply_styles():
    st.markdown("""<style>
    .stApp {font-family: Inter, -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;}
    [data-testid="stMainBlockContainer"] {padding:2.2rem 3rem 3rem; max-width:1600px;}
    [data-testid="stHeader"] {background:transparent;}
    [data-testid="stSidebar"] {border-right:1px solid #273044;}
    [data-testid="stSidebar"] [data-testid="stRadio"] label {padding:12px 14px;border-radius:10px;margin:3px 0;transition:background .15s;}
    [data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) {background:#293253;box-shadow:inset 3px 0 #a5adff;}
    [data-testid="stSidebar"] [data-testid="stRadio"] label:hover {background:#232c41;}
    [data-testid="stSidebar"] [data-testid="stRadio"] label > div:first-child {display:none;}
    [data-testid="stVerticalBlockBorderWrapper"] > div {box-shadow:0 3px 12px #172b4d03;}
    [data-testid="stMetric"] {background:#fff;border:1px solid #e8ebf2;border-radius:14px;padding:18px 20px;min-height:115px;}
    [data-testid="stMetricLabel"] {color:#738096;font-size:12px;letter-spacing:.015em;}
    [data-testid="stMetricValue"] {font-size:30px;font-weight:650;letter-spacing:-1px;line-height:1.5;}
    [data-testid="stButton"] button {min-height:42px; font-weight:550;}
    [data-testid="stDataFrame"] {border:1px solid #e8ebf2;border-radius:12px;overflow:hidden;}
    h1 {font-size:30px!important;font-weight:700!important;letter-spacing:-1px;padding-top:0!important;}
    h2 {font-size:21px!important;letter-spacing:-.4px;}
    h3 {font-size:17px!important;font-weight:650!important;}
    .eyebrow {font-size:10px;font-weight:700;letter-spacing:2px;color:#7d87a2;text-transform:uppercase;margin-bottom:8px;}
    .brand {display:flex;gap:10px;align-items:center;margin:8px 0 28px;}
    .brand-icon {width:37px;height:37px;background:#6973f5;border-radius:11px;display:grid;place-items:center;color:white;font-size:22px;font-weight:700;}
    .brand-title {color:#f4f6fe;font-size:17px;font-weight:700;letter-spacing:-.4px;}
    .brand-sub {font-size:10px;color:#8b98b4;letter-spacing:1.5px;margin-top:2px;}
    .topline {display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #e4e9f1;padding-bottom:17px;margin-bottom:30px;color:#8390a4;font-size:12px;}
    .topline strong {color:#4b5870;font-weight:500;}
    .badge {display:inline-block;padding:5px 9px;border-radius:6px;background:#eef1ff;color:#5863c9;font-size:11px;font-weight:600;}
    .empty {border:1px dashed #d5dce9;background:#fff;border-radius:16px;padding:48px 22px;text-align:center;margin:18px 0;}
    .empty-icon {font-size:27px;color:#8993db;background:#f1f3ff;width:58px;height:58px;margin:0 auto 18px;border-radius:16px;display:grid;place-items:center;}
    .empty-title {color:#303c55;font-size:18px;font-weight:600;margin-bottom:9px;}
    .empty-note {color:#8792a6;font-size:13px;line-height:1.8;max-width:540px;margin:auto;}
    .source {color:#7d89a0;font-size:12px;line-height:1.8;overflow-wrap:anywhere;}
    .step {font-size:12px;letter-spacing:.5px;color:#6973cf;font-weight:650;padding:4px 0 10px;}
    [data-testid="stSidebar"] .side-foot {border-top:1px solid #2b344b;margin-top:40px;padding-top:18px;color:#8492ae;font-size:11px;line-height:1.9;}
    @media(max-width:800px) {[data-testid="stMainBlockContainer"] {padding:1.4rem 1rem;} [data-testid="stMetricValue"] {font-size:23px;}}
    @media(prefers-reduced-motion:reduce) {* {transition:none!important;}}
    </style>""", unsafe_allow_html=True)


def page_header(title, subtitle, eyebrow):
    st.markdown(f'<div class="eyebrow">{escape(eyebrow)}</div>', unsafe_allow_html=True)
    st.title(title)
    st.caption(subtitle)


def empty_state(title, note, icon="◫"):
    st.markdown(f'<div class="empty"><div class="empty-icon">{escape(icon)}</div>'
                f'<div class="empty-title">{escape(title)}</div><div class="empty-note">{escape(note)}</div></div>',
                unsafe_allow_html=True)


def metric_cards(metrics):
    columns = st.columns(6)
    values = [metrics["case_count"], metrics["passed_count"],
              f'{metrics["pass_rate"]:.1%}' if metrics["pass_rate"] is not None else "—",
              metrics["failed_count"], metrics["judge_error_count"],
              f'{metrics["average_score"]:.3f}' if metrics["average_score"] is not None else "—"]
    for column, label, value in zip(columns, ["评测用例", "业务通过", "业务通过率", "业务失败", "评分失败", "平均综合分"], values):
        column.metric(label, value)
    st.caption(f'业务通过率分母：{metrics["evaluated_count"]} 条可判定结果（含 MCP 规则判定），排除评分失败与未执行。'
               f' 平均分仅统计 {metrics["scored_count"]} 条有效综合分。未执行 {metrics["unevaluated_count"]} 条。')
