#!/usr/bin/env python3
"""MCP 采集、匹配与工具调用校验。"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from config import TestContext

# 判别文本内容拼接
def combined_text_for_mcp_no_tool_llm_judge(turn: Dict[str, Any]) -> str:
    """不要求 MCP 的 turn：给 LLM 裁判的助手侧文本，避免多字段重复拼接。"""
    c = str(turn.get("combined_server_text", "") or "").strip()
    if c:
        return c
    parts: List[str] = []
    for u in turn.get("utterances") or []:
        if not isinstance(u, dict):
            continue
        seg = str(u.get("server_text_for_judge") or u.get("server_text") or "").strip()
        if seg:
            parts.append(seg)
    return "\n".join(parts)


# MCP消息搜集，识别与转化
class MCPInspector:
    """MCP 消息采集、匹配与简要转换。"""

    def __init__(self, ctx: TestContext):
        self.ctx = ctx

    # 从日志获取mcp调用消息
    @staticmethod
    def collect_calls_from_logs(logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        for item in logs:
            if item.get("type") != "mcp":
                continue
            method = str(item.get("mcp_method", ""))
            if method != "tools/call":
                continue
            msg = item.get("message", {}) or {}
            payload = msg.get("payload", {}) or {}
            params = payload.get("params", {}) or {}
            calls.append(
                {
                    "call_id": str(payload.get("id", "")),
                    "tool_name": str(params.get("name", "")),
                    "tool_args": params.get("arguments", {}) or {},
                    "raw_message": msg,
                }
            )
        return calls

    # mcp工具调用匹配
    @staticmethod
    def match_expected_tool(expected: Optional[str], call: Dict[str, Any]) -> bool:
        if not expected or not expected.strip():
            return True
        exp = expected.strip().lower()
        tool_name = str(call.get("tool_name", "")).lower()
        tool_args_text = json.dumps(call.get("tool_args", {}), ensure_ascii=False).lower()
        return exp in tool_name or exp in tool_args_text

    # 格式化mcp调用消息 用于短期记忆中的上下文构建
    @staticmethod
    def format_calls_brief(mcp_calls: List[Dict[str, Any]]) -> str:
        if not mcp_calls:
            return "[MCP调用] 无 tools/call 详情"
        items: List[str] = []
        for c in mcp_calls:
            tname = str(c.get("tool_name", ""))
            targs = c.get("tool_args", {}) or {}
            items.append(f"{tname}({json.dumps(targs, ensure_ascii=False)})")
        return "[MCP调用] " + "; ".join(items)

    # 获取mcp调用时服务端发送的文本 用于查看服务端面对mcp指令时的实际返回
    @staticmethod
    def assistant_text_from_logs(logs: List[Dict[str, Any]]) -> str:
        """从 router_logs 片段中提取本轮可能出现的 llm/tts 文本（便于与异步 MCP 并行时对照）。"""
        chunks: List[str] = [] #用于文本拼接
        for item in logs:
            if item.get("type") == "llm":
                m = item.get("message") or {}
                t = m.get("text", "")
                if isinstance(t, str) and t.strip() and t.strip() != "😊":
                    chunks.append(t.strip())
            elif item.get("type") == "tts":
                m = item.get("message") or {}
                t = m.get("text", "")
                if isinstance(t, str) and t.strip():
                    chunks.append(t.strip())
        return "\n".join(chunks)

    # 记录mcp发送后的非mcp信息
    @staticmethod
    def mcp_non_call_events_from_logs(logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for item in logs:
            if item.get("type") != "mcp":
                continue
            method = str(item.get("mcp_method", "") or "")
            if method == "tools/call":
                continue
            out.append({"method": method or "(empty)", "mcp_id": item.get("mcp_id")})
        return out

    # 获取消息发送后的mcp调用信息
    async def collect_result_after_send(self, start_log_index: int) -> Dict[str, Any]:
        """等待本轮 MCP 工具调用及其后的完整收尾，避免工具调用串入下一用例。"""
        state = self.ctx.state
        loop = asyncio.get_running_loop()
        hard_deadline = loop.time() + self.ctx.cfg.response_collect_timeout
        no_mcp_grace_seconds = float(self.ctx.cfg.mcp_no_call_grace_seconds)
        completion_quiet_seconds = float(self.ctx.cfg.mcp_completion_quiet_seconds)
        post_stop_drain_seconds = float(self.ctx.cfg.mcp_post_stop_drain_seconds)
        no_mcp_grace_deadline: Optional[float] = None
        mcp_seen = False
        mcp_reply_confirmed = False
        mcp_reply_time: Optional[float] = None
        last_activity_time = loop.time()
        stop_reason = "timeout"

        while True:
            now = loop.time()
            new_logs = state.router_logs[start_log_index:]
            mcp_calls = self.collect_calls_from_logs(new_logs)

            if mcp_calls and not mcp_seen:
                mcp_seen = True
                tool_names = [str(call.get("tool_name", "")) for call in mcp_calls]
                print(f"    [MCP] 收到工具调用: {', '.join(tool_names)}，等待工具结果和本轮结束...", flush=True)

            if mcp_seen:
                call_ids = {str(call.get("call_id", "")) for call in mcp_calls}
                replied_ids = {
                    str(item.get("mcp_id", ""))
                    for item in new_logs
                    if item.get("type") == "mcp_client_reply" and bool(item.get("sent", False))
                }
                if call_ids and call_ids.issubset(replied_ids) and not mcp_reply_confirmed:
                    mcp_reply_confirmed = True
                    mcp_reply_time = now
                    last_activity_time = now
                    print(
                        f"    [MCP] 工具结果已发送，继续等待 tts stop 或 {completion_quiet_seconds:.1f} 秒静默...",
                        flush=True,
                    )

                if mcp_reply_confirmed:
                    if state.response_complete_event.is_set() and mcp_reply_time is not None:
                        if now - mcp_reply_time >= post_stop_drain_seconds:
                            stop_reason = "mcp_turn_completed"
                            break
                    elif now - last_activity_time >= completion_quiet_seconds:
                        stop_reason = "mcp_turn_quiet"
                        break
            elif state.response_complete_event.is_set() and no_mcp_grace_deadline is None:
                no_mcp_grace_deadline = min(hard_deadline, now + no_mcp_grace_seconds)
                print(
                    f"    [MCP] 已收到 tts stop，继续等待工具调用 {no_mcp_grace_seconds:.1f} 秒...",
                    flush=True,
                )

            effective_deadline = no_mcp_grace_deadline if no_mcp_grace_deadline is not None and not mcp_seen else hard_deadline
            remaining = effective_deadline - now
            if remaining <= 0:
                stop_reason = "response_complete_no_mcp" if no_mcp_grace_deadline is not None and not mcp_seen else "timeout"
                break

            msg = await self.ctx.transport.recv_loop_once(timeout=min(0.5, max(0.05, remaining)))
            if msg is not None:
                last_activity_time = loop.time()

        new_logs = state.router_logs[start_log_index:]
        mcp_calls = self.collect_calls_from_logs(new_logs)
        if mcp_calls and stop_reason in ("mcp_turn_completed", "mcp_turn_quiet"):
            return {
                "saw_mcp": True,
                "mcp_calls": mcp_calls,
                "timeout": False,
                "stop_reason": stop_reason,
                "mcp_reply_confirmed": True,
                "response_complete": state.response_complete_event.is_set(),
                "assistant_visible_text": state.get_full_server_response_text(),
                "assistant_text_from_logs": self.assistant_text_from_logs(new_logs),
                "mcp_non_call_events": self.mcp_non_call_events_from_logs(new_logs),
            }
        if stop_reason == "response_complete_no_mcp":
            print(
                f"    [MCP] 未调用工具：收到 tts stop 后等待 {no_mcp_grace_seconds:.1f} 秒，仍未收到 tools/call",
                flush=True,
            )
        else:
            print(
                f"    [MCP] 等待超时：{self.ctx.cfg.response_collect_timeout:.1f} 秒内未收到 tools/call",
                flush=True,
            )
        return {
            "saw_mcp": any(item.get("type") == "mcp" for item in new_logs),
            "mcp_calls": mcp_calls,
            "timeout": stop_reason == "timeout",
            "stop_reason": stop_reason,
            "mcp_reply_confirmed": mcp_reply_confirmed,
            "response_complete": state.response_complete_event.is_set(),
            "assistant_visible_text": state.get_full_server_response_text(),
            "assistant_text_from_logs": self.assistant_text_from_logs(new_logs),
            "mcp_non_call_events": self.mcp_non_call_events_from_logs(new_logs),
        }
