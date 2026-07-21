const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");

const { ensureRuntime } = require("../../lib/bootstrap");

async function withFixture(run) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "autodevice-bootstrap-"));
  const packageRoot = path.join(directory, "package");
  const constraintsPath = path.join(packageRoot, "runtime", "constraints.txt");
  const cacheHome = path.join(directory, "cache");
  await fs.mkdir(path.join(packageRoot, "mobile_auto_mcp"), { recursive: true });
  await fs.mkdir(path.dirname(constraintsPath), { recursive: true });
  await fs.writeFile(path.join(packageRoot, "pyproject.toml"), "[project]\nname='autodevice-mcp'\n");
  await fs.writeFile(path.join(packageRoot, "mobile_auto_mcp", "server.py"), "print('fixture')\n");
  await fs.writeFile(constraintsPath, "mcp==1.28.1\n");

  try {
    await run({ directory, packageRoot, constraintsPath, cacheHome });
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
}

function harness(fixture, overrides = {}) {
  let installs = 0;
  const calls = [];
  const logs = [];
  let downloads = 0;
  const runFile = async (command, args) => {
    calls.push({ command, args: [...args] });
    if (args[0] === "-m" && args[1] === "venv") {
      if (overrides.venvError) throw overrides.venvError;
      const environment = args[2];
      const executable = path.join(environment, "bin", "python");
      await fs.mkdir(path.dirname(executable), { recursive: true });
      await fs.writeFile(executable, "fixture python");
      return { stdout: "", stderr: "" };
    }
    if (args.length === 1 && args[0] === "--version") {
      const version = String(command).includes(`${path.sep}managed${path.sep}`) ? "3.12.13" : "3.12.7";
      return { stdout: `Python ${version}\n`, stderr: "" };
    }
    if (args[0] === "-m" && args[1] === "pip") {
      installs += 1;
      if (overrides.installDelay) await overrides.installDelay();
      if (overrides.installError) throw overrides.installError;
      if (overrides.afterInstall) await overrides.afterInstall();
      return { stdout: "", stderr: "" };
    }
    throw new Error(`Unexpected process call: ${command} ${args.join(" ")}`);
  };

  return {
    options: {
      packageRoot: fixture.packageRoot,
      constraintsPath: fixture.constraintsPath,
      env: { MOBILE_AUTO_MCP_CACHE_HOME: fixture.cacheHome },
      homedir: fixture.directory,
      version: "0.3.0",
      platform: "darwin",
      arch: "arm64",
      logger: (message) => logs.push(message),
      deps: {
        findPython312: async () => ({ executable: "/system/python3.12", version: "3.12.7" }),
        runFile,
        downloadVerified: async (_asset, destination) => {
          downloads += 1;
          await fs.writeFile(destination, "managed archive");
        },
        extractArchive: async (_asset, _archive, destination) => {
          const executable = path.join(destination, "python", "bin", "python3");
          await fs.mkdir(path.dirname(executable), { recursive: true });
          await fs.writeFile(executable, "managed fixture python");
        },
        randomUUID: () => "fixture-id",
        sleep: async () => new Promise((resolve) => setTimeout(resolve, 5)),
      },
    },
    calls,
    logs,
    get downloads() { return downloads; },
    get installs() { return installs; },
  };
}

test("a valid ready manifest returns without reinstalling", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture);
    const first = await ensureRuntime(state.options);
    const second = await ensureRuntime(state.options);

    assert.equal(state.installs, 1);
    assert.equal(second.root, first.root);
    assert.equal(second.python, first.python);
    assert.deepEqual(second.manifest, first.manifest);
  });
});

test("an invalid ready manifest triggers a rebuild", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture);
    const first = await ensureRuntime(state.options);
    await fs.writeFile(
      path.join(first.root, ".ready.json"),
      `${JSON.stringify({ ...first.manifest, constraintDigest: "0".repeat(64) })}\n`,
    );

    const rebuilt = await ensureRuntime(state.options);

    assert.equal(state.installs, 2);
    assert.notEqual(rebuilt.manifest.constraintDigest, "0".repeat(64));
  });
});

test("two callers share one lock and one installation", async () => {
  await withFixture(async (fixture) => {
    let releaseInstall;
    const installationStarted = new Promise((resolve) => { releaseInstall = resolve; });
    let unblock;
    const blocked = new Promise((resolve) => { unblock = resolve; });
    const state = harness(fixture, {
      installDelay: async () => {
        releaseInstall();
        await blocked;
      },
    });

    const first = ensureRuntime(state.options);
    await installationStarted;
    const second = ensureRuntime(state.options);
    unblock();
    const [one, two] = await Promise.all([first, second]);

    assert.equal(state.installs, 1);
    assert.equal(one.root, two.root);
    await assert.rejects(fs.stat(`${one.root}.lock`), { code: "ENOENT" });
  });
});

test("an install failure leaves the earlier cache untouched", async () => {
  await withFixture(async (fixture) => {
    const good = harness(fixture);
    const existing = await ensureRuntime(good.options);
    const previousManifest = await fs.readFile(path.join(existing.root, ".ready.json"), "utf8");
    await fs.writeFile(path.join(existing.root, "keep.txt"), "earlier cache\n");
    await fs.writeFile(path.join(fixture.packageRoot, "mobile_auto_mcp", "server.py"), "print('changed')\n");

    const failing = harness(fixture, { installError: new Error("pip failed") });
    await assert.rejects(() => ensureRuntime(failing.options), /pip failed/);

    assert.equal(await fs.readFile(path.join(existing.root, ".ready.json"), "utf8"), previousManifest);
    assert.equal(await fs.readFile(path.join(existing.root, "keep.txt"), "utf8"), "earlier cache\n");
    assert.deepEqual(
      (await fs.readdir(path.dirname(existing.root))).sort(),
      [path.basename(existing.root)],
    );
  });
});

test("the ready manifest binds npm, Python, source, and constraint versions", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture);
    const runtime = await ensureRuntime(state.options);

    assert.equal(runtime.manifest.npmVersion, "0.3.0");
    assert.equal(runtime.manifest.pythonVersion, "3.12.7");
    assert.match(runtime.manifest.sourceDigest, /^[a-f0-9]{64}$/);
    assert.match(runtime.manifest.constraintDigest, /^[a-f0-9]{64}$/);
    assert.equal(runtime.manifest.pythonRelative, path.join("venv", "bin", "python"));
  });
});

test("bootstrap logs diagnostics without writing protocol stdout", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture);
    let stdout = "";
    const originalWrite = process.stdout.write;
    process.stdout.write = (chunk) => { stdout += chunk; return true; };
    try {
      await ensureRuntime(state.options);
    } finally {
      process.stdout.write = originalWrite;
    }

    assert.ok(state.logs.length > 0);
    assert.equal(stdout, "");
  });
});

test("pip receives constraints and the package source as literal argv elements", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture);
    await ensureRuntime(state.options);
    const install = state.calls.find((call) => call.args[0] === "-m" && call.args[1] === "pip");

    assert.deepEqual(install.args, [
      "-m",
      "pip",
      "install",
      "--disable-pip-version-check",
      "--constraint",
      fixture.constraintsPath,
      fixture.packageRoot,
    ]);
  });
});

test("an unusable system Python falls back to the managed Python runtime", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture, { venvError: new Error("venv unavailable") });
    const runtime = await ensureRuntime(state.options);

    assert.equal(state.downloads, 1);
    assert.equal(state.installs, 1);
    assert.match(runtime.python, new RegExp(`${path.sep}managed${path.sep}`));
    assert.equal(runtime.manifest.pythonVersion, "3.12.13");
    assert.ok(state.logs.some((message) => message.includes("managed Python")));
  });
});

test("a source mutation during install is never published as ready", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture, {
      afterInstall: () => fs.writeFile(
        path.join(fixture.packageRoot, "mobile_auto_mcp", "server.py"),
        "print('mutated during install')\n",
      ),
    });

    await assert.rejects(() => ensureRuntime(state.options), /source changed during bootstrap/i);
    await assert.rejects(
      fs.stat(path.join(fixture.cacheHome, "0.3.0", "darwin-arm64")),
      { code: "ENOENT" },
    );
  });
});
