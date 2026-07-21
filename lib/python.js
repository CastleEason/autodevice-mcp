"use strict";

const { execFile } = require("node:child_process");

/** Returns the ordered Python commands appropriate for the current host. */
function platformCandidates(platform = process.platform) {
  const candidates = ["python3.12", "python3", "python"];
  if (platform === "win32") candidates.push(["py", "-3.12"]);
  return candidates;
}

/** Executes a candidate with --version without joining argv through a shell. */
function runCandidateVersion(candidate) {
  const argv = Array.isArray(candidate) ? candidate : [candidate];
  const [command, ...args] = argv;

  return new Promise((resolve, reject) => {
    execFile(command, [...args, "--version"], (error, stdout, stderr) => {
      if (error) {
        reject(error);
        return;
      }
      resolve((stdout || stderr).trim());
    });
  });
}

/** Extracts a stable three-part version from Python's version response. */
function parseVersion(value) {
  const match = /^(?:Python\s+)?(\d+)\.(\d+)\.(\d+)$/.exec(String(value).trim());
  if (!match) return null;
  return { major: Number(match[1]), minor: Number(match[2]), version: match.slice(1).join(".") };
}

/** Finds the first explicitly configured or platform candidate running Python 3.12. */
async function findPython312({
  env = process.env,
  candidates = platformCandidates(),
  runVersion = runCandidateVersion,
} = {}) {
  const ordered = env.MOBILE_AUTO_MCP_PYTHON
    ? [env.MOBILE_AUTO_MCP_PYTHON, ...candidates]
    : candidates;

  for (const executable of ordered) {
    try {
      const parsed = parseVersion(await runVersion(executable));
      if (parsed?.major === 3 && parsed.minor === 12) {
        return { executable, version: parsed.version };
      }
    } catch {
      // A missing or unusable candidate is expected during ordered discovery.
    }
  }

  return null;
}

module.exports = { findPython312 };
