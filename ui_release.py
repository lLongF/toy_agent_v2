"""后端发版自测：简化输入，共用评测引擎、任务隔离与报告契约。"""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import replace
from html import escape
from pathlib import Path

import streamlit as st

from config import load_dialogue_groups_from_xlsx
from dataset_store import list_datasets
from judge_store import list_judge_models, resolve_judge_credential
from safe_output import redact_text
from toy_profiles import apply_toy_environment
from ui_request import validate_request
from ui_results import attachments, confined, read_report
from ui_scoring import configure_judges
from ui_styles import page_header


def release_config(base, toy, *, url, token, datasets, judges, mac, client, judge_keys=None):
    if not datasets:
        raise ValueError("请至少选择一份测试集")
    if not judges:
        raise ValueError("请至少选择一个裁判模型")
    if not token.strip():
        raise ValueError("请输入本次连接使用的玩具 Token")
    # 临时 Token 优先；不得读取或覆盖该玩具已保存的凭据。
    cfg = apply_toy_environment(replace(base, token=token.strip()), toy)
    cfg = replace(cfg, ws_url=url.strip(), token=token.strip(), device_mac=mac.strip(),
                  client_id=client.strip(), connection_mode="direct", connection_scene="company",
                  trigger_type="release_selftest", release_environment="",
                  selected_dataset_ids=list(datasets), selected_judge_ids=list(judges),
                  judge_credentials={identity: value.strip() for identity, value in (judge_keys or {}).items()
                                     if identity in judges and value.strip()})
    from connection_scenes import validate_connection_mode
    validate_connection_mode(cfg)
    return configure_judges(cfg)


def release_jobs(root):
    jobs = []
    for path in root.glob("*/ui_runs/*/job.json"):
        try:
            path = confined(path, root)
            data = json.loads(path.read_text(encoding="utf-8"))
            # 兼容热更新前旧管理器写出的任务状态，从报告快照恢复来源标记。
            if not data.get("trigger_type") and data.get("report_paths", {}).get("detail_report"):
                report_path = confined(Path(data["report_paths"]["detail_report"]), root)
                protocol = json.loads(report_path.read_text(encoding="utf-8")).get("protocol_info", {})
                for key in ("trigger_type", "release_environment", "target_name"):
                    data[key] = protocol.get(key, "")
            if data.get("trigger_type") == "release_selftest":
                jobs.append((str(path.parent), data))
        except (OSError, ValueError):
            continue
    return sorted(jobs, key=lambda item: item[1].get("started_at", ""), reverse=True)


def result_status(row):
    # 执行异常在历史报告中归入未执行；此处细分展示，不改变既有评分口径。
    return "执行异常" if row.get("execution_error") else row["status"]


def release_results(report, root, detail_panel, open_report, interrupted=False):
    rows = report["rows"]
    counts = Counter(result_status(row) for row in rows)
    if interrupted:
        st.info("以下仅为停止前已生成的结果，本次任务未完成。")
    elif rows and counts["通过"] == len(rows):
        st.success("执行完成，全部用例通过。")
    else:
        st.warning("存在未通过、异常或未执行用例，请查看明细。")
    columns = st.columns(5)
    for column, status in zip(columns, ("通过", "不通过", "执行异常", "评分失败", "未执行")):
        column.metric("未通过" if status == "不通过" else status, counts[status])
    models = report["protocol"].get("judge_models", [])
    st.caption("本次裁判：" + (" ＋ ".join(f'{m["name"]} · {m["model"]}' for m in models) or "本次未调用语言裁判"))
    choice = st.radio("结果筛选", ["全部", "未通过", "异常 / 未执行"], horizontal=True, key="release_filter")
    visible = [r for r in rows if choice == "全部" or
               (choice == "未通过" and result_status(r) == "不通过") or
               (choice == "异常 / 未执行" and result_status(r) in {"执行异常", "评分失败", "未执行"})]
    if visible:
        st.dataframe([{"用例": r["case_id"], "类型": r["category"], "输入": " / ".join(r["messages"]),
                       "实际结果": r["actual"], "状态": result_status(r), "原因": r["reason"]}
                      for r in visible], hide_index=True, width="stretch")
        index = st.selectbox("查看用例详情", range(len(visible)),
                             format_func=lambda i: f'{visible[i]["case_id"]} · {visible[i]["category"]} · 第 {visible[i]["row"]} 行',
                             key="release_detail_" + report["path"].parent.name + choice)
        with st.container(border=True):
            detail_panel(visible[index])
    else:
        st.info("没有匹配的用例。")
    for file in attachments(report, root):
        if file.suffix == ".xlsx":
            st.download_button("下载 Excel 报告", file.read_bytes(), file_name=file.name,
                               key="release_download_" + str(file))
    if st.button("查看完整结果 →", key="release_full_report"):
        open_report(report["path"])
        st.rerun(scope="app")


def show_progress(directory, job, root):
    if job["status"] == "completed":
        st.progress(1.0, text="任务执行完成 · 用例通过情况见下方结果")
        return
    path = confined(Path(directory) / "progress.json", root)
    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        progress = {}
    if job["status"] in {"failed", "stopped"}:
        label = "用户已停止运行" if job["status"] == "stopped" else "执行失败"
        st.caption("任务已结束：" + label + "。" + ("最后阶段：" + progress["stage"] if progress.get("stage") else ""))
        return
    stage = progress.get("stage", "任务运行中，等待进度计数")
    completed, total = progress.get("completed", 0), progress.get("total", 0)
    if total > 0:
        st.progress(min(1.0, max(0.0, completed / total)), text=f"{stage}：已处理 {completed} / {total}")
        st.caption("进度为当前阶段的处理数量，包含失败项，不代表用例通过率。")
    else:
        st.progress(0.0, text=stage + "，请稍候……")


@st.fragment(run_every="3s")
def release_history(manager, root, detail_panel, open_report, device_mac=None, *, presentation=False):
    manager.refresh()
    jobs = release_jobs(root)
    active = getattr(manager, "active_job", lambda device: None)(device_mac) if device_mac else None
    if device_mac and st.session_state.get("release_device_busy") and not active:
        st.session_state["release_device_busy"] = False
        st.rerun(scope="app")
    if active and str(active) not in {p for p, _ in jobs}:
        try:
            active = confined(Path(active), root)
            jobs.insert(0, (str(active), json.loads((active / "job.json").read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            st.warning("设备已有任务运行，暂时无法读取任务记录。")
    st.markdown("### 本次自测结果" if presentation else "### 02 · 自测结果与最近运行")
    if not jobs:
        st.info("尚无发版自测记录。开始后将展示真实状态与日志。")
        return
    options = {path: data for path, data in jobs}
    current = st.session_state.get("release_current_job")
    if current not in options:
        st.session_state["release_current_job"] = next(iter(options))
    selected = st.selectbox("查看自测记录", list(options), key="release_current_job",
                            format_func=lambda p: f'{options[p].get("started_at", "")[:19].replace("T", " ")} · '
                            f'{options[p].get("target_name", "")} · '
                            f'{ {"running": "运行中", "completed": "执行完成", "failed": "执行失败", "stopped": "已停止"}.get(options[p]["status"], "未知状态")}')
    job = options[selected]
    show_progress(selected, job, root)
    if job["status"] == "running":
        st.info("运行中：日志按实际阶段更新，每 3 秒刷新。单次任务上限 4 小时。")
        if st.button("停止运行", key="release_stop_" + Path(selected).name, type="secondary"):
            from ui_jobs import stop_job
            try:
                with st.spinner("正在中断任务……"):
                    stop_job(manager, Path(selected))
                st.rerun(scope="app")
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                st.error(redact_text(str(exc)))
    if job["status"] == "stopped":
        st.warning("本次运行已由用户停止。日志及已生成的报告已保留；未完成部分不视为通过。")
    if job.get("error"):
        st.error(redact_text(job["error"]))
    path = job.get("report_paths", {}).get("detail_report")
    if path:
        try:
            report = read_report(Path(path), root)
            if presentation:
                from ui_release_results import release_presentation_results
                release_presentation_results(report, root, detail_panel, open_report, job_status=job["status"])
            else:
                release_results(report, root, detail_panel, open_report, interrupted=job["status"] == "stopped")
        except (OSError, ValueError, KeyError) as exc:
            st.error("报告读取失败：" + redact_text(str(exc)))
    log = confined(Path(selected) / "run.log", root)
    with st.expander("运行日志", expanded=job["status"] == "running"):
        if log.is_file():
            with log.open("rb") as handle:
                handle.seek(max(0, log.stat().st_size - 24000))
                st.code(redact_text(handle.read().decode("utf-8", errors="replace")) or "正在启动……", language="text")


def release_page(toys, base_config, manager, root, detail_panel, open_report, *, presentation=False):
    for identity in st.session_state.pop("release_clear_keys", []):
        st.session_state.pop("release_api_key_" + identity, None)
    if presentation:
        with st.container(key="release-hero"):
            st.markdown('<div class="release-brand"><span>◈</span> 产品质量验证 <b>RELEASE CHECK</b></div>', unsafe_allow_html=True)
            st.title("发版自测")
            st.caption("配置自测、查看结果，完整掌握本次发版表现。")
            st.markdown('<div class="release-flow"><span><b>01</b> 配置自测</span><i>→</i>'
                        '<span><b>02</b> 执行与评分</span><i>→</i><span><b>03</b> 查看报告</span></div>', unsafe_allow_html=True)
    else:
        page_header("发版自测", "选择玩具和测试集，快速检查本次发版。", "RELEASE CHECK")
    enabled = {t.id: t for t in toys if t.enabled}
    if not enabled:
        st.warning("没有已启用的玩具，请先在玩具管理中配置。")
        return
    setup, overview = st.columns([1.4, 1], gap="medium") if presentation else (st.container(), None)
    with setup, st.container(border=True, key="release-setup"):
        st.markdown("### ⚙ 自测配置" if presentation else "### 01 · 连接与用例")
        st.caption("使用下方填写的接口地址直连 WebSocket，跳过 OTA。")
        url = st.text_input("WebSocket 接口地址", placeholder="wss://测试环境域名/接口路径", key="release_url")
        left, right = st.columns(2)
        toy_id = left.selectbox("玩具", list(enabled), format_func=lambda x: enabled[x].name or x, key="release_toy")
        toy = enabled[toy_id]
        token = right.text_input("玩具 Token", type="password", key="release_token_" + toy_id,
                                 help="仅本次使用，不覆盖玩具已保存的凭据。")
        datasets = {d.id: d for d in list_datasets(toy) if d.enabled}
        selected = st.multiselect("测试集", list(datasets), default=list(datasets)[:1],
                                  format_func=lambda x: datasets[x].name, key="release_datasets_" + toy_id)
        try:
            counts = Counter()
            for identity in selected:
                for group in load_dialogue_groups_from_xlsx(datasets[identity].path):
                    counts[group.category] += len(group.turns)
            st.caption(f"共 {sum(counts.values())} 条用例" + "".join(f" · {k} {v} 条" for k, v in counts.items()))
        except (OSError, ValueError) as exc:
            st.error("测试集读取失败：" + redact_text(str(exc)))
        catalog = {m["id"]: m for m in list_judge_models() if m["enabled"]}
        judges = st.multiselect("裁判模型", list(catalog), default=list(catalog)[:1],
                               format_func=lambda x: catalog[x]["name"] + " · " + catalog[x]["model"], key="release_judges")
        judge_keys = {}
        credential_states = {}
        for identity in judges:
            model_column, key_column = st.columns([1, 2])
            model_column.markdown(f'**{catalog[identity]["name"]} · {catalog[identity]["model"]}**')
            judge_keys[identity] = key_column.text_input(
                catalog[identity]["name"] + " API Key", type="password",
                key="release_api_key_" + identity, placeholder="本次临时输入；留空使用已配置凭据")
            if presentation:
                try:
                    credential_states[identity] = bool(resolve_judge_credential(
                        catalog[identity], {identity: judge_keys[identity]} if judge_keys[identity].strip() else {}))
                except (OSError, ValueError):
                    credential_states[identity] = False
                key_column.caption("● 已读取本次输入的凭据" if judge_keys[identity].strip() else
                                   ("● 已读取配置凭据，可留空" if credential_states[identity] else "○ 未读取到配置凭据，请填写 Key"))
        st.caption("Key 留空使用已配置凭据；手动输入仅用于本次自测。")
        base = base_config(toy)
        with st.expander("高级连接设置"):
            mac = st.text_input("设备 MAC", value=base.device_mac, key="release_mac_" + toy_id)
            client = st.text_input("客户端 ID", value=base.client_id, key="release_client_" + toy_id)
        st.caption("沿用测试集阈值及现有评分规则；玩具 Token 仅供本次使用。")
        active = manager.active_job(mac.strip())
        st.session_state["release_device_busy"] = bool(active)
        if active:
            st.info(f"该设备正在运行任务 {Path(active).name}，下方可查看状态，请等待结束。")
        if st.button("开始自测 →", type="primary", key="release_start", width="stretch" if presentation else "content",
                     disabled=bool(active) or not (url.strip() and token.strip() and selected and judges)):
            try:
                cfg = release_config(base, toy, url=url, token=token,
                                     datasets=selected, judges=judges, mac=mac, client=client, judge_keys=judge_keys)
                validate_request(cfg)
                st.session_state["release_current_job"] = str(manager.start(cfg))
                st.session_state["release_clear_keys"] = list(judges)
                st.rerun()
            except (OSError, ValueError, KeyError) as exc:
                st.error(redact_text(str(exc), [token, *judge_keys.values()]))
    if overview is not None:
        with overview, st.container(border=True, key="release-overview"):
            st.markdown("### ◈ 本次配置概览")
            st.markdown(f'<div class="release-product"><span>◈</span><div><strong>{escape(toy.name or toy.id)}</strong>'
                        '<small>发版质量验证</small></div></div>', unsafe_allow_html=True)
            st.markdown("### 自测概览")
            for column, label, value in zip(st.columns(3), ("测试集", "用例", "裁判"),
                                             (len(selected), sum(counts.values()), len(judges))):
                column.metric(label, value)
            if counts:
                chips = "".join(f'<span>{escape(category)} <b>{count}</b></span>' for category, count in counts.items())
                st.markdown('<div class="release-categories">' + chips + '</div>', unsafe_allow_html=True)
            st.markdown("#### 评分裁判")
            if not judges:
                st.caption("请选择本次使用的裁判模型。")
            for identity in judges:
                item = catalog[identity]
                ready = credential_states[identity]
                st.markdown(f'<div class="release-judge"><div><strong>{escape(item["name"])}</strong>'
                            f'<small>{escape(item["model"])}</small></div><span class="{"available" if ready else "missing"}">'
                            f'{"已读取凭据" if ready else "未读取凭据"}</span></div>', unsafe_allow_html=True)
            st.caption("凭据状态仅表示当前服务可读取，实际调用以运行结果为准。")
            st.divider()
            st.markdown("#### 自测流程")
            st.markdown('<div class="release-guide"><p><b>01</b> 连接玩具，采集真实回答</p>'
                        '<p><b>02</b> 按用例规则进行评分</p><p><b>03</b> 查看明细，下载自测报告</p></div>', unsafe_allow_html=True)
            st.caption("强制工具调用按规则校验；执行异常与评分失败单独展示。")
    with st.container(key="release-history"):
        release_history(manager, root, detail_panel, open_report, mac.strip(), presentation=presentation)
