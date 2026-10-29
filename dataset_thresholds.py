"""Per-dataset evaluation thresholds with CAS-safe sidecar persistence."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from toy_management import atomic_json, file_hash

DEFAULT_THRESHOLD = 0.7
MCP_THRESHOLD = 1.0


def _root(root=None):
    return Path(root or Path(__file__).resolve().parent).resolve()


def _dataset_id(dataset):
    if isinstance(dataset, dict):
        value = dataset.get("id", dataset.get("dataset_id"))
    else:
        value = getattr(dataset, "id", None)
        if value is None:
            value = getattr(dataset, "dataset_id", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("测试集缺少 dataset_id")
    return value.strip()


def _dataset_path(dataset, root):
    path = getattr(dataset, "path", None)
    if path is None and isinstance(dataset, dict):
        path = dataset.get("path")
    if path is None:
        raise ValueError("测试集缺少 xlsx 路径")
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def thresholds_path(dataset, root=None):
    root = _root(root)
    return (root / "data" / "datasets" / (_dataset_id(dataset) + ".thresholds.json")).resolve()


def _valid(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
        raise ValueError("评分阈值必须为 0–1 的数值")
    return float(value)


def _mcp(category, expect_tool_call=None, evaluation_mode=None):
    return evaluation_mode == "embedded_rule" or (category == "MCP调用" and expect_tool_call is True)


def _old_rules(cfg):
    if cfg is None:
        return []
    path = getattr(cfg, "thresholds_file", None)
    if not path or not Path(path).is_file():
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    result = []
    for item in raw.get("case_thresholds", []) if isinstance(raw, dict) else []:
        if isinstance(item, dict):
            result.append(item)
    return result


def _normalize(dataset, raw, root, cfg=None):
    did = _dataset_id(dataset)
    if not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("dataset_id") != did:
        raise ValueError("测试集阈值 sidecar 格式无效")
    expected = file_hash(_dataset_path(dataset, root))
    if raw.get("dataset_sha256") != expected:
        raise ValueError("测试集阈值与当前测试集内容不匹配")
    cases = raw.get("case_thresholds", [])
    if not isinstance(cases, list):
        raise ValueError("case_thresholds 必须为列表")
    normalized = []
    for item in cases:
        if not isinstance(item, dict):
            raise ValueError("单条阈值格式无效")
        if item.get("dataset_id", did) != did or not item.get("category") or not item.get("case_id"):
            raise ValueError("单条阈值必须提供匹配的 dataset_id、category、case_id")
        threshold = _valid(item.get("threshold"))
        category = str(item["category"])
        mode = item.get("evaluation_mode")
        if mode is not None and not isinstance(mode, str):
            raise ValueError("evaluation_mode 格式无效")
        if _mcp(category, item.get("expect_tool_call"), mode):
            threshold = MCP_THRESHOLD
        entry = {"dataset_id": did, "category": category, "case_id": str(item["case_id"]), "threshold": threshold}
        if "expect_tool_call" in item:
            entry["expect_tool_call"] = bool(item["expect_tool_call"])
        if "sheet_row" in item:
            if type(item["sheet_row"]) is not int or item["sheet_row"] < 2:
                raise ValueError("sheet_row 必须为不小于 2 的整数")
            entry["sheet_row"] = item["sheet_row"]
        if mode is not None:
            entry["evaluation_mode"] = mode
        normalized.append(entry)
    return {"version": 1, "dataset_id": did, "dataset_sha256": expected, "case_thresholds": normalized}


def load_dataset_thresholds(dataset, root=None, cfg=None):
    root = _root(root)
    path = thresholds_path(dataset, root)
    if path.is_file():
        return _normalize(dataset, json.loads(path.read_text(encoding="utf-8")), root, cfg)
    return ensure_dataset_thresholds(dataset, root, cfg)


def ensure_dataset_thresholds(dataset, root=None, cfg=None):
    root = _root(root)
    did = _dataset_id(dataset)
    sha = file_hash(_dataset_path(dataset, root))
    cases = []
    # Legacy rules are used only when no sidecar exists and identity matches.
    for item in _old_rules(cfg):
        if item.get("dataset_id") not in (None, did):
            continue
        if not item.get("category") or not item.get("case_id"):
            continue
        threshold = _valid(item.get("threshold"))
        if _mcp(item.get("category"), item.get("expect_tool_call"), item.get("evaluation_mode")):
            threshold = MCP_THRESHOLD
        entry = {"dataset_id": did, "category": str(item["category"]), "case_id": str(item["case_id"]), "threshold": threshold}
        if "sheet_row" in item: entry["sheet_row"] = item["sheet_row"]
        cases.append(entry)
    return {"version": 1, "dataset_id": did, "dataset_sha256": sha, "case_thresholds": cases}


def case_threshold(dataset, category, case_id, sheet_row, root=None, cfg=None):
    data = load_dataset_thresholds(dataset, root, cfg)
    for item in data["case_thresholds"]:
        if item["category"] == category and item["case_id"] == str(case_id) and ("sheet_row" not in item or item["sheet_row"] == sheet_row):
            return float(item["threshold"])
    if category == "MCP调用" and getattr(dataset, "expect_tool_call", False):
        return MCP_THRESHOLD
    return DEFAULT_THRESHOLD


def snapshot_threshold(snapshot, category, case_id, sheet_row, root=None, cfg=None):
    """Resolve a run-row threshold from the frozen dataset snapshot.

    Returns None when the snapshot has no source mapping or no sidecar for that
    dataset, so callers keep the legacy category/case behaviour. An existing
    sidecar is authoritative: hash/schema errors stay visible instead of being
    silently replaced by defaults.
    """
    if not isinstance(snapshot, dict):
        return None
    rows = snapshot.get("rows")
    source_row = rows.get(str(sheet_row)) if isinstance(rows, dict) else None
    if not isinstance(source_row, dict):
        return None
    dataset_id = source_row.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        return None
    root = _root(root)
    meta = next((item for item in (snapshot.get("datasets") or [])
                 if isinstance(item, dict) and item.get("id") == dataset_id), None)
    if not isinstance(meta, dict) or not meta.get("path"):
        return None
    dataset_ref = {"id": dataset_id, "path": root / meta["path"]}
    if not thresholds_path(dataset_ref, root).is_file():
        return None
    return case_threshold(dataset_ref, category, case_id, source_row.get("row"), root=root, cfg=cfg)


def save_dataset_thresholds(dataset, thresholds, expected_hash, root=None):
    root = _root(root)
    did = _dataset_id(dataset)
    sha = file_hash(_dataset_path(dataset, root))
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds 必须为对象")
    payload = _normalize(dataset, {"version": 1, "dataset_id": did, "dataset_sha256": sha,
                                   "case_thresholds": thresholds.get("case_thresholds", thresholds.get("thresholds", []))}, root)
    return atomic_json(thresholds_path(dataset, root), payload, expected_hash, root)
