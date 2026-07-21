"""Exact request matching shared by probe and response mutation paths."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def request_matches_rule(
    rule: dict[str, Any] | str,
    request_url: str,
    method: str = "",
) -> bool:
    """Return whether one request exactly satisfies a rule's host, path, and method.

    Incomplete legacy rules are rejected rather than treated as wildcards.
    Query strings do not broaden the path match, and methods are case-insensitive.
    """
    normalized_rule = {"api": rule} if isinstance(rule, str) else dict(rule or {})
    api = str(normalized_rule.get("api") or normalized_rule.get("path") or "").strip()
    if not api or not request_url:
        return False

    request = urlsplit(request_url)
    api_parts = urlsplit(api)
    expected_path = (
        api_parts.path
        if api_parts.scheme or api_parts.netloc
        else urlsplit(f"https://placeholder{_with_leading_slash(api)}").path
    )
    if not expected_path or request.path != expected_path:
        # 精确 path 门禁防止 /v1/user 错命中 /v1/users 或任意包含该片段的接口。
        return False

    configured_host = (
        str(normalized_rule.get("host") or api_parts.hostname or "").strip().lower()
    )
    if not configured_host or (request.hostname or "").lower() != configured_host:
        return False

    configured_method = (
        str(normalized_rule.get("method") or normalized_rule.get("http_method") or "")
        .strip()
        .upper()
    )
    if not configured_method or str(method or "").strip().upper() != configured_method:
        return False
    return True


def _with_leading_slash(value: str) -> str:
    """Normalize a path-only API so ``urlsplit`` cannot mistake it for a host."""
    return value if value.startswith("/") else f"/{value}"
