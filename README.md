# mobile-auto-mcp

`mobile-auto-mcp` is a local MCP server for repeatable abnormal API-response testing on Android, iOS, and HarmonyOS devices. It combines device automation, mitmproxy response mutation, evidence capture, cross-platform comparison, and auditable HTML/JSON reports behind one stdio MCP process.

## Highlights

- Imports Markdown test cases and converts them into reusable mutation rules.
- Runs Android, iOS, and HarmonyOS lanes independently in one `run_cases` call.
- Validates tools, devices, proxy reachability, and page anchors before formal execution.
- Discovers each device client IP from a fresh request instead of static configuration.
- Stores every modified response JSON with recursive sensitive-value redaction.
- Uses owner-only permissions (`0700` directories and `0600` evidence files) for runtime data.
- Bounds each stored modified response; oversized bodies become size-and-SHA-256 audit markers.
- Captures screenshots, request hits, execution events, non-final visual prechecks, and final semantic reviews.
- Produces a fixed three-platform HTML report plus machine-readable artifacts.
- Persists scoped navigation and field-alias knowledge for later runs.

## Safety model

`preflight` is read-only. A formal `run_cases` execution may configure the connected devices' Wi-Fi HTTP proxy after first taking a snapshot and verifying the new value. If readiness fails, partially applied settings are rolled back.

After a formal run, the verified phone proxy and mitmproxy process are intentionally retained for continued capture. Original proxy snapshots and owned-process evidence are persisted across MCP restarts. When capture is no longer required, call `restore_retained_proxy`; it restores every phone, verifies the readback, and stops only the project-owned mitmproxy process. A failed cleanup retains the recovery record for another attempt.

Only one `run_cases` call can mutate a workspace at a time. A concurrent call returns `workspace_busy` before preflight.

Do not run this project against devices, applications, or traffic you are not authorized to test.

## Requirements

- Python 3.12 or newer
- Android: `adb` and an authorized device
- iOS: Xcode command-line tools, `iproxy`, and a prepared WebDriverAgent
- HarmonyOS: `hdc` and an authorized device
- Phones and the host must have a routable network path for proxy-backed runs

## Installation

Clone the repository, then install it in an isolated environment:

```bash
cd mobile-auto-mcp
python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e .
```

The package installs a `mobile-auto-mcp` console command. You can also run the module directly:

```bash
./.venv/bin/mobile-auto-mcp
# or
./.venv/bin/python -m mobile_auto_mcp.server
```

Both commands use stdio transport and are intended to be started by an MCP client.

## MCP client configuration

Use absolute paths and a private writable data directory:

```json
{
  "mcpServers": {
    "mobile-auto-mcp": {
      "command": "/absolute/path/mobile-auto-mcp/.venv/bin/python",
      "args": ["-m", "mobile_auto_mcp.server"],
      "env": {
        "MOBILE_AUTO_MCP_HOME": "/absolute/private/path/mobile-auto-mcp-data"
      }
    }
  }
}
```

Codex TOML example:

```toml
[mcp_servers.mobile_auto_mcp]
command = "/absolute/path/mobile-auto-mcp/.venv/bin/python"
args = ["-m", "mobile_auto_mcp.server"]
startup_timeout_sec = 30

[mcp_servers.mobile_auto_mcp.env]
MOBILE_AUTO_MCP_HOME = "/absolute/private/path/mobile-auto-mcp-data"
```

Each operator should use a separate `MOBILE_AUTO_MCP_HOME`. It contains rules, navigation knowledge, sessions, proxy events, modified responses, screenshots, and reports.

`MOBILE_AUTO_MCP_MAX_RESPONSE_BYTES` controls the maximum serialized modified-response body retained per hit and defaults to 10 MiB. Explicit report output paths must remain inside the selected workspace data home.

## First run

1. Call `doctor` to verify Python dependencies.
2. Call `runtime_status` to inspect devices, WDA/HDC, and proxy-port state.
3. Call the read-only `preflight` tool for the target platform.
4. Import a case containing a stable rule, exact API, and mutation/patch asset.
5. Execute it through one `run_cases` call.
6. Provide `target_page` or explicit `target_page_assertions`; missing anchors are a hard block.
7. Review `index.html`, `modified_responses.json`, and `manifest.json`, then submit final VLM or human decisions.
8. Call `restore_retained_proxy` when capture is no longer needed.

`proxy_host` is an optional preference, not a bypass. The address must be a current local candidate, satisfy every device's advertised Wi-Fi prefix, and pass a source-bound route probe to every selected phone. If any device address is unavailable or any route cannot be proven, readiness stops before changing the proxy.

Every executable rule must contain an exact `host`, path-bearing `api`, HTTP `method`, and at least one non-empty mutation asset. Markdown such as `GET https://api.example.test/v1/profile` is imported end-to-end. For path-only source material, use `apply_case_asset_overrides` to supply `host_override` and `method_override` before running.

The report server binds to `127.0.0.1` by default. LAN sharing is explicit: pass a LAN bind host only when the report directory has been reviewed for sensitive evidence and the network is trusted.

## Report bundle

Every archived report contains:

- `summary.json`
- `runs.json`
- `modified_responses.json`
- `trace.json`
- `mitmproxy_events.jsonl`
- `execution_events.jsonl`
- `report.md`
- `index.html`
- `manifest.json`
- copied screenshots

The integrity gate prevents the HTML report from claiming success when execution events, modified-response evidence, matching proxy hits, or verified proxy lifecycle evidence are missing.

Built-in Pillow comparison writes `visual_precheck` only. It never sets `passed` or `failed`. Successful execution returns `awaiting_review`; a Session becomes `reviewed` only after every lane has an explicit final pass.

See [architecture](docs/architecture.md), [device setup](docs/device-setup.md), [report format](docs/report-format.md), [real-device regression](docs/real-device-regression.md), and [troubleshooting](docs/troubleshooting.md) for details.

## Development and disclosure

- Contributions: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security reports: [SECURITY.md](SECURITY.md)
- Conduct: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- Changes: [CHANGELOG.md](CHANGELOG.md)

## License

Licensed under the [Apache License 2.0](LICENSE).
