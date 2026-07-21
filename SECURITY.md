# Security Policy

## Supported versions

Security fixes are applied to the latest release on the default branch.

## Reporting a vulnerability

Use the repository host's private security-reporting channel. Do not publish credentials, captured traffic, modified responses, certificates, device identifiers, screenshots, or private endpoints in a public issue.

Include the affected version, platform, reproduction conditions, impact, and a minimal synthetic proof when possible.

## Operational security

- Run only on devices and applications you are authorized to test.
- Install the npm package from the public registry and verify its provenance. The release workflow uses npm Trusted Publishing with short-lived OIDC identity and does not require a long-lived npm publish token.
- Keep the versioned runtime cache private. `MOBILE_AUTO_MCP_CACHE_HOME` contains executable Python environments; do not share it across untrusted operating-system users or restore it from unverified archives.
- Managed Python archives are pinned by URL, byte length, and SHA-256 digest. Do not bypass TLS or checksum verification to work around a bootstrap failure.
- Keep `MOBILE_AUTO_MCP_HOME` private; it contains execution and network evidence.
- Runtime directories and evidence files are forced to owner-only permissions, but operators remain responsible for disk backups and host access.
- Report servers bind to loopback by default. Binding to a LAN address is an explicit operator action and should be used only on a trusted network.
- Report export redacts common credential and personal-identity fields, URL query values, and raw mutation before/after values. Test fixtures should still avoid production personal data because arbitrary business field names cannot be classified perfectly.
- Oversized modified responses are replaced by an audit marker containing their original byte length and SHA-256 digest.
- Treat report bundles as sensitive even though known secret-shaped fields are redacted.
- Do not expose the report server outside a trusted network.
- Install and trust mitmproxy certificates only on dedicated test devices.
- Disable the phone Wi-Fi proxy and stop mitmproxy when capture is no longer required.
