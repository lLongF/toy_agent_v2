"""Fixed Xiaozhi OTA/WS branch; no firmware downloads or physical tool execution."""
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

from collection import TransportClient


class XiaozhiTransportClient(TransportClient):
    async def send_ota_request(self):
        # Board identity and firmware metadata follow the supplied OTA example.
        headers = {"Content-Type": "application/json", "Device-Id": self.cfg.device_mac,
                   "Client-Id": self.cfg.client_id,
                   "User-Agent": "xingzhi-cube-1.54tft-wifi/1.0.1", "Accept-Language": "zh-CN"}
        payload = {"version": 2, "language": "zh-CN", "mac_address": self.cfg.device_mac,
                   "uuid": self.cfg.client_id,
                   "application": {"name": "xiaozhi", "version": "1.0.1",
                       "elf_sha256": "c8a8ecb6d6fbcda682494d9675cd1ead240ecf38bdde75282a42365a0e396033"},
                   "board": {"type": "xingzhi-cube-1.54tft-wifi", "name": "xingzhi-cube-1.54tft-wifi",
                             "ssid": "evaluation", "rssi": -55, "channel": 1,
                             "ip": self.cfg.board_ip, "mac": self.cfg.device_mac}}
        meta = {"url": self.cfg.ota_url, "headers": headers, "json": payload}
        try:
            response = requests.post(self.cfg.ota_url, headers=headers, json=payload, timeout=10)
            body = response.json()
            if not response.ok or not isinstance(body, dict):
                return {"ok": False, "status": response.status_code, "data": {},
                        "error": "小智 OTA 请求失败或响应格式无效", "request": meta}
            self.ws_version = (body.get("websocket") or {}).get("version", 1)
            if self.ws_version not in (1, 2, 3):
                raise ValueError("小智 OTA 返回不支持的 WebSocket 协议版本")
            return {"ok": True, "status": response.status_code, "data": body, "request": meta}
        except Exception as exc:
            return {"ok": False, "status": None, "data": {}, "error": str(exc), "request": meta}

    def websocket_target(self, final_url, token):
        # The base client inserts company authentication query parameters. Xiaozhi
        # uses handshake headers; retain only unrelated server-provided parameters.
        parsed = urlsplit(final_url)
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                 if k.lower() not in {"authorization", "device-id", "client-id"}]
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
        headers = {"Device-Id": self.cfg.device_mac, "Client-Id": self.cfg.client_id,
                   "Protocol-Version": str(getattr(self, "ws_version", 1))}
        if token:
            headers["Authorization"] = token if " " in token else "Bearer " + token
        return url, {"additional_headers": headers}

    def hello_payload(self):
        return {"type": "hello", "version": getattr(self, "ws_version", 1),
                "transport": "websocket", "features": {"mcp": False},
                "audio_params": {"format": "opus", "sample_rate": 16000,
                                 "channels": 1, "frame_duration": 60}}
