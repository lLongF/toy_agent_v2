"""OpenAI-compatible judge registry. Persist references, never plaintext credentials."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

from toy_management import atomic_json, file_hash
from toy_profiles import PROJECT_ROOT, safe_path

_FIELDS = {"id", "name", "model", "base_url", "credential_ref", "enabled", "extra_body"}


def registry_path(root=None):
    root = Path(root or PROJECT_ROOT).resolve()
    return safe_path(root / "settings" / "judge_models.json", root)


def registry_hash(root=None):
    return file_hash(registry_path(root))


def validate_judge_model(item):
    """Return an isolated canonical dict; errors deliberately omit field values."""
    if not isinstance(item, dict) or set(item) - _FIELDS:
        raise ValueError("裁判配置包含未知字段或格式错误")
    result = deepcopy(item)
    for key, limit in (("id", 64), ("name", 120), ("model", 200), ("base_url", 2048)):
        value = result.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("裁判 id、名称、模型或 URL 字段无效")
        result[key] = value.strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", result["id"]):
        raise ValueError("裁判 id 只允许字母、数字、下划线与连字符")
    url = result["base_url"]
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or "?" in url or "#" in url
                or "\\" in url or any(c.isspace() for c in url) or "%" in parsed.netloc
                or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", parsed.netloc)
                or (port is not None and not 1 <= port <= 65535)
                or any(p in (".", "..") for p in parsed.path.split("/"))):
            raise ValueError
    except ValueError:
        raise ValueError("裁判 base_url 必须是无凭据、查询参数或片段的 HTTP(S) URL") from None
    reference = result.setdefault("credential_ref", "")
    if not isinstance(reference, str) or (reference and not re.fullmatch(r"(?:env:)?[A-Za-z_][A-Za-z0-9_]*|local:[a-f0-9]{32}", reference)):
        raise ValueError("裁判凭据只能使用环境变量或本机加密引用")
    if type(result.setdefault("enabled", True)) is not bool:
        raise ValueError("裁判 enabled 必须为布尔值")
    body = result.setdefault("extra_body", {})
    # Deliberately small protocol allowlist: no headers, URLs, scripts or keys.
    if not isinstance(body, dict) or set(body) - {"thinking", "enable_thinking", "reasoning_effort"}:
        raise ValueError("裁判 extra_body 仅支持 thinking、enable_thinking、reasoning_effort")
    if "thinking" in body and (not isinstance(body["thinking"], dict) or set(body["thinking"]) != {"type"}
                               or body["thinking"]["type"] not in ("enabled", "disabled")):
        raise ValueError("裁判 thinking 配置无效")
    if "enable_thinking" in body and type(body["enable_thinking"]) is not bool:
        raise ValueError("裁判 enable_thinking 必须为布尔值")
    if "reasoning_effort" in body and body["reasoning_effort"] not in ("none", "minimal", "low", "medium", "high", "xhigh"):
        raise ValueError("裁判 reasoning_effort 配置无效")
    return result


def _validate_models(models):
    if not isinstance(models, list):
        raise ValueError("裁判 models 必须为列表")
    result = [validate_judge_model(m) for m in models]
    if len({m["id"].casefold() for m in result}) != len(result):
        raise ValueError("裁判 id 重复")
    return result


def default_judge_models(cfg=None):
    """Compatibility entries are virtual: listing does not create any files."""
    return _validate_models([
        dict(id="qwen", name="Qwen", model=getattr(cfg, "judge_model", "qwen-plus"),
             base_url=(getattr(cfg, "judge_base_url", "") or os.getenv("DASHSCOPE_BASE_URL")
                       or "https://dashscope.aliyuncs.com/compatible-mode/v1"),
             credential_ref="DASHSCOPE_API_KEY", enabled=True, extra_body={}),
        dict(id="deepseek", name="DeepSeek", model=getattr(cfg, "deepseek_judge_model", "") or "deepseek-v4-pro",
             base_url=getattr(cfg, "deepseek_base_url", "") or "https://api.deepseek.com",
             credential_ref="DEEPSEEK_API_KEY", enabled=True, extra_body={"thinking": {"type": "disabled"}}),
    ])


def list_judge_models(root=None):
    path = registry_path(root)
    if not path.exists():
        return default_judge_models()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise ValueError("裁判注册表不是有效 JSON") from None
    if not isinstance(raw, dict) or set(raw) != {"version", "models"} or type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("裁判注册表格式或版本无效")
    return _validate_models(raw["models"])


def _save(models, expected_hash, root):
    root = Path(root or PROJECT_ROOT).resolve()
    return atomic_json(registry_path(root), {"version": 1, "models": _validate_models(models)}, expected_hash, root)


def create_judge_model(name, model, base_url, credential_ref="", *, judge_id=None,
                       enabled=True, extra_body=None, token=None, expected_hash=None, root=None):
    current_hash = registry_hash(root)
    if expected_hash is not None and expected_hash != current_hash:
        raise ValueError("配置已修改，请重新加载后保存")
    entries = list_judge_models(root)
    item = validate_judge_model(dict(id=judge_id or "judge-" + uuid4().hex[:12], name=name, model=model,
                                    base_url=base_url, credential_ref=credential_ref, enabled=enabled,
                                    extra_body={} if extra_body is None else extra_body))
    if any(m["id"].casefold() == item["id"].casefold() for m in entries):
        raise ValueError("裁判 id 已存在")
    if token is not None:
        from local_credentials import save_token
        item["credential_ref"] = save_token(token, Path(root or PROJECT_ROOT).resolve())
    _save(entries + [item], current_hash, root)
    return deepcopy(item)


def update_judge_model(judge_id, *, expected_hash, root=None, name=None, model=None,
                       base_url=None, credential_ref=None, enabled=None, extra_body=None, token=None):
    if registry_hash(root) != expected_hash:
        raise ValueError("配置已修改，请重新加载后保存")
    entries = list_judge_models(root)
    item = next((m for m in entries if m["id"] == judge_id), None)
    if item is None:
        raise ValueError("裁判不存在")
    for key, value in dict(name=name, model=model, base_url=base_url, credential_ref=credential_ref,
                           enabled=enabled, extra_body=extra_body).items():
        if value is not None:
            item[key] = value
    checked = validate_judge_model(item)
    if token is not None:
        from local_credentials import save_token
        checked["credential_ref"] = save_token(token, Path(root or PROJECT_ROOT).resolve())
    entries = [checked if m["id"] == judge_id else m for m in entries]
    _save(entries, expected_hash, root)
    return deepcopy(checked)


def snapshot_judge_models(selected_ids, root=None, cfg=None):
    """Freeze the ordered explicit selection, with no resolved keys in the result."""
    if not isinstance(selected_ids, list) or any(not isinstance(i, str) for i in selected_ids):
        raise ValueError("裁判选择必须为 id 列表")
    if len(set(i.casefold() for i in selected_ids)) != len(selected_ids):
        raise ValueError("重复选择裁判")
    models = list_judge_models(root) if registry_path(root).exists() else default_judge_models(cfg)
    available = {m["id"]: m for m in models}
    result = []
    for identity in selected_ids:
        item = available.get(identity)
        if item is None or not item["enabled"]:
            raise ValueError("所选裁判不存在或已禁用")
        result.append(deepcopy(item))
    return result


def resolve_judge_credential(item, credentials=None, root=None):
    """Resolve only in worker memory; explicit in-memory key wins over references."""
    if credentials is not None and not isinstance(credentials, dict):
        raise ValueError("裁判临时凭据必须为字典")
    if item["id"] in (credentials or {}):
        key = credentials[item["id"]]
        if not isinstance(key, str):
            raise ValueError("裁判临时凭据必须为字符串")
        return key.strip()
    reference = item["credential_ref"]
    if reference.startswith("local:"):
        from local_credentials import read_token
        return read_token(reference, Path(root or PROJECT_ROOT).resolve())
    return (os.getenv(reference.removeprefix("env:"), "") if reference else "").strip()
