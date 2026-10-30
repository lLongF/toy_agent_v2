"""本地工具文件与 MCP Streamable HTTP 工具发现，仅发现，不执行远程工具。"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

from config import turn_requires_mcp


def parse_embedded_tools(text: str) -> list[dict]:
    """解析嵌入式定义，不执行代码；返回标准 MCP tools/list 条目。

    接受 JSON 列表、tools/list 的 tools/result 包装、Python 字面量
    （含历史逗号分隔条目）以及 OpenAI function 定义。空文本表示无工具。
    """
    if not isinstance(text, str):
        raise ValueError("工具定义必须为文本")
    raw = text.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        try:
            data = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            try:
                data = ast.literal_eval("[" + raw + "]")
            except (ValueError, SyntaxError) as exc:
                raise ValueError("工具定义必须为 JSON 或 Python 字面量，不能包含可执行代码") from exc
    if isinstance(data, tuple):
        data = list(data)
    if isinstance(data, dict):
        if "result" in data:
            data = data["result"]
        if isinstance(data, dict) and "tools" in data:
            data = data["tools"]
        elif isinstance(data, dict) and ("name" in data or "function" in data):
            data = [data]
    if not isinstance(data, list):
        raise ValueError("工具定义必须为工具列表")
    tools, names = [], set()
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个工具必须为对象")
        definition = item
        schema_key = "inputSchema"
        if "function" in item:
            if item.get("type", "function") != "function" or not isinstance(item["function"], dict):
                raise ValueError(f"第 {index} 个 function 定义无效")
            definition, schema_key = item["function"], "parameters"
        name = definition.get("name")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError(f"第 {index} 个工具缺少有效名称（不能含首尾空白）")
        if name in names:
            raise ValueError(f"工具名称重复：{name}")
        schema = definition.get(schema_key)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(f"工具 {name} inputSchema 必须为 object")
        description = definition.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"工具 {name} description 必须为字符串")
        names.add(name)
        tools.append({"name": name, "description": description, "inputSchema": schema})
    return tools


def validate_expected_tools(groups, tools, rules=None) -> None:
    """预检强制调用：仅 MCP调用/短期记忆支持，且必须明确指定已定义工具名。"""
    names = {tool["name"] for tool in tools}
    for group in groups:
        category = group.category
        for turn in group.turns:
            if not turn_requires_mcp(turn.expect_tool_call):
                continue
            location = f"{category} 第 {turn.sheet_row} 行"
            if (rules or {}).get("category_bases", {}).get(category, category) not in {"MCP调用", "短期记忆"}:
                raise ValueError(f"{location} 不支持强制工具调用")
            name = turn.tool_name
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"{location} 强制工具调用必须明确填写工具名称")
            if name.strip() not in names:
                raise ValueError(f"{location} 工具名称未在嵌入式定义中声明：{name.strip()}")


def validate_source(source: dict, require_credentials=True):
    if not isinstance(source, dict) or source.get("type", "local") not in {"local", "mcp_http"}:
        raise ValueError("工具来源必须为 local 或 mcp_http")
    if source.get("type") == "mcp_http":
        url = urlparse(source.get("url", ""))
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("MCP URL 必须为不含凭据、查询参数或片段的 HTTP(S) 地址")
        if source.get("protocol_version", "2025-11-25") not in {"2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"}:
            raise ValueError("MCP 协议版本不受支持，请使用页面列出的 Streamable HTTP 版本")
        key = source.get("token_env", "")
        if key and (not isinstance(key, str) or not key.isidentifier()):
            raise ValueError("MCP 鉴权配置只允许环境变量名，不允许写入真实令牌")
        if require_credentials and key and not os.getenv(key):
            raise ValueError(f"缺少 MCP 鉴权环境变量：{key}")


def discover_tools(source: dict, local_path):
    validate_source(source)
    if source.get("type", "local") == "local":
        from pathlib import Path
        tools = parse_embedded_tools(Path(local_path).read_text(encoding="utf-8")) if Path(local_path).is_file() else []
        origin = {"type": "local", "path": str(local_path)}
    else:
        tools = _remote_tools(source)
        origin = {"type": "mcp_http", "url": source["url"],
                  "protocol_version": source.get("protocol_version", "2025-11-25")}
    origin.update(read_at=datetime.now().isoformat(), count=len(tools),
                  sha256=hashlib.sha256(json.dumps(tools, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
    return tools, origin


def _remote_tools(source):
    version = source.get("protocol_version", "2025-11-25")
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": version}
    if source.get("token_env"):
        headers["Authorization"] = "Bearer " + os.environ[source["token_env"]]
    request_id = 0
    deadline = time.monotonic() + 60
    with requests.Session() as session:
        def rpc(method, params=None, notification=False):
            nonlocal request_id
            request_id += 1
            params = dict(params or {})
            if version == "2026-07-28":
                params["_meta"] = {"io.modelcontextprotocol/protocolVersion": version,
                                   "io.modelcontextprotocol/clientInfo": {"name": "toy-agent-eval", "version": "0.1.0"}}
            body = {"jsonrpc": "2.0", "method": method, "params": params}
            if not notification:
                body["id"] = request_id
            if time.monotonic() >= deadline:
                raise ValueError("MCP 工具发现超过 60 秒上限")
            with session.post(source["url"], json=body, headers={**headers, "Mcp-Method": method},
                              timeout=(10, min(20, max(1, deadline - time.monotonic()))),
                              stream=True, allow_redirects=False) as response:
                if 300 <= response.status_code < 400:
                    raise ValueError("MCP 服务重定向被拒绝，请配置最终地址")
                response.raise_for_status()
                if response.headers.get("Mcp-Session-Id") and version != "2026-07-28":
                    headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
                if notification:
                    return {}
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    event, message, size = [], None, 0
                    for raw_line in response.iter_lines(chunk_size=1):
                        if time.monotonic() >= deadline:
                            raise ValueError("MCP 工具发现超时")
                        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                        size += len(line)
                        if size > 5_000_000:
                            raise ValueError("MCP 工具清单超过大小限制")
                        if line.startswith("data:"):
                            event.append(line[5:].lstrip())
                        elif not line and event:
                            candidate = json.loads("\n".join(event))
                            event = []
                            if candidate.get("id") == request_id:
                                message = candidate
                                break
                    if message is None:
                        raise ValueError("MCP SSE 未返回对应请求结果")
                else:
                    raw = bytearray()
                    for chunk in response.iter_content(65536):
                        raw.extend(chunk)
                        if len(raw) > 5_000_000 or time.monotonic() >= deadline:
                            raise ValueError("MCP 工具清单过大或请求超时")
                    message = json.loads(raw)
                if not isinstance(message, dict) or message.get("id") != request_id or message.get("error"):
                    raise ValueError("MCP 返回无效响应或协议错误")
                if not isinstance(message.get("result"), dict):
                    raise ValueError("MCP 缺少 result 对象")
                return message["result"]

        if version != "2026-07-28":
            result = rpc("initialize", {"protocolVersion": version, "capabilities": {},
                                        "clientInfo": {"name": "toy-agent-eval", "version": "0.1.0"}})
            negotiated = result.get("protocolVersion")
            if negotiated != version:
                raise ValueError("MCP 服务不支持所配置的协议版本，请核对配置")
            rpc("notifications/initialized", notification=True)
        tools, cursors, cursor = [], set(), None
        for _ in range(100):
            result = rpc("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list):
                raise ValueError("MCP 工具列表格式无效")
            for tool in page:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"].strip():
                    raise ValueError("MCP 工具缺少名称")
                schema = tool.get("inputSchema")
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    raise ValueError("MCP 工具 inputSchema 必须为 object")
                if any(t["name"] == tool["name"] for t in tools):
                    raise ValueError("MCP 工具名称重复")
                if not isinstance(tool.get("description", ""), str):
                    raise ValueError("MCP 工具 description 必须为字符串")
                tools.append({"name": tool["name"], "description": tool.get("description", ""), "inputSchema": schema})
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("MCP 分页游标无效或循环")
            cursors.add(cursor)
        raise ValueError("MCP 分页超过上限")
