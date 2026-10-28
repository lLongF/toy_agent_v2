"""Local-only toy management; no credentials are persisted by these APIs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from toy_profiles import (PROJECT_ROOT, load_toy_profile, safe_path, validate_id,
                          validate_management_fields, apply_toy_environment)


def file_hash(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def atomic_json(path, data, expected_hash, root):
    """Locked CAS save; the empty hash means the file must not yet exist."""
    path = safe_path(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = safe_path(path.with_name(path.name + ".edit.lock"), root)
    temporary = safe_path(path.with_name("." + uuid4().hex + ".tmp"), root)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("配置正在保存，请稍后重试") from None
    os.close(descriptor)
    try:
        if expected_hash is None or file_hash(path) != expected_hash:
            raise ValueError("配置已修改，请重新加载后保存")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            backups = safe_path(path.parent / ".backups", root)
            backups.mkdir(exist_ok=True)
            shutil.copy2(path, backups / (uuid4().hex + "_" + path.name))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    return file_hash(path)


def _fields(name, credential_ref, capabilities, environments, enabled):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("玩具名称不能为空")
    data = dict(name=name.strip(), credential_ref=credential_ref,
                capabilities=capabilities, environments=environments or {}, enabled=enabled)
    validate_management_fields(data)
    return data


def create_toy(name, credential_ref='', capabilities=None, environments=None, *,
               toy_id=None, enabled=True, root=None, token=None):
    """Create toys/<id>/toy.json without writing or copying real cases or secrets."""
    root = Path(root or PROJECT_ROOT).resolve()
    toy_id = validate_id(toy_id or "toy-" + uuid4().hex[:12])
    if toy_id == "_template":
        raise ValueError("保留的玩具 id")
    data = _fields(name, credential_ref, capabilities, environments, enabled)
    toys_dir = safe_path(root / "toys", root)
    directory = safe_path(toys_dir / toy_id, toys_dir)
    directory.parent.mkdir(parents=True, exist_ok=True)
    if any(child.name.casefold() == toy_id.casefold() for child in directory.parent.iterdir()):
        raise ValueError("玩具 id 已存在")
    if token is not None:
        from local_credentials import save_token
        data["credential_ref"] = save_token(token, root)
    # mkdir is the uniqueness operation: do not reuse even an incomplete existing toy.
    directory.mkdir()
    data.update(id=toy_id, cases_xlsx="../../data/测试样例.xlsx",
                mcp_tools_file="../../data/工具描述.txt")
    atomic_json(directory / "toy.json", data, "", root)
    return load_toy_profile(toy_id, root / "toys")


def update_toy(profile, *, expected_hash, name=None, credential_ref=None,
               capabilities=None, environments=None, enabled=None, root=None, token=None):
    """CAS update. None preserves each field; use {} or [] to clear capabilities."""
    root = Path(root or profile.root.parent.parent).resolve()
    path = safe_path(profile.root / "toy.json", root)
    if path.parent != safe_path(root / "toys" / validate_id(profile.id), root):
        raise ValueError("玩具路径不匹配")
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in dict(name=name, credential_ref=credential_ref,
                           capabilities=capabilities, environments=environments,
                           enabled=enabled).items():
        if value is not None:
            data[key] = value
    data.update(_fields(data.get("name", profile.id), data.get("credential_ref", ""),
                        data.get("capabilities"), data.get("environments", {}), data.get("enabled", True)))
    # Legacy secrets remain readable but management never writes them back or to backups.
    if data.get("token"):
        raise ValueError("旧配置含明文 token，请先由授权人员迁移到凭据环境变量引用")
    data.pop("token", None)
    if data.get("id", profile.id) != profile.id:
        raise ValueError("玩具 id 不匹配")
    if token is not None and str(token).strip():
        if file_hash(path) != expected_hash:
            raise ValueError("配置已修改，请重新加载后保存")
        from local_credentials import save_token
        data["credential_ref"] = save_token(token, root)
    atomic_json(path, data, expected_hash, root)
    return load_toy_profile(profile.id, root / "toys")
