"""Browser bridge for local use or an HTTPS reverse proxy; upstream stays official."""
import asyncio
import base64
import json
import os
import secrets
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from websockets.asyncio.server import serve
from config import RuntimeState
from collection import MessageRouter
from connection_scenes import XIAOZHI_OTA_URL, XIAOZHI_DEVICE_ID, XIAOZHI_CLIENT_ID
from execution_lock import device_lock
from xiaozhi_connection import XiaozhiTransportClient


def normalise_origin(value):
    """Canonical HTTP origin, without paths, credentials or wildcard matching."""
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or any(c.isspace() for c in value) or "\\" in value):
        raise ValueError("XIAOZHI_CHAT_PUBLIC_ORIGIN 必须为完整 HTTP(S) 域名来源，不含路径或凭据")
    port = parsed.port  # Invalid ports fail before starting the listener.
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    suffix = "" if port is None or port == {"http": 80, "https": 443}[parsed.scheme] else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


class ChatBridge:
    def __init__(self):
        configured_origin = os.getenv("XIAOZHI_CHAT_PUBLIC_ORIGIN", "").strip()
        self.public_origin = normalise_origin(configured_origin) if configured_origin else ""
        self.listen_port = int(os.getenv("XIAOZHI_CHAT_PORT", "8766" if self.public_origin else "0"))
        if not 0 <= self.listen_port <= 65535:
            raise ValueError("XIAOZHI_CHAT_PORT 必须为 0–65535 的端口")
        self.ticket = secrets.token_urlsafe(32)
        self.ready = threading.Event()
        self.error = None
        threading.Thread(target=self._thread, daemon=True).start()
        if not self.ready.wait(5) or self.error:
            raise RuntimeError("小智桥接启动失败，请检查桥接端口是否被占用")

    def _thread(self):
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self.error = type(exc).__name__
            self.ready.set()

    async def _serve(self):
        # Only the reverse proxy is public. The raw bridge always stays on loopback.
        async with serve(self.handle, "127.0.0.1", self.listen_port, max_size=2**20) as server:
            self.port = server.sockets[0].getsockname()[1]
            self.ready.set()
            await asyncio.Future()

    async def handle(self, browser):
        try:
            origin = normalise_origin(browser.request.headers.get("Origin", ""))
            local = urlsplit(origin).hostname in {"127.0.0.1", "localhost"}
            allowed = local or (bool(self.public_origin) and origin == self.public_origin)
        except ValueError:
            allowed = False
        if not secrets.compare_digest(browser.request.path, "/" + self.ticket) or not allowed:
            await browser.close(1008, "Unauthorized")
            return
        upstream = None
        try:
            first = json.loads(await asyncio.wait_for(browser.recv(), 10))
            if first.get("type") != "hello":
                raise ValueError("Expected hello")
            with device_lock(XIAOZHI_DEVICE_ID):
                cfg = SimpleNamespace(ota_url=XIAOZHI_OTA_URL, device_mac=XIAOZHI_DEVICE_ID,
                                      client_id=XIAOZHI_CLIENT_ID, board_ip="192.168.1.1")
                state = RuntimeState()
                transport = XiaozhiTransportClient(cfg, state, MessageRouter(state))
                ota = await asyncio.to_thread(lambda: asyncio.run(transport.send_ota_request()))
                url, token = transport.extract_websocket_from_ota(ota.get("data"))
                if not ota.get("ok") or not url:
                    raise ValueError("小智 OTA 未提供连接配置，请检查设备激活状态")
                # Raw browser Opus packets use protocol version 1, without binary headers.
                transport.ws_version = 1
                if not await transport.connect_websocket(url, token):
                    raise ValueError("小智 WebSocket 连接失败")
                upstream = state.websocket
                await upstream.send(json.dumps(transport.hello_payload()))
                hello = json.loads(await asyncio.wait_for(upstream.recv(), 10))
                if hello.get("type") != "hello" or not hello.get("session_id"):
                    raise ValueError("小智握手失败")
                await browser.send(json.dumps(hello))

                async def send():
                    async for message in browser:
                        if isinstance(message, str):
                            item = json.loads(message)
                            if item.get("type") not in {"listen", "abort"}:
                                continue
                            item["session_id"] = hello["session_id"]
                            if item.get("state") == "detect":
                                item["mode"] = "manual"
                            message = json.dumps(item)
                        await upstream.send(message)

                async def receive():
                    async for message in upstream:
                        await browser.send(message)

                tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
                try:
                    done, _ = await asyncio.wait(tasks, timeout=4*3600, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            # Do not expose OTA tokens, upstream URLs or SDK exception payloads.
            await browser.close(1011, "小智连接中断，请检查设备激活或稍后重新唤醒")
        finally:
            if upstream is not None:
                await upstream.close()

    def html(self):
        source = (Path(__file__).parent / "templates/xiaozhi_chat.html").read_text(encoding="utf-8")
        icon = (Path(__file__).parent / "templates/xiaozhi_logo.svg").read_bytes()
        icon_url = "data:image/svg+xml;base64," + base64.b64encode(icon).decode("ascii")
        if self.public_origin:
            endpoint = self.public_origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
            endpoint += f"/xiaozhi-ws/{self.ticket}"
        else:
            endpoint = f"ws://127.0.0.1:{self.port}/{self.ticket}"
        return source.replace("__XIAOZHI_ICON__", icon_url).replace("__BRIDGE_URL__", json.dumps(endpoint))
