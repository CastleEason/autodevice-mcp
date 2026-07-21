# Report Format

An archived Session report contains human-readable and machine-readable evidence.

## Files

- `summary.json`: aggregate status and evidence integrity.
- `runs.json`: per-rule, per-platform execution results, `visual_precheck`, and final `visual_review`.
- `modified_responses.json`: every redacted response JSON that was actually modified.
- `trace.json`: proxy events, execution events, hits, and modified responses.
- `mitmproxy_events.jsonl`: append-only proxy evidence.
- `execution_events.jsonl`: append-only lane-stage evidence.
- `report.md`: compact tabular summary.
- `index.html`: three-platform comparison report with expandable JSON and screenshots.
- `manifest.json`: file inventory, screenshot paths, integrity result, and manual proxy action.

## HTML columns

The main table contains case, mutation, expected result, Android, iOS, HarmonyOS, cross-platform conclusion, and verdict columns. Each case is followed by an expandable row containing every stored modified response JSON grouped by platform and hit sequence. The report labels Pillow output as a non-final visual precheck and renders the final semantic review separately.

## Stable statuses

- `invalid_execution_contract`: rules, API, mutation asset, requested IDs, or page anchor failed before device work.
- `readiness_failed`: tools, devices, WDA, proxy ownership, or host-route proof failed before Session creation.

Every report row displays the exact HTTP method plus host/path contract. The bundle stores each modified response JSON in order, while redacting common credentials, device identifiers, account identifiers, names, phone numbers, email addresses, and URL query values. Proxy hit/event evidence keeps structural mutation metadata but omits raw before/after values.

Report integrity fails when execution events, mutation hits, modified responses, verified page anchors, final semantic review, or proxy lifecycle evidence are missing. A Pillow visual comparison is only a precheck and cannot produce the final pass state.
- `partial`: at least one execution gate lacks valid evidence.
- `awaiting_review`: response changes and page anchors are proven, but final semantic reviews are incomplete.
- `reviewed`: every expected lane has an explicit final pass.
- `failed`: final reviews are complete and at least one lane failed.
- `workspace_busy`: another process owns the workspace run lock.

`visual_precheck.status` can be `similar`, `different`, or `needs_check`; these values never become final `passed` or `failed` by themselves.

## Integrity gate

The report cannot claim success when required execution events are absent, a changed run lacks its modified response, a matching activation lacks a proxy hit, the page anchor is skipped/unverified, final semantic review is incomplete, or the device proxy lifecycle was not verified.

Known secret-shaped keys and URL query parameters are recursively redacted before modified responses are written. Redaction reduces risk but does not make a report safe for unrestricted publication.

If a serialized modified response exceeds `MOBILE_AUTO_MCP_MAX_RESPONSE_BYTES` (10 MiB by default), the stored body is replaced by a truncation marker containing the original byte length and SHA-256 digest. Screenshots are copied only when their resolved source path is inside the active workspace data home.
