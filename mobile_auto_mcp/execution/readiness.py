"""Run preparation and user-facing readiness diagnostics."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from mobile_auto_mcp.cases.parser import analyze_case_file
from mobile_auto_mcp.execution.preflight import run_preflight


def diagnose_environment(target: str = "android") -> dict[str, Any]:
    """Return local tool availability without mutating devices."""
    targets = _targets_for(target)
    tool_names = ["mitmdump", "adb"]
    if "ios" in targets:
        tool_names.extend(["xcodebuild", "xcrun", "iproxy", "tidevice", "appium"])
    if "harmony" in targets:
        tool_names.extend(["hdc"])
    tools = {name: _tool_status(name) for name in dict.fromkeys(tool_names)}
    ios_tap_backend = _ios_tap_backend_status() if "ios" in targets else {}
    if ios_tap_backend:
        tools["ios_tap_backend"] = ios_tap_backend
    return {"targets": targets, "tools": tools}


def prepare_run(
    *,
    app_id: str,
    target: str = "android",
    case_file: str = "",
    target_app_package: str = "",
    package_name: str = "",
    target_app_packages: dict[str, str] | None = None,
    proxy_required: bool = True,
    proxy_port: int | None = None,
    device_serial: str = "",
    android_serial: str = "",
    ios_udid: str = "",
    harmony_serial: str = "",
    wda_url: str = "",
    auto_start_wda: bool | None = None,
    allow_wda_reinstall: bool = False,
    wda_start_command: str = "",
    wda_iproxy_command: str = "",
) -> dict[str, Any]:
    """Prepare a run for users who only know the desired outcome."""
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    targets = _targets_for(target)
    env = diagnose_environment(target)
    packages = target_app_packages or {}

    if not app_id:
        blockers.append(
            _blocker(
                stage="namespace",
                user_message="缺少 app_id，无法隔离知识库和运行数据。",
                why_it_matters="app_id 用来给规则、报告和导航知识分配命名空间。",
                how_to_fix=["传入业务或 App 的稳定名称，例如 app_id='demo_app'。"],
            )
        )

    case_analysis = _analyze_case(case_file)
    if case_analysis.get("blocker"):
        blockers.append(case_analysis["blocker"])

    if "android" in targets and not _package_for_target("android", targets, packages, target_app_package or package_name):
        blockers.append(
            _blocker(
                stage="app_launch",
                user_message="缺少 Android target_app_package，无法启动目标 App。",
                why_it_matters="app_id 只用于数据命名空间，不能推断真实包名。",
                how_to_fix=["传入 Android 包名，例如 target_app_package='com.example.android'。"],
            )
        )
    if "ios" in targets and not _package_for_target("ios", targets, packages, target_app_package or package_name):
        blockers.append(
            _blocker(
                stage="app_launch",
                user_message="缺少 iOS bundle id，无法启动目标 App。",
                why_it_matters="iOS 自动化必须使用显式 bundle id 启动应用。",
                how_to_fix=["传入 iOS bundle id，例如 target_app_package='com.example.ios'。"],
            )
        )
    if "harmony" in targets and not _package_for_target("harmony", targets, packages, target_app_package or package_name):
        blockers.append(
            _blocker(
                stage="app_launch",
                user_message="缺少 HarmonyOS bundle/ability，无法启动目标 App。",
                why_it_matters="HarmonyOS 自动化需要显式 bundle，建议带 ability。",
                how_to_fix=["传入 HarmonyOS 启动标识，例如 target_app_packages={'harmony':'com.example.hmclient/EntryAbility'}。"],
            )
        )

    should_auto_start_wda = ("ios" in targets) if auto_start_wda is None else bool(auto_start_wda)
    preflight_dict = _run_target_preflights(
        targets=targets,
        requested_target=target,
        proxy_required=proxy_required,
        proxy_port=proxy_port,
        device_serial=device_serial,
        android_serial=android_serial,
        ios_udid=ios_udid,
        harmony_serial=harmony_serial,
        wda_url=wda_url,
        auto_start_wda=should_auto_start_wda,
        allow_wda_reinstall=allow_wda_reinstall,
        wda_start_command=wda_start_command,
        wda_iproxy_command=wda_iproxy_command,
    )
    if "lanes" in preflight_dict:
        for lane_target, lane_preflight in (preflight_dict.get("lanes") or {}).items():
            blockers.extend(_blockers_from_preflight(lane_preflight, target=lane_target))
            warnings.extend(_warnings_from_preflight(lane_preflight, target=lane_target))
    else:
        blockers.extend(_blockers_from_preflight(preflight_dict, target=targets[0] if targets else target))
        warnings.extend(_warnings_from_preflight(preflight_dict, target=targets[0] if targets else target))
    if "ios" in targets and not _ios_tap_backend_status().get("configured"):
        warnings.append(
            {
                "stage": "ios_tap_backend",
                "severity": "warning",
                "target": "ios",
                "user_message": "未配置 iOS 外部点击后端；WDA 标准坐标点击在部分 App 页面可能卡在 accessibility snapshot。",
                "how_to_fix": ["配置 MOBILE_AUTO_MCP_IOS_TAP_COMMAND，或在 click 调用中传 ios_tap_backend='external' 和 ios_tap_command。"],
            }
        )

    return {
        "ready": not blockers,
        "targets": targets,
        "environment": env,
        "case_analysis": case_analysis.get("analysis", {}),
        "preflight": preflight_dict,
        "proxy_instruction": _proxy_instruction_from_preflight(preflight_dict),
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": _next_actions(blockers),
    }


def _run_target_preflights(
    *,
    targets: list[str],
    requested_target: str,
    proxy_required: bool,
    proxy_port: int | None,
    device_serial: str,
    android_serial: str,
    ios_udid: str,
    harmony_serial: str,
    wda_url: str,
    auto_start_wda: bool,
    allow_wda_reinstall: bool,
    wda_start_command: str,
    wda_iproxy_command: str,
) -> dict[str, Any]:
    """Run target preflights using the supplied state and inputs."""
    lanes: dict[str, dict[str, Any]] = {}
    for lane_target in targets:
        lane_serial = {
            "android": android_serial,
            "ios": ios_udid,
            "harmony": harmony_serial,
        }.get(lane_target) or (device_serial if len(targets) == 1 else "")
        lanes[lane_target] = run_preflight(
            target=lane_target,
            proxy_required=proxy_required,
            proxy_port=proxy_port,
            device_serial=lane_serial,
            wda_url=wda_url if lane_target == "ios" else "",
            auto_start_wda=auto_start_wda if lane_target == "ios" else False,
            allow_wda_reinstall=allow_wda_reinstall if lane_target == "ios" else False,
            wda_start_command=wda_start_command if lane_target == "ios" else "",
            wda_iproxy_command=wda_iproxy_command if lane_target == "ios" else "",
        ).as_dict()
    if len(lanes) == 1:
        return next(iter(lanes.values()))
    return {
        "ok": all(lane.get("ok") for lane in lanes.values()),
        "target": (requested_target or "both").lower(),
        "targets": targets,
        "lanes": lanes,
        "proxy_instructions": {lane_target: lane.get("proxy_instruction") for lane_target, lane in lanes.items()},
        "blockers": [message for lane in lanes.values() for message in lane.get("blockers", [])],
        "warnings": [message for lane in lanes.values() for message in lane.get("warnings", [])],
    }


def _proxy_instruction_from_preflight(preflight: dict[str, Any]) -> dict[str, Any] | None:
    """Handle proxy instruction from preflight using the supplied state and inputs."""
    if preflight.get("proxy_instruction"):
        return preflight.get("proxy_instruction")
    instructions = preflight.get("proxy_instructions")
    if instructions:
        return {"targets": instructions}
    lanes = preflight.get("lanes") or {}
    if lanes:
        return {"targets": {target: lane.get("proxy_instruction") for target, lane in lanes.items()}}
    return None


def _package_for_target(target: str, targets: list[str], packages: dict[str, str], fallback: str) -> str:
    """Handle package for target using the supplied state and inputs."""
    if target in packages:
        return str(packages.get(target) or "")
    if len(targets) == 1:
        return fallback
    if target == "android":
        return fallback
    return ""


def _analyze_case(case_file: str) -> dict[str, Any]:
    """Analyze case using the supplied state and inputs."""
    if not case_file:
        return {
            "analysis": {},
            "blocker": _blocker(
                stage="case_input",
                user_message="缺少 case_file，无法导入异常场景规则。",
                why_it_matters="没有用例文件就不知道要改写哪个接口和字段。",
                how_to_fix=["传入 Markdown 用例文件路径，或先调用 analyze_cases 检查用例。"],
            ),
        }
    path = Path(case_file).expanduser()
    if not path.exists():
        return {
            "analysis": {},
            "blocker": _blocker(
                stage="case_input",
                user_message=f"用例文件不存在：{path}",
                why_it_matters="无法读取用例就不能生成异常规则。",
                how_to_fix=["确认文件路径正确，或重新导出/生成用例文件。"],
            ),
        }
    try:
        analysis = analyze_case_file(str(path))
    except Exception as exc:
        return {
            "analysis": {},
            "blocker": _blocker(
                stage="case_input",
                user_message=f"用例文件解析失败：{exc}",
                why_it_matters="解析失败会导致规则缺失或字段改写错误。",
                how_to_fix=["检查 Markdown 是否包含目标 API 和字段异常描述。"],
            ),
        }
    if int(analysis.get("rules") or 0) <= 0:
        return {
            "analysis": analysis,
            "blocker": _blocker(
                stage="case_input",
                user_message="用例文件没有解析出可执行异常规则。",
                why_it_matters="没有规则就无法验证字段异常。",
                how_to_fix=["在用例中写明 API、字段路径和异常动作，例如 字段: title 为空。"],
            ),
        }
    return {"analysis": analysis}


def _blockers_from_preflight(preflight: dict[str, Any], target: str = "") -> list[dict[str, Any]]:
    """Handle blockers from preflight using the supplied state and inputs."""
    checks = preflight.get("checks") or {}
    blockers: list[dict[str, Any]] = []
    for message in preflight.get("blockers") or []:
        stage = _stage_for_preflight_blocker(str(message), checks)
        blockers.append(
            _blocker(
                stage=stage,
                user_message=str(message),
                why_it_matters=_why_for_stage(stage),
                auto_fixable=_auto_fixable_for_stage(stage, checks),
                how_to_fix=_fixes_for_stage(stage, checks, str(message)),
                retry_tool="prepare_run",
                target=target,
            )
        )
    return blockers


def _warnings_from_preflight(preflight: dict[str, Any], target: str = "") -> list[dict[str, Any]]:
    """Handle warnings from preflight using the supplied state and inputs."""
    warnings = []
    for message in preflight.get("warnings") or []:
        item = {
            "stage": "preflight",
            "severity": "warning",
            "user_message": str(message),
        }
        if target:
            item["target"] = target
        warnings.append(item)
    return warnings


def _stage_for_preflight_blocker(message: str, checks: dict[str, Any]) -> str:
    """Handle stage for preflight blocker using the supplied state and inputs."""
    if "WDA" in message:
        return "ios_wda"
    if "mitm" in message or "代理端口" in message:
        return "proxy"
    if "WLAN 代理" in message:
        return "phone_proxy"
    if "adb" in message or "Android 设备" in message:
        return "device"
    if "HarmonyOS" in message or "hdc" in message or "harmony_device_discovery" in checks:
        return "harmony_device"
    if not checks.get("device_driver_supported", True):
        return "device_driver"
    return "preflight"


def _why_for_stage(stage: str) -> str:
    """Handle why for stage using the supplied state and inputs."""
    return {
        "ios_wda": "没有 WDA 就无法控制 iPhone：启动 App、读取元素、点击和截图都会失败。",
        "proxy": "异常规则依赖 mitmproxy 捕获并改写接口响应。",
        "phone_proxy": "手机流量不经过本机代理时，MCP 无法看到或改写目标接口。",
        "device": "没有可用设备就无法执行真实 App 验证。",
        "device_driver": "缺少设备驱动会导致自动化命令无处执行。",
        "harmony_device": "鸿蒙设备没有通过 HDC 发现或授权，无法执行页面操作和截图。",
        "case_input": "没有有效用例就无法生成可执行规则。",
        "app_launch": "没有显式包名或 bundle id 就无法可靠启动目标 App。",
    }.get(stage, "该前置条件失败会导致执行结果不可信。")


def _auto_fixable_for_stage(stage: str, checks: dict[str, Any]) -> bool:
    """Handle auto fixable for stage using the supplied state and inputs."""
    if stage == "ios_wda":
        return bool((checks.get("wda_start") or {}).get("ok") or (checks.get("wda") or {}).get("startable"))
    return False


def _fixes_for_stage(stage: str, checks: dict[str, Any], message: str) -> list[str]:
    """Handle fixes for stage using the supplied state and inputs."""
    if stage == "ios_wda":
        hint = (checks.get("wda") or {}).get("setup_hint")
        fixes = [hint] if hint else ["启动 WebDriverAgent，并确认 /status 可访问。"]
        fixes.append("如果本机已有启动脚本，可配置 MOBILE_AUTO_MCP_WDA_START_CMD，之后 MCP 会自动尝试启动。")
        return fixes
    if stage == "proxy":
        return ["停止占用端口的旧 mitmproxy/mitmdump，或传入未占用的 proxy_port。", "确认本机已安装 mitmdump。"]
    if stage == "phone_proxy":
        return ["手动把手机 WLAN 代理设置为 MCP 提示的本机 IP 和端口。", "确认手机已信任 mitmproxy 证书。"]
    if stage == "device":
        return ["连接设备并确认 adb devices 或 iOS WDA 状态正常。"]
    return [message]


def _next_actions(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Handle next actions using the supplied state and inputs."""
    return [
        {
            "stage": blocker["stage"],
            "action": blocker["how_to_fix"][0] if blocker.get("how_to_fix") else blocker["user_message"],
            "retry_tool": blocker.get("retry_tool", "prepare_run"),
        }
        for blocker in blockers
    ]


def _blocker(
    *,
    stage: str,
    user_message: str,
    why_it_matters: str,
    how_to_fix: list[str],
    severity: str = "blocker",
    auto_fixable: bool = False,
    retry_tool: str = "prepare_run",
    target: str = "",
) -> dict[str, Any]:
    """Handle blocker using the supplied state and inputs."""
    item = {
        "stage": stage,
        "severity": severity,
        "user_message": user_message,
        "why_it_matters": why_it_matters,
        "auto_fixable": auto_fixable,
        "how_to_fix": how_to_fix,
        "retry_tool": retry_tool,
    }
    if target:
        item["target"] = target
    return item


def _tool_status(name: str) -> dict[str, Any]:
    """Handle tool status using the supplied state and inputs."""
    path = shutil.which(name)
    return {"available": bool(path), "path": path or ""}


def _ios_tap_backend_status() -> dict[str, Any]:
    """Handle ios tap backend status using the supplied state and inputs."""
    command = os.environ.get("MOBILE_AUTO_MCP_IOS_TAP_COMMAND") or ""
    backend = (os.environ.get("MOBILE_AUTO_MCP_IOS_TAP_BACKEND") or "auto").lower()
    external_configured = bool(command)
    return {
        "configured": backend != "external" or external_configured,
        "backend": backend,
        "command_env": "MOBILE_AUTO_MCP_IOS_TAP_COMMAND" if command else "",
        "primary_strategy": "external_ios_tap" if backend == "external" else "wda_actions",
        "external_configured": external_configured,
        "purpose": "iOS 默认使用 W3C Actions 点击；外部命令是可选的更高优先级后端。",
    }


def _targets_for(target: str) -> list[str]:
    """Handle targets for using the supplied state and inputs."""
    normalized = (target or "android").lower()
    if normalized in {"both", "dual"}:
        return ["android", "ios"]
    if normalized in {"triple", "all", "三端"}:
        return ["android", "ios", "harmony"]
    return [normalized]
