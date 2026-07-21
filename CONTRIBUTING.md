# Contributing

Thank you for improving `mobile-auto-mcp`.

## Before opening a change

1. Open an issue for behavior changes or new device backends.
2. Keep core code application-agnostic: do not embed product names, page copy, fixed business coordinates, credentials, or private endpoints.
3. Preserve the single-call `run_cases` contract and independent platform lanes.
4. Never include real captures, screenshots, modified responses, device identifiers, certificates, or operator paths.

## Local checks

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -e .
./.venv/bin/python -m compileall -q mobile_auto_mcp
./.venv/bin/python -c "from mcp.server.fastmcp import FastMCP; from mobile_auto_mcp.mcp_tools import register_all_tools; m=FastMCP('check'); register_all_tools(m); print(len(m._tool_manager.list_tools()))"
```

Changes that touch a real device must document the platform, OS version, driver/WDA/HDC state, and the non-destructive evidence used for verification.

## Pull requests

- Explain the user-visible effect and safety impact.
- Keep changes focused and reversible.
- Update public documentation when configuration, MCP tools, report fields, or proxy behavior changes.
- Use synthetic data in examples.
