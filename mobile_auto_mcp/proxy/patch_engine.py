"""Patch JSON payloads from fixtures or mock sources before mutation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from mobile_auto_mcp.proxy.json_path import get_value as _get
from mobile_auto_mcp.proxy.json_path import read_path as _read_path
from mobile_auto_mcp.proxy.json_path import read_value as _read_value
from mobile_auto_mcp.proxy.json_path import resolve_parent as _resolve_parent
from mobile_auto_mcp.proxy.json_path import set_value as _set


def apply_patches(payload: Any, patches: list[dict[str, Any]] | None = None, fixtures: list[dict[str, Any]] | None = None, mock_sources: list[dict[str, Any]] | None = None) -> Any:
    """Apply patches using the supplied state and inputs."""
    result, _ = apply_patches_with_evidence(payload, patches, fixtures, mock_sources)
    return result


def apply_patches_with_evidence(
    payload: Any,
    patches: list[dict[str, Any]] | None = None,
    fixtures: list[dict[str, Any]] | None = None,
    mock_sources: list[dict[str, Any]] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Apply patches with evidence using the supplied state and inputs."""
    result = deepcopy(payload)
    evidence: list[dict[str, Any]] = []
    for source in mock_sources or []:
        value = _mock_value(source)
        if value is None:
            continue
        strategy = str(source.get("strategy") or "merge_missing")
        if strategy in {"replace_root", "root_replace"}:
            before = deepcopy(result)
            result = deepcopy(value)
            evidence.append(_evidence("<root>", strategy, True, before, True, result, applied=True, source="mock_source"))
            continue
        for patch in infer_missing_patches(result, value):
            item = _apply_patch(result, patch)
            item["source"] = "mock_source"
            evidence.append(item)
    for fixture in fixtures or []:
        evidence.append(_apply_fixture(result, fixture))
    for patch in patches or []:
        evidence.append(_apply_patch(result, patch))
    return result, evidence


def infer_missing_patches(real_payload: Any, mock_payload: Any) -> list[dict[str, Any]]:
    """Handle infer missing patches using the supplied state and inputs."""
    patches: list[dict[str, Any]] = []
    _collect_missing_patches(real_payload, mock_payload, "", patches)
    return patches


def _mock_value(source: dict[str, Any]) -> Any:
    """Handle mock value using the supplied state and inputs."""
    if "value" in source:
        return source.get("value")
    if "payload" in source:
        return source.get("payload")
    return source.get("body")


def _apply_fixture(root: Any, fixture: dict[str, Any]) -> dict[str, Any]:
    """Apply fixture using the supplied state and inputs."""
    merge_path = str(fixture.get("merge_path") or "").strip()
    value = deepcopy(fixture.get("value"))
    before_exists, before = _read_path(root, merge_path)
    if merge_path:
        parent, key = _resolve_parent(root, merge_path, create_missing=True)
        if parent is None:
            return _evidence(merge_path, "deep_merge", before_exists, before, False, None, reason="path_not_found")
        exists, current = _read_value(parent, key)
        if exists and isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(_get(parent, key), value)
        else:
            _set(parent, key, value, create_missing=True)
    elif isinstance(root, dict) and isinstance(value, dict):
        _deep_merge(root, value)
    else:
        return _evidence(merge_path or "<root>", "deep_merge", before_exists, before, before_exists, before, reason="root_merge_requires_objects")
    after_exists, after = _read_path(root, merge_path)
    return _evidence(merge_path or "<root>", "deep_merge", before_exists, before, after_exists, after, applied=True)


def _apply_patch(root: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """Apply patch using the supplied state and inputs."""
    field = str(patch.get("field") or "").strip()
    action = str(patch.get("action") or "upsert")
    value = deepcopy(patch.get("value"))
    before_exists, before = _read_path(root, field)
    parent, key = _resolve_parent(root, field, create_missing=action in {"upsert", "deep_merge"})
    if not field or parent is None:
        return _evidence(field, action, before_exists, before, False, None, reason="path_not_found")
    if action == "replace" and not before_exists:
        return _evidence(field, action, before_exists, before, before_exists, before, reason="path_not_found")
    if action == "deep_merge":
        exists, current = _read_value(parent, key)
        if exists and isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(_get(parent, key), value)
        else:
            _set(parent, key, value, create_missing=True)
    else:
        _set(parent, key, value, create_missing=True)
    after_exists, after = _read_path(root, field)
    return _evidence(field, action, before_exists, before, after_exists, after, applied=True)


def _evidence(field: str, action: str, before_exists: bool, before: Any, after_exists: bool, after: Any, applied: bool = False, reason: str = "", source: str = "") -> dict[str, Any]:
    """Handle evidence using the supplied state and inputs."""
    payload = {"field": field, "action": action, "applied": applied, "before_exists": before_exists, "before": before, "after_exists": after_exists, "after": after, "reason": reason, "evidence_type": "patch"}
    if source:
        payload["source"] = source
    return payload


def _collect_missing_patches(real: Any, mock: Any, path: str, patches: list[dict[str, Any]]) -> None:
    """Collect missing patches using the supplied state and inputs."""
    if isinstance(real, dict) and isinstance(mock, dict):
        for key, value in mock.items():
            child = f"{path}.{key}" if path else str(key)
            if key not in real:
                patches.append({"field": child, "action": "upsert", "value": deepcopy(value)})
            else:
                _collect_missing_patches(real[key], value, child, patches)
    elif isinstance(real, list) and isinstance(mock, list):
        for index, value in enumerate(mock):
            child = f"{path}[{index}]" if path else f"[{index}]"
            if index >= len(real):
                patches.append({"field": child, "action": "upsert", "value": deepcopy(value)})
            else:
                _collect_missing_patches(real[index], value, child, patches)


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Handle deep merge using the supplied state and inputs."""
    for key, value in source.items():
        if isinstance(target.get(key), dict) and isinstance(value, dict):
            _deep_merge(target[key], value)
        else:
            target[key] = deepcopy(value)
