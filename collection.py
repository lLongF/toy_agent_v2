#!/usr/bin/env python3
"""采集：OTA / WebSocket / hello / 文本收发。"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
import websockets

from config import Config, DEFAULT_FIRMWARE_VERSION, RuntimeState

def _append_assistant_visible_text(state: RuntimeState, text: Any) -> None:
    """将非空字符串追加到本轮助手回应聚合（保留原文，仅过滤全空白）。"""
    if not isinstance(text, str):
        return
    if not text.strip():
        return
    state.response_text_segments.append(text)


# 消息处理
class MessageRouter:
    """按 `type` 路由服务端文本消息。各处理函数保持轻量。"""

    def __init__(self, state: RuntimeState):
        self.state = state
        self._mcp_tasks: set[asyncio.Task[Any]] = set()

    def handle_text_message(self, message: Dict[str, Any]) -> None:
        msg_type = message.get("type", "unknown")
        handler_map = {
            "hello": self.handle_hello,
            "tts": self.handle_tts,
            "stt": self.handle_stt,
            "llm": self.handle_llm,
            "mcp": self.handle_mcp,
            "audio": self.handle_audio,
            "abort": self.handle_abort,
            "iot": self.handle_iot,
            "server": self.handle_server,
        }
        handler = handler_map.get(msg_type, self.handle_unknown)
        if msg_type == "mcp":
            task = asyncio.create_task(handler(message))
            self._mcp_tasks.add(task)
            task.add_done_callback(self._on_mcp_task_done)
        else:
            handler(message)

    def _on_mcp_task_done(self, task: "asyncio.Task[Any]") -> None:
        self._mcp_tasks.discard(task)
        self._record_mcp_task_error(task)

    def _record_mcp_task_error(self, task: "asyncio.Task[Any]") -> None:
        """异步 MCP 回包失败时保留诊断信息，避免异常被静默吞掉。"""
        try:
            task.result()
        except Exception as exc:
            self.state.router_logs.append({"type": "mcp_client_reply", "sent": False, "error": str(exc)})

    async def wait_pending_mcp(self, timeout: float = 2.0) -> None:
        """等待已调度的 MCP 回包任务完成，避免 tools/list 回复尚未发出就开始发用户话。"""
        pending = [t for t in list(self._mcp_tasks) if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=max(0.05, timeout))

    def handle_hello(self, message: Dict[str, Any]) -> None:
        self.state.session_id = str(message.get("session_id", "")) or self.state.session_id
        self.state.router_logs.append({"type": "hello", "message": message})

    def handle_tts(self, message: Dict[str, Any]) -> None:
        state = message.get("state", "")
        text = message.get("text", "")
        _append_assistant_visible_text(self.state, text)
        if state == "stop":
            self.state.response_complete_event.set()
        self.state.router_logs.append({"type": "tts", "message": message})

    def handle_stt(self, message: Dict[str, Any]) -> None:
        # STT 一般为用户侧识别结果，不并入「助手回应」聚合；需要时可查 router_logs
        self.state.router_logs.append({"type": "stt", "message": message})

    def handle_llm(self, message: Dict[str, Any]) -> None:
        text = message.get("text", "")
        if isinstance(text, str) and text.strip() and text.strip() != "😊":
            _append_assistant_visible_text(self.state, text)
        self.state.router_logs.append({"type": "llm", "message": message})

    def handle_audio(self, message: Dict[str, Any]) -> None:
        self.state.router_logs.append({"type": "audio", "message": message})

    def handle_iot(self, message: Dict[str, Any]) -> None:
        self.state.router_logs.append({"type": "iot", "message": message})

    def handle_server(self, message: Dict[str, Any]) -> None:
        self.state.router_logs.append({"type": "server", "message": message})

    async def send_mcp_payload(self, session_id: str, request_id: Any, result: Dict[str, Any]) -> bool:
        if not self.state.websocket:
            self.state.router_logs.append({"type": "mcp_client_reply", "mcp_id": request_id, "sent": False})
            return False
        reply = {
            "session_id": session_id,
            "type": "mcp",
            "payload": {"jsonrpc": "2.0", "id": request_id, "result": result},
        }
        await self.state.websocket.send(json.dumps(reply, ensure_ascii=False))
        self.state.router_logs.append({"type": "mcp_client_reply", "mcp_id": request_id, "sent": True})
        return True

    async def send_mcp_error(self, session_id: str, request_id: Any, message: str) -> bool:
        if not self.state.websocket:
            self.state.router_logs.append({"type": "mcp_client_reply", "mcp_id": request_id, "sent": False})
            return False
        reply = {
            "session_id": session_id,
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": message},
            },
        }
        await self.state.websocket.send(json.dumps(reply, ensure_ascii=False))
        self.state.router_logs.append({"type": "mcp_client_reply", "mcp_id": request_id, "sent": True})
        return True

    def execute_tool_placeholder(self, tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        tool = next((t for t in self.state.mcp_tools if t.get("name") == tool_name), None)
        if not tool:
            return {"success": False, "error": f"unknown tool: {tool_name}"}
        return {"success": True, "tool": tool_name, "arguments": tool_args, "message": "placeholder execution success"}

    async def handle_mcp(self, message: Dict[str, Any]) -> None:
        # Framework stage: parse MCP envelope and return protocol-level responses.
        payload = message.get("payload", {}) or {}
        method = payload.get("method", "")
        request_id = payload.get("id", "")
        session_id = str(message.get("session_id", "")) or self.state.session_id

        self.state.router_logs.append(
            {
                "type": "mcp",
                "message": message,
                "mcp_method": method,
                "mcp_id": request_id,
            }
        )
        if method == "initialize":
            self.state.mcp_stats["initialize_requests"] = int(self.state.mcp_stats.get("initialize_requests", 0)) + 1
            await self.send_mcp_payload(
                session_id=session_id,
                request_id=request_id,
                result={
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "text-test-client", "version": "0.0.1"},
                },
            )
            return

        if method == "tools/list":
            self.state.mcp_stats["tools_list_requests"] = int(self.state.mcp_stats.get("tools_list_requests", 0)) + 1
            self.state.mcp_stats["last_tools_list_reply_count"] = len(self.state.mcp_tools)
            await self.send_mcp_payload(
                session_id=session_id,
                request_id=request_id,
                result={"tools": self.state.mcp_tools},
            )
            return

        if method == "tools/call":
            params = payload.get("params", {}) or {}
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {}) or {}
            tool_result = self.execute_tool_placeholder(str(tool_name), tool_args)
            is_error = not tool_result.get("success", False)
            await self.send_mcp_payload(
                session_id=session_id,
                request_id=request_id,
                result={
                    "content": [{"type": "text", "text": json.dumps(tool_result, ensure_ascii=False)}],
                    "isError": is_error,
                },
            )
            return
        # 处理服务端发送的错误method的mcp消息的处理
        await self.send_mcp_error(session_id=session_id, request_id=request_id, message=f"unsupported method: {method}")

    def handle_abort(self, message: Dict[str, Any]) -> None:
        self.state.response_complete_event.set()
        self.state.router_logs.append({"type": "abort", "message": message})

    def handle_unknown(self, message: Dict[str, Any]) -> None:
        self.state.router_logs.append({"type": "unknown", "message": message})

# 协议连接：ota——>ws——>hello
class TransportClient:
    """协议骨架：OTA -> WebSocket 连接 -> hello 握手。"""

    def __init__(self, cfg: Config, state: RuntimeState, router: MessageRouter):
        self.cfg = cfg
        self.state = state
        self.router = router

    async def send_ota_request(self) -> Dict[str, Any]:
        """POST OTA 激活检查，返回 {ok, status, data, error, request}。"""
        headers = {
            "Content-Type": "application/json",
            "Device-Id": self.cfg.device_mac,
            "Client-Id": self.cfg.client_id,
        }
        firmware_version = str(getattr(self.cfg, "firmware_version", "") or DEFAULT_FIRMWARE_VERSION)
        application_name = str(getattr(self.cfg, "application_name", "") or "xiaozhi-web-test")
        payload = {
            "version": int(self.cfg.ota_version),
            "uuid": "",
            "application": {
                "name": application_name,
                "version": firmware_version,
                "compile_time": "2025-04-16 10:00:00",
                "idf_version": "4.4.3",
                "elf_sha256": "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
            },
            "token": self.cfg.token,
            "board": {
                "ip": self.cfg.board_ip,
                "mac": self.cfg.device_mac,
                "type": self.cfg.board_type,
                "mac_address": self.cfg.device_mac,
            },
        }
        request_meta = {
            "url": self.cfg.ota_url,
            "headers": dict(headers),
            "json": payload,
        }
        try:
            resp = requests.post(
                self.cfg.ota_url,
                headers=headers,
                json=payload,
                timeout=10,
            )
            body: Any = None
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            if not resp.ok:
                return {
                    "ok": False,
                    "status": resp.status_code,
                    "data": body,
                    "error": f"ota http {resp.status_code}",
                    "request": request_meta,
                }
            if not isinstance(body, dict):
                return {
                    "ok": False,
                    "status": resp.status_code,
                    "data": body,
                    "error": "ota response is not json object",
                    "request": request_meta,
                }
            return {
                "ok": True,
                "status": resp.status_code,
                "data": body,
                "error": None,
                "request": request_meta,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": None,
                "data": None,
                "error": str(exc),
                "request": request_meta,
            }

    @staticmethod
    def extract_websocket_from_ota(ota_data: Optional[Dict[str, Any]]) -> tuple[str, str]:
        ws = (ota_data or {}).get("websocket") if isinstance(ota_data, dict) else None
        if not isinstance(ws, dict):
            return "", ""
        return str(ws.get("url") or "").strip(), str(ws.get("token") or "").strip()

    async def connect_websocket(self, ws_url: str, token: str) -> bool:
        if not ws_url:
            return False

        parsed = urlparse(ws_url)
        query = parse_qs(parsed.query)
        if token:
            auth = token if token.startswith("Bearer ") else f"Bearer {token}"
            query["authorization"] = [auth]
        query["device-id"] = [self.cfg.device_mac]
        query["client-id"] = [self.cfg.client_id]
        final_url = urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query, doseq=True), parsed.fragment)
        )

        try:
            self.state.websocket = await websockets.connect(final_url)
            return True
        except Exception:
            return False

    async def send_hello_message(self) -> bool:
        if not self.state.websocket:
            return False
        hello = {
            "type": "hello",
            "device_id": self.cfg.device_mac,
            "device_name": self.cfg.device_name,
            "device_mac": self.cfg.device_mac,
            "token": self.cfg.token,
            "features": {"mcp": True},
        }
        await self.state.websocket.send(json.dumps(hello, ensure_ascii=False))
        try:
            raw = await asyncio.wait_for(self.state.websocket.recv(), timeout=5.0)
            if not isinstance(raw, str):
                return False
            msg = json.loads(raw)
            if msg.get("type") == "hello" and msg.get("session_id"):
                self.router.handle_text_message(msg)
                try:
                    self._last_hello_drain_info = await self.drain_after_hello()
                except Exception as exc:
                    self._last_hello_drain_info = {"drain_error": str(exc)}
                handshake = getattr(self, "_last_hello_drain_info", {}) or {}
                if handshake.get("saw_mcp_tools_list_request"):
                    print(
                        f"    [MCP] 工具清单已就绪，已回复 "
                        f"{int((handshake.get('mcp_stats_after') or {}).get('last_tools_list_reply_count', 0) or 0)} 个工具",
                        flush=True,
                    )
                else:
                    print(
                        "    [MCP] 警告：hello 后未收到 tools/list，第一条用例可能无法调用工具",
                        flush=True,
                    )
                return True
        except Exception:
            return False
        return False
    # 处理hello消息发送后的请求 包括initialize 以及 tools/list
    async def drain_after_hello(
        self,
        per_wait: Optional[float] = None,
        idle_rounds: Optional[int] = None,
        max_rounds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """hello 成功后等到 MCP initialize / tools/list 回完，再短暂排空，避免第一条用户话抢在工具清单之前发出。"""
        per_wait = float(self.cfg.hello_drain_per_wait if per_wait is None else per_wait)
        idle_rounds = int(self.cfg.hello_drain_idle_rounds if idle_rounds is None else idle_rounds)
        max_rounds = int(self.cfg.hello_drain_max_rounds if max_rounds is None else max_rounds)
        ready_timeout = float(getattr(self.cfg, "hello_mcp_ready_timeout", 8.0) or 8.0)
        log_start = len(self.state.router_logs)
        idle = 0
        rounds = 0
        last_msg: Optional[Dict[str, Any]] = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.5, ready_timeout)
        safety_rounds = max(max_rounds, int(ready_timeout / max(0.05, per_wait)) + idle_rounds + 2)
        tools_list_ready = False

        while rounds < safety_rounds and loop.time() < deadline:
            remaining = deadline - loop.time()
            msg = await self.recv_loop_once(timeout=min(per_wait, max(0.05, remaining)))
            rounds += 1
            if msg is None:
                idle += 1
            else:
                idle = 0
                last_msg = msg
            await self.router.wait_pending_mcp()
            if int(self.state.mcp_stats.get("tools_list_requests", 0) or 0) > 0:
                tools_list_ready = True
                if idle >= idle_rounds:
                    break
            # 尚未收到 tools/list 时不因空闲提前结束，一直等到超时。

        await self.router.wait_pending_mcp()
        new_logs = self.state.router_logs[log_start:]
        saw_tools_list = any(
            x.get("type") == "mcp" and str(x.get("mcp_method", "")) == "tools/list" for x in new_logs
        ) or tools_list_ready
        tool_names = [str(t.get("name", "")) for t in self.state.mcp_tools if t.get("name")]
        return {
            "drain_rounds": rounds,
            "last_message_type": (last_msg or {}).get("type"),
            "new_router_log_count": len(new_logs),
            "saw_mcp_tools_list_request": saw_tools_list,
            "tools_list_ready": tools_list_ready,
            "mcp_stats_after": dict(self.state.mcp_stats),
            "local_tool_catalog_size": len(self.state.mcp_tools),
            "local_tool_names": tool_names[:32],
            "tools_description_reachability": {
                "replied_tools_list_at_least_once": int(self.state.mcp_stats.get("tools_list_requests", 0)) > 0,
                "last_reply_tool_count": int(self.state.mcp_stats.get("last_tools_list_reply_count", 0)),
            },
        }

    async def recv_loop_once(self, timeout: float) -> Optional[Dict[str, Any]]:
        """接收一条消息并路由。保持框架简单。"""
        if not self.state.websocket:
            return None
        try:
            raw = await asyncio.wait_for(self.state.websocket.recv(), timeout=timeout)
            if isinstance(raw, str):
                msg = json.loads(raw)
                self.router.handle_text_message(msg)
                return msg
            return {"type": "binary", "size": len(raw)}
        except Exception:
            return None

# 文本消息收发
class TextInteractionPipeline:
    """基于 listen/manual/detect 协议的文本发送骨架。"""

    def __init__(self, state: RuntimeState, cfg: Optional[Config] = None):
        self.state = state
        self.cfg = cfg
    # 
    async def send_text_message(self, text: str) -> bool:
        if not self.state.websocket:
            return False
        payload = {"type": "listen", "mode": "manual", "state": "detect", "text": text}
        preview = " ".join(str(text).split())
        if len(preview) > 100:
            preview = preview[:100] + "..."
        print(f"    [发送] {preview}", flush=True)
        self.state.response_complete_event.clear()
        await self.state.websocket.send(json.dumps(payload, ensure_ascii=False))
        return True

    async def collect_server_response_after_send(self, transport: "TransportClient", timeout_s: float) -> tuple[str, bool]:
        """一次用户发送后采集服务端文本：先等到首句 TTS 结束，再在末句后静默约 2 秒。

        若静默期内又来新句，计时清零，收完后再静默；没有下文才进入下一题。总时长仍受 timeout_s 限制。
        """
        loop = asyncio.get_running_loop()
        recv_timeout = float(self.cfg.collect_recv_timeout) if self.cfg else 0.25
        quiet_seconds = float(getattr(self.cfg, "collect_quiet_seconds", 2.0) or 2.0)
        deadline = loop.time() + max(0.05, timeout_s)

        # Phase 1：直到首次 tts stop 或总等待上限
        while loop.time() < deadline:
            if self.state.response_complete_event.is_set():
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await transport.recv_loop_once(timeout=min(recv_timeout, remaining))

        # Phase 2：末句后再静默 quiet_seconds；中途有新包则重新计时
        last_activity = loop.time()
        while loop.time() < deadline:
            idle = loop.time() - last_activity
            if idle >= quiet_seconds:
                break
            remaining = min(recv_timeout, quiet_seconds - idle, deadline - loop.time())
            if remaining <= 0:
                break
            msg = await transport.recv_loop_once(timeout=remaining)
            if msg is not None:
                last_activity = loop.time()

        complete = self.state.response_complete_event.is_set()
        return self.state.get_full_server_response_text(), complete

