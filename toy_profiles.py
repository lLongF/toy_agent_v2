#!/usr/bin/env python3
"""多玩具配置：每个玩具只区分用例、工具描述、token，其余走公共固定配置。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
TOYS_DIR = PROJECT_ROOT / "toys"  # 每个子目录是一个玩具
DEFAULT_TOY_ID = "default"  # 不传 --toy 时使用
TEMPLATE_TOY_ID = "_template"  # 复制用模板，不会出现在 --list-toys
CASES_FILENAME = "测试样例.xlsx"  # 玩具目录内默认用例文件名
TOOLS_FILENAME = "工具描述.txt"  # 玩具目录内默认工具清单文件名


@dataclass
class ToyProfile:
    """单个玩具在测试侧仅有的差异：用例表、工具描述、token。"""

    id: str
    root: Path
    cases_xlsx: Path
    mcp_tools_file: Path
    token: str = ""

    def resolve_token(self, cli_token: Optional[str] = None) -> str:
        """令牌优先级：命令行 > 环境变量 TOY_AGENT_TOKEN_<ID> > TOY_AGENT_TOKEN > toy.json。"""
        if cli_token and str(cli_token).strip():
            return str(cli_token).strip()
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
        path = (toy_root / path).resolve()
    return path


def load_toy_profile(toy_id: str, toys_dir: Optional[Path] = None) -> ToyProfile:
    """按目录名加载 toys/<id>/toy.json。"""
    root_dir = toys_dir or TOYS_DIR
    toy_id = (toy_id or DEFAULT_TOY_ID).strip() or DEFAULT_TOY_ID
    if toy_id.startswith(".") or "/" in toy_id or "\\" in toy_id:
        raise ValueError(f"invalid toy id: {toy_id}")
    toy_root = (root_dir / toy_id).resolve()
    config_path = toy_root / "toy.json"
    if not config_path.is_file():
        known = ", ".join(p.id for p in list_toy_profiles(root_dir)) or "(none)"
        raise FileNotFoundError(
            f"toy not found: {toy_id} (expected {config_path}). available: {known}"
        )
    data = _read_json(config_path)
    return ToyProfile(
        id=_as_str(data, "id", toy_id) or toy_id,
        root=toy_root,
        cases_xlsx=_resolve_data_path(toy_root, _as_str(data, "cases_xlsx"), CASES_FILENAME),
        mcp_tools_file=_resolve_data_path(toy_root, _as_str(data, "mcp_tools_file"), TOOLS_FILENAME),
        token=_as_str(data, "token"),
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
