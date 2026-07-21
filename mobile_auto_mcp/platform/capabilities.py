"""Host capability checks that keep unsupported lanes from breaking MCP startup."""

from __future__ import annotations

import platform


def host_capability(target: str, system: str | None = None) -> dict[str, object]:
    """Report whether the host platform can prepare the requested mobile target."""
    normalized = target.lower()
    host_platform = system or platform.system()
    if normalized == "ios" and host_platform != "Darwin":
        return {
            "ok": False,
            "code": "platform_not_supported",
            "target": normalized,
            "host_platform": host_platform,
        }
    return {
        "ok": True,
        "code": "platform_supported",
        "target": normalized,
        "host_platform": host_platform,
    }
