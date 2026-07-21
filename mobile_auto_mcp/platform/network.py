"""Platform-specific argv builders for shell-free host network probes."""

from __future__ import annotations

import platform


def ping_command(host: str, destination: str, system: str | None = None) -> list[str]:
    """Build a one-packet, source-bound ping argv for the selected host platform."""
    host_platform = system or platform.system()
    if host_platform == "Darwin":
        return ["/sbin/ping", "-S", host, "-c", "1", "-W", "1000", destination]
    if host_platform == "Linux":
        return ["ping", "-I", host, "-c", "1", "-W", "1", destination]
    if host_platform == "Windows":
        return ["ping", "-S", host, "-n", "1", "-w", "1000", destination]
    raise ValueError(f"unsupported host platform: {host_platform}")
