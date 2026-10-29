#!/usr/bin/env python3
"""多玩具配置：每个玩具只区分用例、工具描述、token，其余走公共固定配置。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
TOYS_DIR = PROJECT_ROOT / "toys"  # 每个子目录是一个玩具
DEFAULT_TOY_ID = "default"  # 不传 --toy 时使用
TEMPLATE_TOY_ID = "_template"  # 复制用模板，不会出现在 --list-toys
CASES_FILENAME = "测试样例.xlsx"  # 玩具目录内默认用例文件名
TOOLS_FILENAME = "工具描述.txt"  # 玩具目录内默认工具清单文件名
ALL_CAPABILITIES = {key: "supported" for key in (
    "text", "context", "knowledge_base", "tool_injection", "tool_observation")}


@dataclass
class ToyProfile:
    """单个玩具在测试侧仅有的差异：用例表、工具描述、token。"""

    id: str
    root: Path
    cases_xlsx: Path
    mcp_tools_file: Path
    token: str = ""
    tools_source: dict = field(default_factory=dict)
    name: str = ""
    enabled: bool = True
    environments: dict = field(default_factory=dict)
    capabilities: Any = None
    credential_ref: str = ""

    @property
    def configured(self) -> bool:
        return self.capabilities is not None

    def resolve_token(self, cli_token: Optional[str] = None) -> str:
        """令牌优先级：命令行 > 环境变量 TOY_AGENT_TOKEN_<ID> > TOY_AGENT_TOKEN > toy.json。"""
        if cli_token and str(cli_token).strip():
            return str(cli_token).strip()
        if self.credential_ref.startswith("local:"):
            from local_credentials import read_token
            return read_token(self.credential_ref, self.root.parent.parent)
        if self.credential_ref:
            value = (os.getenv(self.credential_ref) or "").strip()
            if not value:
                raise ValueError("玩具显式凭据引用的环境变量未配置")
            return value
        env_id = self.id.upper().replace("-", "_")
        for key in (f"TOY_AGENT_TOKEN_{env_id}", "TOY_AGENT_TOKEN"):
            env_val = (os.getenv(key) or "").strip()
            if env_val:
                return env_val
        return (self.token or "").strip()


def _read_json(path: Path) -> Dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"toy config must be a JSON object: {path}")
    return raw


def _as_str(data: Dict[str, Any], key: str, default: str = "") -> str:
    val = data.get(key, default)
    if val is None:
        return default
    return str(val).strip()


def _resolve_data_path(toy_root: Path, raw: str, fallback_name: str) -> Path:
    text = (raw or "").strip() or fallback_name
    path = Path(text)
    if not path.is_absolute():
        path = safe_path(toy_root / path, toy_root.parent.parent)
    return path


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("invalid id")
    if value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("reserved id")
    return value


def safe_path(path: Path, root: Path) -> Path:
    path, root = Path(path).resolve(), Path(root).resolve()
    if not path.is_relative_to(root):
        raise ValueError("路径必须在项目目录内")
    return path


ENVIRONMENT_FIELDS = frozenset(("ota_url", "ws_url", "device_mac", "client_id", "device_name", "board_ip", "board_type", "firmware_version"))


def validate_management_fields(data):
    if not isinstance(data.get("enabled", True), bool):
        raise ValueError("enabled must be boolean")
    ref = data.get("credential_ref", "")
    if not isinstance(ref, str) or (ref and not re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]*|local:[a-f0-9]{32})", ref)):
        raise ValueError("credential_ref 必须为环境变量名")
    environments = data.get("environments", {})
    if not isinstance(environments, dict):
        raise ValueError("environments must be an object")
    for name, values in environments.items():
        validate_id(name)
        if not isinstance(values, dict) or set(values) - (ENVIRONMENT_FIELDS | {"credential_ref", "enabled"}):
            raise ValueError("环境只允许设备接口、启用状态及凭据引用")
        if not isinstance(values.get("enabled", True), bool):
            raise ValueError("环境 enabled must be boolean")
        env_ref = values.get("credential_ref", "")
        if not isinstance(env_ref, str) or (env_ref and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_ref)):
            raise ValueError("环境 credential_ref 必须为环境变量名")
        if any(not isinstance(values[k], str) for k in values if k in ENVIRONMENT_FIELDS):
            raise ValueError("环境接口字段必须为字符串")
        from urllib.parse import urlsplit
        for key in ("ota_url", "ws_url"):
            if values.get(key):
                url = urlsplit(values[key])
                schemes = ("http", "https") if key == "ota_url" else ("ws", "wss")
                if url.scheme not in schemes or not url.hostname or url.username or url.password or url.query or url.fragment:
                    raise ValueError("环境 URL 格式无效或含凭据/查询参数")
    caps = data.get("capabilities")
    if caps is not None and not isinstance(caps, (dict, list)):
        raise ValueError("capabilities must be an object or list")
    if isinstance(caps, dict) and any(not isinstance(k, str) or (not isinstance(v, bool) and v not in ("supported", "unsupported", "unverified")) for k, v in caps.items()):
        raise ValueError("capabilities values must be boolean or supported/unsupported/unverified")
    if isinstance(caps, list) and any(not isinstance(v, str) for v in caps):
        raise ValueError("capabilities must contain strings")


def apply_toy_environment(cfg, toy: ToyProfile, environment: str = ""):
    """Return an independent config, rejecting disabled toys and unknown environments."""
    if not toy.enabled:
        raise ValueError("玩具已禁用")
    validate_management_fields(vars(toy))
    values = {}
    if environment:
        if environment not in toy.environments:
            raise ValueError("玩具环境不存在")
        env_values = toy.environments[environment]
        if not env_values.get("enabled", True):
            raise ValueError("玩具环境已禁用")
        values.update({k: v for k, v in env_values.items() if k in ENVIRONMENT_FIELDS})
    else:
        env_values = {}
    effective_toy = replace(toy, credential_ref=env_values.get("credential_ref", toy.credential_ref))
    caps = toy.capabilities
    normalized_caps = ({key: "supported" for key in caps} if isinstance(caps, list) else
                       {key: ("supported" if value else "unsupported") if isinstance(value, bool) else value
                        for key, value in (caps or {}).items()})
    values.update(toy_id=toy.id, environment=environment,
                  token=effective_toy.resolve_token(getattr(cfg, "token", "")),
                  toy_snapshot={"id": toy.id, "name": toy.name or toy.id,
                                "enabled": toy.enabled, "environment": environment,
                                "capabilities": normalized_caps, "configured": toy.configured})
    return replace(cfg, **values)


def load_toy_profile(toy_id: str, toys_dir: Optional[Path] = None) -> ToyProfile:
    """按目录名加载 toys/<id>/toy.json。"""
    root_dir = Path(toys_dir or TOYS_DIR).resolve()
    toy_id = validate_id((toy_id or DEFAULT_TOY_ID).strip() or DEFAULT_TOY_ID)
    toy_root = safe_path(root_dir / toy_id, root_dir)
    config_path = safe_path(toy_root / "toy.json", root_dir)
    if not config_path.is_file():
        known = ", ".join(p.id for p in list_toy_profiles(root_dir)) or "(none)"
        raise FileNotFoundError(
            f"toy not found: {toy_id} (expected {config_path}). available: {known}"
        )
    data = _read_json(config_path)
    if data.get("id", toy_id) != toy_id:
        raise ValueError("玩具 id 必须与目录名一致")
    validate_management_fields(data)
    if not isinstance(data.get("tools_source", {}), dict):
        raise ValueError(f"tools_source must be a JSON object: {config_path}")
    return ToyProfile(
        id=_as_str(data, "id", toy_id) or toy_id,
        root=toy_root,
        # 兼容旧档案显式绝对资产路径；页面写入仍受管理层项目根校验。
        cases_xlsx=_resolve_data_path(toy_root, _as_str(data, "cases_xlsx"), CASES_FILENAME),
        mcp_tools_file=_resolve_data_path(toy_root, _as_str(data, "mcp_tools_file"), TOOLS_FILENAME),
        token=_as_str(data, "token"),
        tools_source=data.get("tools_source") or {},
        name=_as_str(data, "name", toy_id) or toy_id,
        enabled=data.get("enabled", True),
        environments=data.get("environments", {}),
        capabilities=data.get("capabilities"),
        credential_ref=_as_str(data, "credential_ref"),
    )


def list_toy_profiles(toys_dir: Optional[Path] = None) -> List[ToyProfile]:
    root_dir = toys_dir or TOYS_DIR
    if not root_dir.is_dir():
        return []
    out: List[ToyProfile] = []
    for child in sorted(root_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith(".") or child.name == TEMPLATE_TOY_ID:
            continue
        if not (child / "toy.json").is_file():
            continue
        try:
            out.append(load_toy_profile(child.name, root_dir))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return out


def format_toy_catalog(toys_dir: Optional[Path] = None) -> str:
    profiles = list_toy_profiles(toys_dir)
    if not profiles:
        return "未找到玩具配置。请在 toys/<id>/ 放入 toy.json、测试样例.xlsx、工具描述.txt。"
    lines = ["可用玩具："]
    for p in profiles:
        lines.append(f"- {p.id}")
        lines.append(f"    用例: {p.cases_xlsx}")
        lines.append(f"    工具: {p.mcp_tools_file}")
    lines.append("")
    lines.append("新增玩具：复制 toys/_template/ 为 toys/<id>/，写入 token，放入该玩具的用例和工具描述。")
    return "\n".join(lines)
