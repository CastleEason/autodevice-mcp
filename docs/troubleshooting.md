# Troubleshooting

## Proxy port is already in use

The Runner reuses a listener only when its stored home, port, addon path, PID, and process command prove project ownership. Stop an unrelated listener or select another port.

## A phone cannot reach the proxy

Inspect `proxy_host_unproven` and `proxy_host_unreachable` evidence. The selected host must be a current local candidate, match every advertised device prefix, and complete a source-bound route probe to every phone. VLAN/client isolation can block the route even when addresses look similar. Computer and phone SSID names are informational only.

## A rule never receives a hit

Verify the page anchor, request-trigger path, API matcher, and fresh client-IP probe. Historical IP values are never accepted without a new request.

## iOS WDA is unavailable

Check that the device is unlocked, trusted, connected, and running the expected signed WebDriverAgent. Normal execution does not reinstall or re-sign WDA. Use `repair_wda` only when that action is explicitly authorized.

Use the returned readiness stage to narrow the failure: `transport` means `/status` is unreachable, `status` means WDA did not declare itself ready, `session` means XCTest could not create a session, and `command` means the session could not complete the read-only window-size probe. The Runner does not create the navigation driver until all stages pass.

## Android proxy affects every network

Current releases never write Android global HTTP proxy settings. A formal run opens `android.settings.WIFI_SETTINGS`, identifies the connected SSID, edits that Wi-Fi network through semantic system labels, and verifies the visible host and port. If the current network or editable proxy controls cannot be identified, the run stops without falling back to a global proxy.

## Report says evidence is incomplete

Inspect `manifest.json` for issue codes, then correlate `execution_events.jsonl`, `mitmproxy_events.jsonl`, `trace.json`, and `modified_responses.json` by Session, lane, rule, and activation identifier.

## Network remains proxied after execution

This is intentional. Call `restore_retained_proxy` when capture is no longer required. If it returns `device_restore_failed` or `proxy_stop_failed`, keep the workspace intact and retry after restoring device connectivity; the recovery record is deliberately preserved.

## A run returns awaiting_review

This is expected after valid execution. Pillow comparison is only a precheck. Use `prepare_visual_review`, then submit explicit results with `apply_visual_reviews` or `apply_manual_reviews`.
