const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFile, spawn, spawnSync } = require("node:child_process");
const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);
const packageRoot = path.resolve(__dirname, "../..");
const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";

function currentPython312() {
  const candidates = [
    process.env.MOBILE_AUTO_MCP_PYTHON,
    process.env.PYTHON,
    "python3.12",
    "python",
  ].filter(Boolean);
  for (const candidate of candidates) {
    const result = spawnSync(candidate, ["-c", "import sys; print(sys.executable); print(sys.version_info[:2])"], {
      encoding: "utf8",
      windowsHide: true,
    });
    const lines = result.stdout?.trim().split(/\r?\n/) ?? [];
    if (result.status === 0 && lines.at(-1) === "(3, 12)") return lines[0];
  }
  throw new Error("A Python 3.12 executable is required for the installed-tarball smoke test");
}

function pythonCertificateBundle(python) {
  const result = spawnSync(
    python,
    ["-c", "import certifi; print(certifi.where())"],
    { encoding: "utf8", windowsHide: true },
  );
  return result.status === 0 ? result.stdout.trim() : null;
}

function firstStdoutLine(child) {
  return new Promise((resolve, reject) => {
    let stdout = Buffer.alloc(0);
    let stderr = "";
    const onData = (chunk) => {
      stdout = Buffer.concat([stdout, chunk]);
      const newline = stdout.indexOf(0x0a);
      if (newline !== -1) {
        cleanup();
        resolve({ line: stdout.subarray(0, newline), stderr });
      }
    };
    const onStderr = (chunk) => { stderr += chunk.toString("utf8"); };
    const onExit = (code, signal) => {
      cleanup();
      reject(new Error(`MCP exited before its first stdout frame: code=${code} signal=${signal} stderr=${stderr}`));
    };
    const onError = (error) => {
      cleanup();
      reject(error);
    };
    const cleanup = () => {
      child.stdout.off("data", onData);
      child.stderr.off("data", onStderr);
      child.off("exit", onExit);
      child.off("error", onError);
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onStderr);
    child.once("exit", onExit);
    child.once("error", onError);
  });
}

async function stopChild(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise((resolve) => child.once("exit", resolve));
  child.stdin.end();
  await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 1_000))]);
  if (child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true });
  } else {
    child.kill();
  }
  await Promise.race([
    exited,
    new Promise((resolve) => setTimeout(resolve, 5_000)),
  ]);
}

test("installed npm tarball starts with an MCP initialize response on stdout", { timeout: 240_000 }, async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "autodevice-mcp-smoke-"));
  const packDirectory = path.join(temporary, "pack");
  const projectDirectory = path.join(temporary, "project");
  const cacheDirectory = path.join(temporary, "cache");
  let child;
  try {
    await fs.mkdir(packDirectory);
    await fs.mkdir(projectDirectory);
    const { stdout } = await execFileAsync(
      npmCommand,
      ["pack", "--json", "--ignore-scripts", "--pack-destination", packDirectory],
      { cwd: packageRoot, encoding: "utf8" },
    );
    const [{ filename }] = JSON.parse(stdout);
    const tarball = path.join(packDirectory, filename);
    const python = currentPython312();
    const certificateBundle = pythonCertificateBundle(python);
    const env = {
      ...process.env,
      MOBILE_AUTO_MCP_CACHE_HOME: cacheDirectory,
      MOBILE_AUTO_MCP_PYTHON: python,
      ...(process.env.SSL_CERT_FILE || !certificateBundle ? {} : { SSL_CERT_FILE: certificateBundle }),
    };

    await execFileAsync(npmCommand, ["init", "-y"], { cwd: projectDirectory, env, encoding: "utf8" });
    await execFileAsync(npmCommand, ["install", tarball], {
      cwd: projectDirectory,
      env,
      encoding: "utf8",
      timeout: 180_000,
    });

    const executable = process.platform === "win32"
      ? path.join(projectDirectory, "node_modules", ".bin", "autodevice-mcp.cmd")
      : path.join(projectDirectory, "node_modules", ".bin", "autodevice-mcp");
    child = spawn(executable, [], {
      cwd: projectDirectory,
      env,
      stdio: ["pipe", "pipe", "pipe"],
      shell: process.platform === "win32",
      windowsHide: true,
    });
    const responseLine = firstStdoutLine(child);
    child.stdin.write(`${JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "npm-tarball-smoke", version: "1.0.0" },
      },
    })}\n`);

    const { line, stderr } = await responseLine;
    assert.ok(line.length > 0, `empty MCP response; stderr=${stderr}`);
    if (process.env.MCP_SMOKE_EVIDENCE === "1") {
      process.stderr.write(`MCP_FIRST_STDOUT_HEX=${line.toString("hex")}\n`);
      process.stderr.write(`MCP_FIRST_STDOUT_UTF8=${line.toString("utf8")}\n`);
    }
    const response = JSON.parse(line.toString("utf8"));
    assert.equal(response.jsonrpc, "2.0");
    assert.equal(response.id, 1);
    assert.equal(response.result?.serverInfo?.name, "autodevice-mcp");
  } finally {
    await stopChild(child);
    await fs.rm(temporary, { recursive: true, force: true });
  }
});
