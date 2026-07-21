const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { normalizePlatform, runtimeRoot } = require("../../lib/platform");

test("normalizes every supported host tuple", () => {
  for (const [platform, arch] of [
    ["darwin", "x64"],
    ["darwin", "arm64"],
    ["linux", "x64"],
    ["linux", "arm64"],
    ["win32", "x64"],
    ["win32", "arm64"],
  ]) {
    assert.deepEqual(normalizePlatform(platform, arch), {
      platform,
      arch,
      key: `${platform}-${arch}`,
    });
  }
});

test("rejects unsupported host tuples", () => {
  assert.throws(() => normalizePlatform("freebsd", "x64"), /Unsupported platform/);
  assert.throws(() => normalizePlatform("linux", "ia32"), /Unsupported platform/);
});

test("uses a version-isolated explicit cache root", () => {
  assert.equal(
    runtimeRoot({
      env: { MOBILE_AUTO_MCP_CACHE_HOME: "/cache" },
      homedir: "/home/u",
      version: "0.3.0",
      platformKey: "linux-x64",
    }),
    path.join("/cache", "0.3.0", "linux-x64"),
  );
});

test("keeps the default runtime cache separate from MOBILE_AUTO_MCP_HOME", () => {
  assert.equal(
    runtimeRoot({
      env: { MOBILE_AUTO_MCP_HOME: "/persistent/config" },
      homedir: "/home/u",
      version: "0.3.0",
      platformKey: "darwin-arm64",
    }),
    path.join("/home/u", ".cache", "autodevice-mcp", "0.3.0", "darwin-arm64"),
  );
});
