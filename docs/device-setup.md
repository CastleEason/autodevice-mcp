# Device Setup

Use dedicated test devices and a network where the phones can route to the host running `autodevice-mcp`.

## Android

- Install `adb` and authorize the device.
- Confirm `adb devices` lists the expected serial.
- Use an explicit application package in `target_app_package`.

## iOS

- Install Xcode command-line tools and `iproxy`.
- Build, sign, install, and trust WebDriverAgent on the device before normal runs.
- Provide `MOBILE_AUTO_MCP_WDA_URL` and, when required, `MOBILE_AUTO_MCP_IOS_UDID`.
- Normal runs do not reinstall or re-sign WDA. Use the explicit `repair_wda` tool for authorized repairs.

## HarmonyOS

- Install `hdc` and authorize the device.
- Confirm the device is visible before running `preflight`.
- Use an explicit application package for launches.

## Proxy and certificates

Proxy-backed HTTPS mutation requires the test device to trust the mitmproxy certificate. Do this only on dedicated devices. The Runner preserves an advertised Wi-Fi prefix when available, rejects incompatible subnets, and requires a source-bound route probe from the chosen host address to every phone. SSID names alone are not route evidence. iOS/Harmony proxy apply and restore also stop if the current SSID differs from the captured snapshot.

The Runner intentionally retains the managed proxy after execution. Call `restore_retained_proxy` when finished; do not delete the workspace recovery file or kill an arbitrary PID manually.

Start with `doctor`, `runtime_status`, and read-only `preflight` before any formal run.
