"""Apply abnormal field mutations and return before/after evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from mobile_auto_mcp.proxy.json_path import delete_value as _delete
from mobile_auto_mcp.proxy.json_path import parse_path as _parse_path
from mobile_auto_mcp.proxy.json_path import read_value as _read_value
from mobile_auto_mcp.proxy.json_path import resolve_parent as _resolve_parent
from mobile_auto_mcp.proxy.json_path import set_value as _set

LONG_TEXT_MAX_LENGTH = 50
_Unsupported = object()


def apply_mutations(payload: Any, mutations: list[dict[str, Any]]) -> Any:
    """Apply mutations using the supplied state and inputs."""
    result, _ = apply_mutations_with_evidence(payload, mutations)
    return result


def apply_mutations_with_evidence(payload: Any, mutations: list[dict[str, Any]]) -> tuple[Any, list[dict[str, Any]]]:
    """Apply mutations with evidence using the supplied state and inputs."""
    result = deepcopy(payload)
    evidence: list[dict[str, Any]] = []
    for mutation in mutations or []:
        field = str(mutation.get("field") or mutation.get("path") or "").strip()
        action = str(mutation.get("action") or "").strip()
        params = mutation.get("params") or {}
        if not field or not action:
            continue
        evidence.append(_apply_one(result, field, action, params))
    return result, evidence


def _apply_one(root: Any, field: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Apply one using the supplied state and inputs."""
    parent, key = _resolve_parent(root, field)
    before_exists, before = _read_value(parent, key)
    evidence = {"field": field, "action": action, "applied": False, "before_exists": before_exists, "before": before, "after_exists": before_exists, "after": before, "reason": "", "evidence_type": "mutation"}
    if parent is None or not before_exists:
        fallback = _resolve_unique_suffix_path(root, field)
        if fallback is None:
            evidence.update({"after_exists": False, "after": None, "reason": "path_not_found", "candidates": _candidate_paths(root, field)})
            return evidence
        parent, key, resolved = fallback
        before_exists, before = _read_value(parent, key)
        evidence.update({"resolved_field": resolved, "before_exists": before_exists, "before": before, "after_exists": before_exists, "after": before, "reason": "resolved_by_unique_suffix"})
    if action == "missing":
        _delete(parent, key)
        evidence.update({"applied": True, "after_exists": False, "after": None})
        return evidence
    value = _value_for_action(action, params)
    if value is _Unsupported:
        evidence["reason"] = "unsupported_action"
        return evidence
    _set(parent, key, value)
    after_exists, after = _read_value(parent, key)
    evidence.update({"applied": True, "after_exists": after_exists, "after": after})
    return evidence


def _value_for_action(action: str, params: dict[str, Any]) -> Any:
    """Handle value for action using the supplied state and inputs."""
    if action == "empty":
        return ""
    if action == "empty_array":
        return []
    if action == "empty_object":
        return {}
    if action == "long_text":
        length = max(1, min(int(params.get("length") or LONG_TEXT_MAX_LENGTH), LONG_TEXT_MAX_LENGTH))
        return "测" * length
    if action == "emoji":
        return "😀😃😄😁"
    if action == "special_char":
        return "!@#$%^&*()_+-=[]{}|;:',.<>/?"
    if action == "image_unreachable":
        return "https://invalid.localhost/mobile-auto-missing-image.png"
    return _Unsupported


def _resolve_unique_suffix_path(root: Any, field: str) -> tuple[Any, str | int, str] | None:
    """Resolve unique suffix path using the supplied state and inputs."""
    candidates = _candidate_paths(root, field)
    if len(candidates) != 1:
        return None
    parent, key = _resolve_parent(root, candidates[0])
    return (parent, key, candidates[0]) if parent is not None and key is not None else None


def _candidate_paths(root: Any, field: str) -> list[str]:
    """Handle candidate paths using the supplied state and inputs."""
    target = _parse_path(field)
    paths: list[tuple[list[str | int], str]] = []
    _walk_paths(root, [], paths)
    return sorted({path for tokens, path in paths if len(tokens) >= len(target) and tokens[-len(target) :] == target})


def _walk_paths(value: Any, tokens: list[str | int], paths: list[tuple[list[str | int], str]]) -> None:
    """Handle walk paths using the supplied state and inputs."""
    if tokens:
        paths.append((tokens[:], _format_path(tokens)))
    if isinstance(value, dict):
        for key, child in value.items():
            _walk_paths(child, [*tokens, key], paths)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_paths(child, [*tokens, index], paths)


def _format_path(tokens: list[str | int]) -> str:
    """Format path using the supplied state and inputs."""
    parts: list[str] = []
    for token in tokens:
        if isinstance(token, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{token}]"
            else:
                parts.append(f"[{token}]")
        else:
            parts.append(token)
    return ".".join(parts)
