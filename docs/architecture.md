# Architecture

`mobile-auto-mcp` is a local stdio MCP server. One process owns tool registration, case storage, device drivers, mitmproxy state, execution events, and report generation.

## Main components

- `mobile_auto_mcp.server`: stdio entrypoint.
- `mobile_auto_mcp.mcp_tools`: public MCP tool contract.
- `mobile_auto_mcp.cases`: Markdown case parsing and rule import.
- `mobile_auto_mcp.execution.adapters`: isolated iOS/WDA and HarmonyOS/HDC transports.
- `mobile_auto_mcp.execution.probe`: fresh request probing and current-run device/IP binding.
- `mobile_auto_mcp.execution`: readiness, cross-platform device facade, navigation, lane state machines, failure playbooks, and the standard Runner.
- `mobile_auto_mcp.proxy`: response mutation, state isolation, proxy process ownership, device Wi-Fi proxy adapters, and trace evidence.
- `mobile_auto_mcp.reports`: report generation, visual comparison, and report hosting.
- `mobile_auto_mcp.state`: isolated rules, sessions, runs, and navigation knowledge.

## Execution flow

1. Acquire the workspace run lock.
2. Parse or select rules and hard-block empty rules, missing requested IDs, missing APIs, absent mutation assets, or missing page anchors.
3. Run host and device readiness checks without creating a formal Session.
4. Read each selected phone's current Wi-Fi address and prove one local proxy candidate is routable from every phone.
5. Start or safely reuse the project-owned mitmproxy listener.
6. Snapshot, configure, and read back each selected device proxy.
7. Create the formal Session only after readiness succeeds.
8. Run independent platform lanes with fresh navigation snapshots and stage budgets.
9. Bind each lane to a fresh observed client IP and probe the exact host, path, and method.
10. Activate one rule, trigger traffic, capture hits, save modified response JSON, verify the page, and capture screenshots.
11. Aggregate platform results and write Pillow metrics as `visual_precheck` only.
12. Retain the verified proxy and durable recovery evidence for continued capture.
13. Export the report as `awaiting_review`; only explicit semantic or human reviews can close it.
14. On demand, `restore_retained_proxy` restores every snapshot and stops the verified owned proxy.

All rule/run mutations and reusable knowledge updates use workspace-scoped advisory file locks in addition to atomic private writes, preventing cross-process lost updates. Device proxy snapshots are persisted before the first phone write; an apply failure clears a newly created recovery record only after rollback is verified and only when no older retained lifecycle existed.

## Isolation

Runtime data is scoped by base home, tenant, workspace, application, Session, lane, rule, and activation identifier. Historical client IPs are never loaded into a new coordinator; every execution starts unbound and requires a fresh observed request.

Runtime directories use owner-only mode `0700`, evidence files use `0600`, and report output cannot escape the selected workspace data home. Modified-response evidence is size-bounded before append-only persistence.

The mitmproxy addon is loaded by file path from `ProxyManager`, so `mobile_auto_mcp/proxy/proxy_addon.py` is a runtime file even though it is not imported as a normal Python module.
