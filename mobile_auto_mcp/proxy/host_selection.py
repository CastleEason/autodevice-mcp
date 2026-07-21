"""Pure helpers for selecting a phone-reachable local proxy address."""

from __future__ import annotations

import ipaddress
import re
import subprocess
from collections.abc import Iterable, Mapping
from typing import Any

from mobile_auto_mcp.platform.network import ping_command


_INET_PATTERN = re.compile(
    r"\binet(?:\s+addr:|\s+)(?P<address>\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?)\b",
    re.IGNORECASE,
)


def parse_device_wifi_ip(output: str) -> str:
    """Extract one usable Wi-Fi IPv4 host address from Android or HarmonyOS command output."""
    interface = parse_device_wifi_interface(output)
    return str(ipaddress.ip_interface(interface).ip) if interface else ""


def parse_device_wifi_interface(output: str) -> str:
    """Extract a usable Wi-Fi IPv4 interface while preserving an advertised netmask."""
    text = str(output or "")
    values = [match.group("address") for match in _INET_PATTERN.finditer(text)]
    if not values:
        values = re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])", text)
    for value in values:
        try:
            interface = ipaddress.ip_interface(value if "/" in value else f"{value}/24")
        except ValueError:
            continue
        address = interface.ip
        # Loopback, link-local, multicast, and unspecified interfaces cannot route a phone to the host proxy.
        if not isinstance(address, ipaddress.IPv4Address) or not _usable_lan_address(address):
            continue
        if "/" in value:
            return str(interface)
        mask_match = re.search(r"\bMask:(?P<mask>\d{1,3}(?:\.\d{1,3}){3})\b", text, flags=re.IGNORECASE)
        if mask_match:
            try:
                return str(ipaddress.ip_interface(f"{address}/{mask_match.group('mask')}"))
            except ValueError:
                pass
        return str(address)
    return ""


def discover_device_wifi_ip(
    target: str,
    device_serial: str = "",
    driver: Any | None = None,
    *,
    command_runner: Any | None = None,
) -> dict[str, Any]:
    """Read a fresh Wi-Fi address from the exact selected device without using static configuration."""
    normalized = str(target or "").lower()
    if normalized == "ios":
        reader = getattr(driver, "read_wifi_network_info", None)
        if not callable(reader):
            return {"ok": False, "target": normalized, "device_ip": "", "code": "device_wifi_ip_unavailable"}
        payload = dict(reader())
        interface = parse_device_wifi_interface(str(payload.get("device_ip") or ""))
        device_ip = str(ipaddress.ip_interface(interface).ip) if interface else ""
        if not payload.get("ok") or not device_ip:
            return {"ok": False, "target": normalized, "device_ip": "", "code": "device_wifi_ip_unavailable"}
        result = {"ok": True, "target": normalized, "device_ip": device_ip, "source": "semantic_settings"}
        if "/" in interface:
            result["device_network"] = interface
        return result
    if normalized not in {"android", "harmony"}:
        return {"ok": False, "target": normalized, "device_ip": "", "code": "unsupported_target"}
    if normalized == "android":
        command = ["adb", *( ["-s", device_serial] if device_serial else []), "shell", "ifconfig", "wlan0"]
    else:
        command = ["hdc", *( ["-t", device_serial] if device_serial else []), "shell", "ifconfig", "wlan0"]
    try:
        completed = command_runner(command) if command_runner else subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "target": normalized, "device_ip": "", "code": "device_wifi_ip_unavailable", "message": str(exc)}
    interface = parse_device_wifi_interface(str(getattr(completed, "stdout", "") or ""))
    device_ip = str(ipaddress.ip_interface(interface).ip) if interface else ""
    if int(getattr(completed, "returncode", 1)) != 0 or not device_ip:
        return {"ok": False, "target": normalized, "device_ip": "", "code": "device_wifi_ip_unavailable"}
    result = {"ok": True, "target": normalized, "device_ip": device_ip, "source": "device_command"}
    if "/" in interface:
        result["device_network"] = interface
    return result


def select_proxy_host(
    candidates: Iterable[str],
    explicit_host: str = "",
    device_wifi_ips: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Select one local candidate proven to share every device's Wi-Fi subnet, or return a hard block."""
    local_candidates = _normalized_candidates(candidates)
    device_networks, invalid_devices = _device_networks(device_wifi_ips or {})
    evidence: dict[str, Any] = {
        "candidates": [str(address) for address in local_candidates],
        "explicit_host": str(explicit_host or "").strip(),
        "device_networks": {target: str(network) for target, network in device_networks.items()},
        "invalid_devices": invalid_devices,
        "proof": "device_wifi_subnet_membership",
    }
    if not local_candidates or not device_networks or invalid_devices:
        return _unproven(evidence, "缺少有效的本机候选地址或设备 Wi-Fi 地址")

    ordered = list(local_candidates)
    if explicit_host:
        explicit = _parse_candidate(explicit_host)
        # An operator override changes order only; it cannot authorize a non-local or unreachable address.
        if explicit is None or explicit not in local_candidates:
            return _unproven(evidence, "显式代理地址不是本机候选地址")
        ordered = [explicit, *(candidate for candidate in ordered if candidate != explicit)]

    for candidate in ordered:
        if all(candidate in network for network in device_networks.values()):
            evidence["selected_host"] = str(candidate)
            return {
                "ok": True,
                "host": str(candidate),
                "code": "proxy_host_selected",
                "evidence": evidence,
            }
    return _unproven(evidence, "没有候选地址与全部设备处于可证明的共同 Wi-Fi 子网")


def probe_proxy_host_reachability(
    host: str,
    device_wifi_ips: Mapping[str, str],
    *,
    command_runner: Any | None = None,
) -> dict[str, Any]:
    """Prove a host-bound route to every selected phone before changing proxy settings."""
    source = _parse_candidate(host)
    evidence: list[dict[str, Any]] = []
    if source is None or not device_wifi_ips:
        return {"ok": False, "code": "proxy_host_unreachable", "host": host, "devices": evidence}
    for target, value in device_wifi_ips.items():
        destination = _parse_candidate(str(value).split("/", 1)[0])
        if destination is None:
            evidence.append({"target": target, "device_ip": str(value), "reachable": False, "reason": "invalid_device_ip"})
            continue
        command = ping_command(str(source), str(destination))
        try:
            completed = command_runner(command) if command_runner else subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            reachable = int(getattr(completed, "returncode", 1)) == 0
            evidence.append(
                {
                    "target": str(target),
                    "device_ip": str(destination),
                    "reachable": reachable,
                    "returncode": int(getattr(completed, "returncode", 1)),
                }
            )
        except (OSError, subprocess.SubprocessError) as exc:
            evidence.append({"target": str(target), "device_ip": str(destination), "reachable": False, "reason": str(exc)})
    ok = len(evidence) == len(device_wifi_ips) and all(item.get("reachable") for item in evidence)
    return {
        "ok": ok,
        "code": "proxy_host_reachable" if ok else "proxy_host_unreachable",
        "host": str(source),
        "devices": evidence,
        "proof": "source_bound_icmp_route",
    }


def _normalized_candidates(candidates: Iterable[str]) -> list[ipaddress.IPv4Address]:
    """Parse and de-duplicate local IPv4 candidates while preserving interface enumeration order."""
    normalized: list[ipaddress.IPv4Address] = []
    for value in candidates:
        address = _parse_candidate(value)
        if address is not None and address not in normalized:
            normalized.append(address)
    return normalized


def _parse_candidate(value: str) -> ipaddress.IPv4Address | None:
    """Parse one host or interface value into a usable LAN IPv4 address."""
    try:
        address = ipaddress.ip_interface(str(value).strip()).ip
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv4Address) or not _usable_lan_address(address):
        return None
    return address


def _device_networks(values: Mapping[str, str]) -> tuple[dict[str, ipaddress.IPv4Network], list[str]]:
    """Convert every device address into its advertised network, defaulting unqualified addresses to /24."""
    networks: dict[str, ipaddress.IPv4Network] = {}
    invalid: list[str] = []
    for target, value in values.items():
        text = str(value or "").strip()
        try:
            interface = ipaddress.ip_interface(text if "/" in text else f"{text}/24")
        except ValueError:
            invalid.append(str(target))
            continue
        if not isinstance(interface, ipaddress.IPv4Interface) or not _usable_lan_address(interface.ip):
            invalid.append(str(target))
            continue
        networks[str(target)] = interface.network
    return networks, invalid


def _usable_lan_address(address: ipaddress.IPv4Address) -> bool:
    """Return whether an address is a private unicast LAN endpoint suitable for phone routing."""
    return bool(
        address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
    )


def _unproven(evidence: dict[str, Any], reason: str) -> dict[str, Any]:
    """Build the stable hard-block result used when LAN reachability cannot be proven."""
    return {
        "ok": False,
        "host": "",
        "code": "proxy_host_unproven",
        "message": reason,
        "evidence": evidence,
    }
