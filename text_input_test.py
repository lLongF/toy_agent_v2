#!/usr/bin/env python3
"""入口：按玩具采集回复，再用公共双裁判打分。

模块划分：
- collection.py  采集（OTA / WebSocket / hello / 文本收发）
- mcp.py         MCP 工具清单回复与调用校验
- testers.py     按 Excel 类型采集（基础问答、知识库单轮等）
- judge.py       Qwen + DeepSeek 裁判
- report.py      JSON / Markdown / Excel 报告
- cli.py         命令行
- config.py      全仓库固定配置（各玩具只换用例、工具、token）
"""
from __future__ import annotations

import asyncio
import json
import sys
import hashlib
from uuid import uuid4
from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, List, Optional

from cli import parse_args
from collection import MessageRouter, TextInteractionPipeline, TransportClient
from config import (
    Config,
    RuntimeState,
    TestContext,
    default_fallback_mcp_tools,
    dialogue_group_to_dict,
    load_dialogue_groups_from_xlsx,
    load_mcp_tools_from_txt,
    summarize_dialogue_groups,
)
from judge import build_judge_endpoints, post_judge_results
from evaluation_support import filter_groups, load_rules, validate_case_targets
from tools_source import discover_tools
from execution_lock import exclusive_device
from safe_output import SafeLog
from report import ReportWriter
from testers import ConversationTestDispatcher, build_test_handlers
from run_progress import update_progress


@exclusive_device
async def run_framework(cfg: Config) -> Dict[str, Any]:
    """按测试类型隔离 WebSocket 会话：同一类型内保留上下文，换类型则重建连接。"""
    cfg = replace(cfg, rules_snapshot=cfg.rules_snapshot or load_rules(cfg), run_id=cfg.run_id or uuid4().hex[:12])
    from connection_scenes import validate_scene_cases, validate_connection_mode
    validate_connection_mode(cfg)
    is_xiaozhi = cfg.connection_scene == "xiaozhi"
    transport_class = TransportClient
    if is_xiaozhi:
        from xiaozhi_connection import XiaozhiTransportClient
        transport_class = XiaozhiTransportClient
    loaded, tools_origin = ([], {"type": "disabled", "reason": "xiaozhi_text_only"}) if is_xiaozhi else discover_tools(cfg.tools_source, cfg.mcp_tools_file)
    if cfg.tools_source.get("type") == "mcp_http" and not loaded:
        raise ValueError("远程 MCP 工具清单为空，已阻止评测；不会静默回退本地工具")
    mcp_tools = [] if is_xiaozhi else (loaded if loaded else default_fallback_mcp_tools())
    mcp_tools_source = tools_origin.get("url") or str(cfg.mcp_tools_file)
    if is_xiaozhi:
        mcp_tools_source = "disabled_text_only"
    elif not loaded:
        mcp_tools_source = "fallback_ping_only"
    writer = ReportWriter(cfg.reports_dir, sensitive_values=[cfg.token, cfg.api_key, cfg.deepseek_api_key])

    if not cfg.cases_xlsx.is_file():
        raise FileNotFoundError(f"cases xlsx not found: {cfg.cases_xlsx}")
    source_cases = cfg.case_snapshot or cfg.cases_xlsx
    dialogue_groups = load_dialogue_groups_from_xlsx(source_cases)
    from evaluation_selection import selected_groups, validate_capabilities, scoped_rules
    from tools_source import validate_expected_tools
    cfg = replace(cfg, rules_snapshot=scoped_rules(cfg, cfg.rules_snapshot))
    validate_case_targets(cfg.rules_snapshot, cfg.toy_id, dialogue_groups)
    dialogue_groups = selected_groups(cfg, source_cases)
    validate_scene_cases(cfg, dialogue_groups, cfg.rules_snapshot)
    validate_capabilities(cfg, dialogue_groups)
    # 原 CLI 缺文件 ping 回退保留；明确配置的定义必须匹配实际预期。
    if loaded or cfg.tools_source:
        validate_expected_tools(dialogue_groups, mcp_tools, cfg.rules_snapshot)
    needs_judge = any(cfg.rules_snapshot.get("category_bases", {}).get(g.category, g.category) != "MCP调用"
                      or any(not t.expect_tool_call for t in g.turns) for g in dialogue_groups)
    if cfg.selected_judge_ids is not None and not cfg.judge_endpoints_snapshot:
        from judge_store import snapshot_judge_models
        cfg = replace(cfg, judge_endpoints_snapshot=snapshot_judge_models(cfg.selected_judge_ids, cfg=cfg))
    active_endpoints = build_judge_endpoints(cfg) if needs_judge else []
    if needs_judge and not active_endpoints:
        raise ValueError("文本评分至少选择一个裁判模型")
    writer.sensitive_values.extend(endpoint.api_key for endpoint in active_endpoints)
    precheck_summary = summarize_dialogue_groups(dialogue_groups)

    protocol_info = {
        "schema_version": "2",
        "connection_scene": cfg.connection_scene,
        "connection_mode": cfg.connection_mode,
        "release_environment": cfg.release_environment,
        "target_name": "小智 AI" if is_xiaozhi else cfg.toy_snapshot.get("name", cfg.toy_id),
        "client_id": cfg.client_id,
        "ota_url": cfg.ota_url,
        "dataset_snapshot": cfg.dataset_snapshot,
        "toy_snapshot": cfg.toy_snapshot,
        "environment": cfg.environment,
        "judge_endpoints_snapshot": cfg.judge_endpoints_snapshot,
        "selected_judge_ids": cfg.selected_judge_ids,
        "tool_execution_mode": "disabled" if is_xiaozhi else "simulated_device_placeholder",
        "started_at": datetime.now().isoformat(),
        "run_id": cfg.run_id,
        "trigger_type": cfg.trigger_type,
        "selection": {"categories": [g.category for g in dialogue_groups],
                      "case_rows": [t.sheet_row for g in dialogue_groups for t in g.turns],
                      "case_ids": [t.case_id for g in dialogue_groups for t in g.turns]},
        "selected_case_specs": [dialogue_group_to_dict(g) for g in dialogue_groups],
        "evaluation_rules": cfg.rules_snapshot,
        "ota_called": False,
        "ota_skipped": False,
        "ota_ok": False,
        "ws_connected": False,
        "hello_ok": False,
        "receive_loop_enabled": True,
        "ws_mode": "fresh-per-category",
        "session_policy": "先请求 OTA，再用返回的 websocket.url/token 连接；每个测试类型独立建立、执行并关闭一个 WebSocket 会话。",
        "ws_url": cfg.ws_url,
        "device_mac": cfg.device_mac,
        "device_mac_random": bool(cfg.device_mac_random),
        "toy_id": cfg.toy_id,
        "cases_xlsx": str(cfg.cases_xlsx),
        "cases_sha256": hashlib.sha256(source_cases.read_bytes()).hexdigest(),
        "mcp_tools_file": str(cfg.mcp_tools_file),
        "mcp_tools_sha256": hashlib.sha256(cfg.mcp_tools_file.read_bytes()).hexdigest() if cfg.mcp_tools_file.is_file() else None,
        "mcp_tools_read_at": datetime.now().isoformat(),
        "mcp_tools_source": mcp_tools_source,
        "tools_origin": tools_origin,
        "mcp_tools_loaded_count": len(mcp_tools),
        "dialogue_group_count": len(dialogue_groups),
        "judge_models": [
            {"name": ep.name, "model": ep.model, "base_url": ep.base_url}
            for ep in active_endpoints
        ],
        "data_precheck": precheck_summary,
        "ws_stats": {"session_count": 0, "connect_failed": 0, "hello_failed": 0},
    }

    results: List[Dict[str, Any]] = []
    run_error: Optional[str] = None
    ws_url = cfg.ws_url
    ws_token = cfg.token
    update_progress(cfg, "连接准备")
    try:
        ota_state = RuntimeState(mcp_tools=list(mcp_tools))
        ota_transport = transport_class(cfg, ota_state, MessageRouter(ota_state))
        print(f"接入场景: {'小智 AI' if is_xiaozhi else '公司玩具'}  Client-Id={cfg.client_id}", flush=True)
        print(
            f"玩具: {cfg.toy_id}  Device-Id={cfg.device_mac}",
            flush=True,
        )
        if cfg.connection_mode == "direct":
            protocol_info["ota_skipped"] = True
            protocol_info["session_policy"] = "直连指定 WebSocket，不请求 OTA；每类用例独立会话。"
            print("直连 WebSocket：使用本次填写的地址与 Token，跳过 OTA。", flush=True)
        else:
            print(f"请求 OTA: {cfg.ota_url}  Device-Id={cfg.device_mac}", flush=True)
            ota_result = await ota_transport.send_ota_request()
            protocol_info["ota_called"] = True
            protocol_info["ota_http_status"] = ota_result.get("status")
            protocol_info["ota_error"] = ota_result.get("error")
            protocol_info["ota_request"] = ota_result.get("request")
            ota_data = ota_result.get("data") if isinstance(ota_result.get("data"), dict) else {}
            if is_xiaozhi:
                activation = ota_data.get("activation") or {}
                protocol_info["ota_activation"] = {"present": bool(activation),
                    "has_code": bool(activation.get("code")) if isinstance(activation, dict) else False,
                    "has_challenge": bool(activation.get("challenge")) if isinstance(activation, dict) else False}
                print(f"小智 OTA 激活信息: present={bool(activation)}（仅记录状态，不输出激活码）", flush=True)
                # MQTT credentials/activation challenges are not used in text evaluation.
                ota_data = {k: v for k, v in ota_data.items() if k in {"websocket", "server_time"}}
                if not ota_data.get("websocket") and (ota_result.get("data") or {}).get("activation"):
                    ota_result["error"] = "小智 OTA 尚未提供可用 WS 地址，请检查激活状态"
            protocol_info["ota_response"] = ota_data
            ota_ws_url, ota_ws_token = TransportClient.extract_websocket_from_ota(ota_data)
            if is_xiaozhi and ota_ws_url:
                from urllib.parse import urlsplit
                address = urlsplit(ota_ws_url)
                if address.scheme not in {"ws", "wss"} or not address.hostname or address.username or address.password:
                    ota_ws_url = ""
                    ota_result["error"] = "小智 OTA 返回无效的 WebSocket 地址"
            protocol_info["ota_error"] = ota_result.get("error")
            protocol_info["ota_ok"] = bool(ota_result.get("ok") and ota_ws_url)
            if ota_ws_url:
                ws_url = ota_ws_url
                protocol_info["ws_url"] = ws_url
            if ota_ws_token:
                ws_token = ota_ws_token
                writer.sensitive_values.append(ota_ws_token)
                if isinstance(sys.stdout, SafeLog):
                    sys.stdout.secrets = [*sys.stdout.secrets, ota_ws_token]
            print(
                f"OTA 结果: ok={protocol_info['ota_ok']} status={ota_result.get('status')} "
                f"websocket.url={ws_url or '(empty)'}",
                flush=True,
            )
            if not protocol_info["ota_ok"]:
                run_error = str(ota_result.get("error") or "ota failed: missing websocket.url")
                protocol_info["run_aborted"] = run_error
                dialogue_groups = []
            else:
                cfg.ws_url = ws_url
                cfg.token = ws_token

        total_groups = len(dialogue_groups)
        update_progress(cfg, "采集会话", 0, total_groups)
        for group_index, group in enumerate(dialogue_groups, start=1):
            print(
                f"\n[{group_index}/{total_groups}] 开始类型会话：{group.category}，"
                f"共 {len(group.turns)} 轮（新建 WS）",
                flush=True,
            )
            state = RuntimeState(mcp_tools=list(mcp_tools))
            router = MessageRouter(state, tts_sentence_only=is_xiaozhi)
            transport = transport_class(cfg, state, router)
            if is_xiaozhi:
                transport.ws_version = getattr(ota_transport, "ws_version", 1)
            pipeline = TextInteractionPipeline(state, cfg)
            ctx = TestContext(cfg=cfg, state=state, transport=transport, pipeline=pipeline)
            protocol_info["ws_stats"]["session_count"] += 1

            try:
                connected = await transport.connect_websocket(cfg.ws_url, cfg.token)
                protocol_info["ws_connected"] = bool(protocol_info["ws_connected"] or connected)
                if not connected:
                    protocol_info["ws_stats"]["connect_failed"] += 1
                    group_result = {
                        "handler": "default",
                        "status": "skipped",
                        "reason": getattr(transport, "last_connect_error", "") or "websocket not connected",
                        "turn_results": [],
                        "group_pass": False,
                    }
                else:
                    hello_ok = await transport.send_hello_message()
                    protocol_info["hello_ok"] = bool(protocol_info["hello_ok"] or hello_ok)
                    handshake = getattr(transport, "_last_hello_drain_info", None)
                    if isinstance(handshake, dict):
                        protocol_info["mcp_post_hello_handshake"] = handshake
                    if not hello_ok:
                        protocol_info["ws_stats"]["hello_failed"] += 1
                        group_result = {
                            "handler": "default",
                            "status": "skipped",
                            "reason": "hello failed",
                            "turn_results": [],
                            "group_pass": False,
                        }
                    else:
                        handlers, default_handler = build_test_handlers(ctx)
                        dispatcher = ConversationTestDispatcher(handlers, default_handler)
                        group_result = await dispatcher.run_group(ctx, group)
            except Exception:
                if is_xiaozhi:
                    protocol_info["failed_session"] = {
                        "category": group.category,
                        "received_events": [{"type": item.get("type"),
                                             "state": (item.get("message") or {}).get("state")}
                                            for item in state.router_logs[-30:]],
                        "partial_text": state.get_full_server_response_text()}
                    print(f"小智会话中断，收到 {len(state.router_logs)} 条协议事件；诊断已保存至报告。", flush=True)
                raise
            finally:
                if state.websocket:
                    try:
                        await state.websocket.close()
                    except Exception:
                        pass

            group_result.setdefault("category", group.category)
            group_result.setdefault("group_id", group.group_id)
            group_result.setdefault("case_spec", dialogue_group_to_dict(group))
            tools_list_request_count = int(state.mcp_stats.get("tools_list_requests", 0) or 0)
            tools_list_received = tools_list_request_count > 0
            returned_tool_count = (
                int(state.mcp_stats.get("last_tools_list_reply_count", 0) or 0)
                if tools_list_received
                else 0
            )
            returned_tool_names = (
                [str(tool.get("name", "")) for tool in state.mcp_tools if str(tool.get("name", "")).strip()]
                if tools_list_received
                else []
            )
            group_result["mcp_tools_list"] = {
                "received": tools_list_received,
                "request_count": tools_list_request_count,
                "returned_tool_count": returned_tool_count,
                "returned_tool_names": returned_tool_names,
            }
            results.append(group_result)
            update_progress(cfg, "采集会话", group_index, total_groups)
            turn_count = len(group_result.get("turn_results") or [])
            print(
                f"[{group_index}/{total_groups}] 采集完成："
                f"handler={group_result.get('handler')}，turns={turn_count}，"
                f"status={group_result.get('status')}",
                flush=True,
            )
            if cfg.send_interval > 0:
                await asyncio.sleep(cfg.send_interval)
    except Exception as exc:
        run_error = str(exc)
        protocol_info["run_aborted"] = run_error
        protocol_info["completed_groups_before_abort"] = len(results)
    protocol_info["judge_phase"] = "post_collection"
    protocol_info["judge_started_at"] = datetime.now().isoformat()
    judge_summary = post_judge_results(results, cfg)
    protocol_info["judge_summary"] = judge_summary
    protocol_info["finished_at"] = datetime.now().isoformat()
    execution_failed = bool(run_error or len(results) != protocol_info["dialogue_group_count"]
                            or any(g.get("status") != "completed" or any(t.get("execution_error") for t in g.get("turn_results", [])) for g in results))
    protocol_info["execution_status"] = "failed" if execution_failed else "completed"
    update_progress(cfg, "生成报告")
    report_paths = writer.write(
        results, protocol_info, cases_xlsx=source_cases, export_xlsx=cfg.export_xlsx
    )
    out = {"protocol_info": protocol_info, "report_paths": report_paths, "result_count": len(results),
           "execution_status": protocol_info["execution_status"]}
    if run_error:
        out["error"] = run_error
    return out


def main() -> None:
    cfg, do_run = parse_args()
    sys.stdout = SafeLog(sys.stdout, [cfg.token, cfg.api_key, cfg.deepseek_api_key])
    if do_run:
        result = asyncio.run(run_framework(cfg))
        print(json.dumps(result.get("report_paths", {}), ensure_ascii=False, indent=2))
        if result.get("protocol_info"):
            pi = result["protocol_info"]
            probe = pi.get("mcp_post_hello_handshake") or {}
            print(
                "MCP 握手探测:",
                json.dumps(
                    {
                        "saw_mcp_tools_list_request": probe.get("saw_mcp_tools_list_request"),
                        "tools_description_reachability": probe.get("tools_description_reachability"),
                        "mcp_stats_after": probe.get("mcp_stats_after"),
                        "local_tool_catalog_size": probe.get("local_tool_catalog_size"),
                    },
                    ensure_ascii=False,
                ),
            )
        if result.get("execution_status") == "failed":
            raise SystemExit(1)
        return
    print("文本测试框架已就绪。")
    print(f"玩具: {cfg.toy_id}")
    print(f"用例文件: {cfg.cases_xlsx}")
    print(f"工具描述: {cfg.mcp_tools_file}")
    print("列出玩具: python text_input_test.py --list-toys")
    print("执行完整流水线: python text_input_test.py --toy <id> --run [...]")
    print("查看命令行帮助: python text_input_test.py --help")


if __name__ == "__main__":
    main()
