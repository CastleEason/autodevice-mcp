const test = require("node:test");
const assert = require("node:assert/strict");
const manifest = require("../../runtime/python-build-standalone.json");
const { selectRuntimeAsset } = require("../../lib/runtime-manifest");

const PUBLIC_PLATFORMS = [
  "darwin-x64",
  "darwin-arm64",
  "linux-x64",
  "linux-arm64",
  "win32-x64",
  "win32-arm64",
];

test("pins one CPython 3.12 release for every public platform", () => {
  assert.equal(manifest.source, "https://github.com/astral-sh/python-build-standalone/releases/tag/20260718");
  assert.equal(manifest.pythonVersion, "3.12.13");
  assert.deepEqual(Object.keys(manifest.assets).sort(), PUBLIC_PLATFORMS.sort());

  for (const platformKey of PUBLIC_PLATFORMS) {
    const asset = selectRuntimeAsset(manifest, platformKey);
    assert.deepEqual(Object.keys(asset).sort(), ["archiveType", "bytes", "sha256", "url"]);
    assert.match(asset.url, /^https:\/\/github\.com\/astral-sh\/python-build-standalone\/releases\/download\/20260718\//);
    assert.match(asset.sha256, /^[a-f0-9]{64}$/);
    assert.ok(Number.isSafeInteger(asset.bytes) && asset.bytes > 0);
    assert.equal(asset.archiveType, "tar.gz");
  }
});

test("rejects an unsupported platform key with a stable error code", () => {
  assert.throws(() => selectRuntimeAsset(manifest, "freebsd-x64"), {
    code: "unsupported_platform",
  });
});
