# Real-device regression

Physical-device tests are disabled by default because they can launch applications, configure proxies, and generate traffic on authorized test phones.

Install development dependencies:

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
```

Create a private JSON file outside the repository. It must contain `run_cases` arguments and must not contain production credentials:

```json
{
  "android_serial": "<android-serial>",
  "ios_udid": "<ios-udid>",
  "harmony_serial": "<harmony-serial>",
  "wda_url": "http://127.0.0.1:8100",
  "run_cases": {
    "app_id": "qa-sandbox",
    "base_home": "/private/path/autodevice-mcp-data",
    "case_file": "/private/path/test-cases.md",
    "target_page": "<generic-page-name>",
    "target_page_assertions": [{"any_text": ["<stable-page-anchor>"]}],
    "target_app_packages": {
      "android": "<android-package>",
      "ios": "<ios-bundle-id>",
      "harmony": "<harmony-bundle>"
    },
    "navigation_paths": {},
    "request_trigger_paths": {},
    "android_serial": "<android-serial>",
    "ios_udid": "<ios-udid>",
    "harmony_serial": "<harmony-serial>",
    "wda_url": "http://127.0.0.1:8100"
  }
}
```

Run the matrix explicitly:

```bash
MOBILE_AUTO_MCP_RUN_REAL_DEVICE_TESTS=1 \
MOBILE_AUTO_MCP_REAL_RUN_CONFIG=/private/path/real-device.json \
./.venv/bin/python -m pytest -m real_device -q
```

The chain is successful only when all three lanes produce valid mutation and page-anchor evidence and the result stops at `awaiting_review`. Final `reviewed` status still requires explicit semantic or human decisions. Call `restore_retained_proxy` after capture is complete.
