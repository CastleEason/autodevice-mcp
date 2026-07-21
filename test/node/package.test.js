const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);
const packageRoot = path.resolve(__dirname, "../..");
const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";

const ALLOWED_ROOT_FILES = new Set([
  "LICENSE",
  "README.md",
  "SECURITY.md",
  "package.json",
  "pyproject.toml",
]);
const ALLOWED_PREFIXES = [
  "bin/",
  "docs/",
  "lib/",
  "mobile_auto_mcp/",
  "runtime/",
  "scripts/",
];
const FORBIDDEN_ROOT = /^(?:tests?|\.github|docs\/superpowers|\.git|work|reports?|devices?|screenshots?)(?:\/|$)/i;
const FORBIDDEN_GENERATED = /(^|\/)__pycache__(?:\/|$)|\.pyc$/i;
const FORBIDDEN_SECRET = /(^|\/)(?:\.env(?:\..*)?|credentials?(?:\..*)?|[^/]+\.(?:pem|key|p12|mobileconfig))$/i;

async function packInto(destination) {
  const { stdout } = await execFileAsync(
    npmCommand,
    ["pack", "--json", "--ignore-scripts", "--pack-destination", destination],
    { cwd: packageRoot, encoding: "utf8", shell: process.platform === "win32" },
  );
  const result = JSON.parse(stdout);
  assert.equal(result.length, 1, stdout);
  return result[0];
}

test("npm tarball contains only the public distribution whitelist", async () => {
  const destination = await fs.mkdtemp(path.join(os.tmpdir(), "autodevice-pack-"));
  try {
    const packed = await packInto(destination);
    const paths = packed.files.map((entry) => entry.path);

    for (const entry of paths) {
      assert.equal(path.isAbsolute(entry), false, `absolute package path: ${entry}`);
      assert.doesNotMatch(entry, FORBIDDEN_ROOT, `private package path: ${entry}`);
      assert.doesNotMatch(entry, FORBIDDEN_GENERATED, `generated package path: ${entry}`);
      assert.doesNotMatch(entry, FORBIDDEN_SECRET, `secret-shaped package path: ${entry}`);
      assert.ok(
        ALLOWED_ROOT_FILES.has(entry) || ALLOWED_PREFIXES.some((prefix) => entry.startsWith(prefix)),
        `path is outside the public package whitelist: ${entry}`,
      );
    }

    for (const required of [
      "package.json",
      "bin/autodevice-mcp.js",
      "lib/bootstrap.js",
      "mobile_auto_mcp/server.py",
      "runtime/constraints.txt",
      "runtime/python-build-standalone.json",
    ]) {
      assert.ok(paths.includes(required), `required package file is missing: ${required}`);
    }
  } finally {
    await fs.rm(destination, { recursive: true, force: true });
  }
});
