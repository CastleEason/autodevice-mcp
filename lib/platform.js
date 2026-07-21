"use strict";

const path = require("node:path");

const SUPPORTED = new Set([
  "darwin-x64",
  "darwin-arm64",
  "linux-x64",
  "linux-arm64",
  "win32-x64",
  "win32-arm64",
]);

/** Maps a Node host tuple to the public runtime key. */
function normalizePlatform(platform, arch) {
  const key = `${platform}-${arch}`;
  if (!SUPPORTED.has(key)) {
    const error = new Error(`Unsupported platform: ${key}`);
    error.code = "unsupported_platform";
    throw error;
  }

  return { platform, arch, key };
}

/** Builds a cache path isolated by npm version and host runtime. */
function runtimeRoot({ env, homedir, version, platformKey }) {
  const cacheHome = env.MOBILE_AUTO_MCP_CACHE_HOME
    || path.join(homedir, ".cache", "autodevice-mcp");
  return path.join(cacheHome, version, platformKey);
}

module.exports = { normalizePlatform, runtimeRoot };
