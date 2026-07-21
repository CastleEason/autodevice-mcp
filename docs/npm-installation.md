# npm installation

## Quick start

Run the package without a global install:

```bash
npx -y autodevice-mcp --doctor
npx -y autodevice-mcp --bootstrap-only
```

`--doctor` reports the selected platform, interpreter, cache location, source digest, and readiness without changing the cache. `--bootstrap-only` creates or repairs the isolated runtime and exits without starting MCP stdio.

The supported npm hosts are macOS, Linux, and Windows on x64 or arm64. Node.js 20 or newer is required. The launcher prefers a compatible Python 3.12 supplied through `MOBILE_AUTO_MCP_PYTHON` or found on `PATH`; otherwise it downloads the pinned, checksum-verified managed Python build for the current host.

## MCP clients

Cursor:

```json
{
  "mcpServers": {
    "autodevice-mcp": {
      "command": "npx",
      "args": ["-y", "autodevice-mcp"]
    }
  }
}
```

Codex:

```toml
[mcp_servers.autodevice_mcp]
command = "npx"
args = ["-y", "autodevice-mcp"]
startup_timeout_sec = 120
```

The first launch can create a Python environment and install pinned dependencies, so retain the 120-second startup allowance. Bootstrap diagnostics go to stderr; stdout remains reserved for MCP JSON-RPC messages.

## Cache and private data

The runtime cache defaults to:

```text
~/.cache/autodevice-mcp/<npm-version>/<platform-architecture>/
```

Set `MOBILE_AUTO_MCP_CACHE_HOME` to relocate it. The launcher publishes a cache generation only after validation, serializes concurrent installers with an ownership-fenced lock, and rebuilds stale or source-mismatched generations. Removing a version directory is safe while no `autodevice-mcp` process is running; the next launch rebuilds it.

Runtime dependencies and application evidence are separate. `MOBILE_AUTO_MCP_HOME` contains imported rules, sessions, network events, modified responses, screenshots, and reports. Give each operator a private location and do not place it in a shared source tree or shared runtime cache.

## Proxy lifecycle reminder

Formal `run_cases` execution can change an authorized device's Wi-Fi proxy after recording and verifying its prior state. A successful run intentionally retains that proxy and the project-owned mitmproxy process for continued capture.

Call `restore_retained_proxy` when capture ends, before changing networks, or before returning the device. If restoration fails, keep the recovery record and retry; do not manually delete the evidence first.

The report server binds to loopback by default. An all-interface bind is explicit LAN sharing and should be used only after reviewing the report directory for sensitive evidence and confirming the network is trusted.

## Troubleshooting

- `python_not_found` or a managed-runtime download failure: install Python 3.12 and set `MOBILE_AUTO_MCP_PYTHON` to its executable, then run `npx -y autodevice-mcp --bootstrap-only`.
- Certificate verification or proxy failure during bootstrap: configure the host's approved HTTPS proxy and CA bundle for npm/Python. Do not disable TLS verification.
- `runtime_lock_timeout`: confirm no other launcher is actively installing, then retry. Expired or dead-owner locks are reclaimed automatically.
- `unsupported_platform`: use one of the documented macOS/Linux/Windows x64 or arm64 hosts.
- Client startup timeout: run `--bootstrap-only` once and retain `startup_timeout_sec = 120` for the MCP client.
- Protocol parse failure: verify the client launches `npx -y autodevice-mcp` directly. Wrappers must not write banners or logs to stdout.
- Device or proxy preflight failure: follow [device setup](device-setup.md) and the detailed [troubleshooting guide](troubleshooting.md); preflight stops before unsafe partial execution.
