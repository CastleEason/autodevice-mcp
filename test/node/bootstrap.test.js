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
      const executable = overrides.platform === "win32"
        ? path.join(environment, "Scripts", "python.exe")
        : path.join(environment, "bin", "python");
      await fs.mkdir(path.dirname(executable), { recursive: true });
      await fs.writeFile(executable, "fixture python");
      return { stdout: "", stderr: "" };
    }
    if (args.length === 1 && args[0] === "--version") {
      if (
        overrides.invalidPublishedRoot
        && String(command).startsWith(path.join(overrides.invalidPublishedRoot, "venv"))
      ) {
        return { stdout: "Python 3.11.9\n", stderr: "" };
      }
      const version = String(command).includes(`${path.sep}managed${path.sep}`) ? "3.12.13" : "3.12.7";
      return { stdout: `Python ${version}\n`, stderr: "" };
    }
    if (args[0] === "-m" && args[1] === "pip") {
      if (args.includes(fixture.packageRoot)) {
        installs += 1;
        if (overrides.installDelay) await overrides.installDelay();
        if (overrides.installError) throw overrides.installError;
        if (overrides.afterInstall) await overrides.afterInstall();
      }
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
      platform: overrides.platform ?? "darwin",
      arch: overrides.arch ?? "arm64",
      logger: (message) => logs.push(message),
      deps: {
        fs: overrides.fs ?? fs,
        findPython312: async () => ({ executable: "/system/python3.12", version: "3.12.7" }),
        runFile,
        downloadVerified: async (_asset, destination) => {
          downloads += 1;
          await fs.writeFile(destination, "managed archive");
        },
        extractArchive: async (_asset, _archive, destination) => {
          const executable = overrides.platform === "win32"
            ? path.join(destination, "python", "python.exe")
            : path.join(destination, "python", "bin", "python3");
          await fs.mkdir(path.dirname(executable), { recursive: true });
          await fs.writeFile(executable, "managed fixture python");
        },
        randomUUID: overrides.randomUUID ?? (() => "fixture-id"),
        sleep: async () => new Promise((resolve) => setTimeout(resolve, 5)),
        now: overrides.now,
        processId: overrides.processId,
        isProcessAlive: overrides.isProcessAlive,
        setInterval: overrides.setInterval,
        clearInterval: overrides.clearInterval,
      },
    },
    calls,
    logs,
    get downloads() { return downloads; },
    get installs() { return installs; },
  };
}

function invalidCanonicalReads(root, rename) {
  let readyReads = 0;
  return {
    ...fs,
    readFile: async (value, ...args) => {
      if (value === path.join(root, ".ready.json") && readyReads < 2) {
        readyReads += 1;
        return "not json";
      }
      return fs.readFile(value, ...args);
    },
    rename: rename ?? fs.rename.bind(fs),
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
    const root = path.join(fixture.cacheHome, "0.3.0", "darwin-arm64");
    const owner = JSON.parse(await fs.readFile(path.join(`${root}.lock`, "owner.json"), "utf8"));
    assert.equal(owner.pid, process.pid);
    assert.equal(typeof owner.createdAt, "number");
    assert.match(owner.token, /^[a-zA-Z0-9-]+$/);
    const second = ensureRuntime(state.options);
    unblock();
    const [one, two] = await Promise.all([first, second]);

    assert.equal(state.installs, 1);
    assert.equal(one.root, two.root);
    await assert.rejects(fs.stat(`${one.root}.lock`), { code: "ENOENT" });
  });
});

test("a dead or expired lock owner is reclaimed without waiting for the full timeout", async () => {
  await withFixture(async (fixture) => {
    const root = path.join(fixture.cacheHome, "0.3.0", "darwin-arm64");
    const lock = `${root}.lock`;
    await fs.mkdir(lock, { recursive: true });
    await fs.writeFile(path.join(lock, "owner.json"), JSON.stringify({
      pid: 999999,
      createdAt: 100,
      token: "stale-owner",
    }));
    const state = harness(fixture, {
      now: () => 10_000,
      processId: 321,
      isProcessAlive: () => false,
    });
    state.options.lockStaleMs = 1_000;
    state.options.lockTimeoutMs = 30;

    const runtime = await ensureRuntime(state.options);

    assert.equal(runtime.root, root);
    assert.equal(state.installs, 1);
    await assert.rejects(fs.stat(lock), { code: "ENOENT" });
  });
});

test("an expired lock is reclaimed even when its PID has been reused by a live process", async () => {
  await withFixture(async (fixture) => {
    const root = path.join(fixture.cacheHome, "0.3.0", "darwin-arm64");
    const lock = `${root}.lock`;
    const ownerPath = path.join(lock, "owner.json");
    await fs.mkdir(lock, { recursive: true });
    await fs.writeFile(ownerPath, JSON.stringify({ pid: 55, createdAt: 100, token: "expired-owner" }));
    await fs.utimes(ownerPath, new Date(100), new Date(100));
    const state = harness(fixture, {
      now: () => 10_000,
      isProcessAlive: () => true,
    });
    state.options.lockStaleMs = 1_000;
    state.options.lockTimeoutMs = 30;

    const runtime = await ensureRuntime(state.options);

    assert.equal(runtime.root, root);
    assert.equal(state.installs, 1);
  });
});

test("lock release frees the canonical lock even when released-state cleanup fails", async () => {
  await withFixture(async (fixture) => {
    const logs = [];
    const wrappedFs = {
      ...fs,
      rm: async (value, options) => {
        if (String(value).includes(".lock.released-")) {
          throw Object.assign(new Error("cleanup denied"), { code: "EACCES" });
        }
        return fs.rm(value, options);
      },
    };
    const state = harness(fixture, { fs: wrappedFs });
    state.options.logger = (message) => logs.push(message);
    const runtime = await ensureRuntime(state.options);

    await assert.rejects(fs.stat(`${runtime.root}.lock`), { code: "ENOENT" });
    assert.ok(logs.some((message) => message.includes("cleanup denied")));
  });
});

test("lock initialization reports cleanup failure instead of silently stranding a lock", async () => {
  await withFixture(async (fixture) => {
    const wrappedFs = {
      ...fs,
      writeFile: async (value, ...args) => {
        if (path.basename(value) === "owner.json") {
          throw Object.assign(new Error("owner write failed"), { code: "EIO" });
        }
        return fs.writeFile(value, ...args);
      },
      rename: async (source, destination) => {
        if (source.endsWith(".lock") && destination.includes(".abandoned-")) {
          throw Object.assign(new Error("abandon rename failed"), { code: "EACCES" });
        }
        return fs.rename(source, destination);
      },
    };
    const state = harness(fixture, { fs: wrappedFs });

    await assert.rejects(
      () => ensureRuntime(state.options),
      (error) => error.code === "runtime_lock_initialization_cleanup_failed"
        && error.message.includes("owner write failed")
        && error.message.includes("abandon rename failed"),
    );
  });
});

test("a reclaimed owner cannot heartbeat or publish through its replacement lease", async () => {
  await withFixture(async (fixture) => {
    const scheduledHeartbeats = [];
    const scheduleHeartbeat = (callback) => {
      scheduledHeartbeats.push(callback);
      return { unref() {} };
    };
    const clearHeartbeat = () => {};

    let resumeA;
    let resumeB;
    let markAStarted;
    let markBStarted;
    const aStarted = new Promise((resolve) => { markAStarted = resolve; });
    const bStarted = new Promise((resolve) => { markBStarted = resolve; });
    const aBlocked = new Promise((resolve) => { resumeA = resolve; });
    const bBlocked = new Promise((resolve) => { resumeB = resolve; });
    const clock = { now: Date.now() };
    const sequence = (values) => () => {
      assert.ok(values.length > 0, "random UUID sequence exhausted");
      return values.shift();
    };
    const ownerA = harness(fixture, {
      processId: 101,
      now: () => clock.now,
      randomUUID: sequence(["owner-a", "generation-a"]),
      setInterval: scheduleHeartbeat,
      clearInterval: clearHeartbeat,
      installDelay: async () => {
        markAStarted();
        await aBlocked;
      },
    });
    const ownerB = harness(fixture, {
      processId: 202,
      now: () => clock.now,
      randomUUID: sequence(["quarantine-a", "owner-b", "generation-b"]),
      setInterval: scheduleHeartbeat,
      clearInterval: clearHeartbeat,
      installDelay: async () => {
        markBStarted();
        await bBlocked;
      },
    });
    ownerA.options.lockStaleMs = 1_000;
    ownerB.options.lockStaleMs = 1_000;
    ownerA.options.lockTimeoutMs = 100;
    ownerB.options.lockTimeoutMs = 100;

    const root = path.join(fixture.cacheHome, "0.3.0", "darwin-arm64");
    const lock = `${root}.lock`;
    const ownerPath = path.join(lock, "owner.json");
    let aPromise;
    let bPromise;
    try {
      aPromise = ensureRuntime(ownerA.options);
      await aStarted;
      clock.now += 5_000;

      bPromise = ensureRuntime(ownerB.options);
      await bStarted;
      assert.equal(scheduledHeartbeats.length, 2);
      const beforeHeartbeat = {
        contents: await fs.readFile(ownerPath, "utf8"),
        mtimeMs: (await fs.stat(ownerPath)).mtimeMs,
      };
      assert.equal(JSON.parse(beforeHeartbeat.contents).token, "owner-b");

      clock.now += 500;
      await scheduledHeartbeats[0]();
      await new Promise((resolve) => setImmediate(resolve));
      const afterHeartbeat = {
        contents: await fs.readFile(ownerPath, "utf8"),
        mtimeMs: (await fs.stat(ownerPath)).mtimeMs,
      };
      assert.deepEqual(afterHeartbeat, beforeHeartbeat);

      resumeA();
      await assert.rejects(aPromise, { code: "runtime_lock_lost" });
      await assert.rejects(fs.stat(root), { code: "ENOENT" });
      assert.deepEqual(await fs.readFile(ownerPath, "utf8"), beforeHeartbeat.contents);
      assert.ok((await fs.stat(`${root}.tmp-generation-b`)).isDirectory());
      await assert.rejects(fs.stat(`${root}.tmp-generation-a`), { code: "ENOENT" });

      resumeB();
      const runtime = await bPromise;
      assert.equal(runtime.root, root);
      assert.equal(ownerA.installs, 1);
      assert.equal(ownerB.installs, 1);
    } finally {
      resumeA?.();
      resumeB?.();
      await Promise.allSettled([aPromise, bPromise].filter(Boolean));
    }
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

test("startup recovers a validated generation after interruption following the old-root rename", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture);
    const runtime = await ensureRuntime(state.options);
    const previous = `${runtime.root}.previous-interrupted`;
    await fs.rename(runtime.root, previous);

    const recovered = await ensureRuntime(state.options);

    assert.equal(state.installs, 1);
    assert.equal(recovered.root, runtime.root);
    assert.equal(await fs.readFile(path.join(recovered.root, ".ready.json"), "utf8"),
      `${JSON.stringify(runtime.manifest, null, 2)}\n`);
    await assert.rejects(fs.stat(previous), { code: "ENOENT" });
  });
});

test("startup replaces a corrupt canonical root with a validated previous generation", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture);
    const runtime = await ensureRuntime(state.options);
    const previous = `${runtime.root}.previous-valid`;
    await fs.rename(runtime.root, previous);
    await fs.mkdir(runtime.root);
    await fs.writeFile(path.join(runtime.root, ".ready.json"), "corrupt\n");

    const recovered = await ensureRuntime(state.options);

    assert.equal(state.installs, 1);
    assert.equal(recovered.manifest.sourceDigest, runtime.manifest.sourceDigest);
    await assert.rejects(fs.stat(previous), { code: "ENOENT" });
  });
});

test("a failed staged-to-canonical rename restores the previous generation", async () => {
  await withFixture(async (fixture) => {
    const initial = harness(fixture);
    const runtime = await ensureRuntime(initial.options);
    await fs.writeFile(path.join(runtime.root, "keep.txt"), "previous generation\n");
    const publishError = Object.assign(new Error("publish rename failed"), { code: "EIO" });
    const wrappedFs = invalidCanonicalReads(runtime.root, async (source, destination) => {
      if (source.includes(".tmp-") && destination === runtime.root) throw publishError;
      return fs.rename(source, destination);
    });
    const failing = harness(fixture, { fs: wrappedFs });

    await assert.rejects(() => ensureRuntime(failing.options), publishError);

    assert.equal(await fs.readFile(path.join(runtime.root, "keep.txt"), "utf8"), "previous generation\n");
    assert.deepEqual(await fs.readdir(path.dirname(runtime.root)), [path.basename(runtime.root)]);
  });
});

test("canonical validation failure rolls back before previous-generation cleanup", async () => {
  await withFixture(async (fixture) => {
    const initial = harness(fixture);
    const runtime = await ensureRuntime(initial.options);
    await fs.writeFile(path.join(runtime.root, "keep.txt"), "validated old generation\n");
    await fs.writeFile(
      path.join(fixture.packageRoot, "mobile_auto_mcp", "server.py"),
      "print('new generation')\n",
    );
    const failing = harness(fixture, { invalidPublishedRoot: runtime.root });

    await assert.rejects(
      () => ensureRuntime(failing.options),
      { code: "runtime_publish_validation_failed" },
    );

    assert.equal(
      await fs.readFile(path.join(runtime.root, "keep.txt"), "utf8"),
      "validated old generation\n",
    );
  });
});

test("a failed restore is reported and remains recoverable on startup", async () => {
  await withFixture(async (fixture) => {
    const initial = harness(fixture);
    const runtime = await ensureRuntime(initial.options);
    await fs.writeFile(
      path.join(fixture.packageRoot, "mobile_auto_mcp", "server.py"),
      "print('new generation')\n",
    );
    const wrappedFs = {
      ...fs,
      rename: async (source, destination) => {
        if (source.includes(".tmp-") && destination === runtime.root) {
          throw Object.assign(new Error("publish rename failed"), { code: "EIO" });
        }
        if (source.includes(".previous-") && destination === runtime.root) {
          throw Object.assign(new Error("restore rename failed"), { code: "EACCES" });
        }
        return fs.rename(source, destination);
      },
    };
    const failing = harness(fixture, { fs: wrappedFs });

    await assert.rejects(
      () => ensureRuntime(failing.options),
      (error) => error.code === "runtime_publish_restore_failed"
        && error.message.includes("publish rename failed")
        && error.message.includes("restore rename failed"),
    );
    await assert.rejects(fs.stat(runtime.root), { code: "ENOENT" });
    assert.ok((await fs.readdir(path.dirname(runtime.root))).some((name) => name.includes(".previous-")));

    const recovered = await ensureRuntime(initial.options);
    assert.equal(recovered.root, runtime.root);
    assert.equal(initial.installs, 1);
    assert.equal(failing.installs, 1);
    assert.notEqual(recovered.manifest.sourceDigest, runtime.manifest.sourceDigest);
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

test("Windows runtimes use the venv Scripts python.exe layout", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture, { platform: "win32", arch: "x64" });
    const runtime = await ensureRuntime(state.options);

    assert.equal(runtime.manifest.pythonRelative, path.join("venv", "Scripts", "python.exe"));
    assert.equal(runtime.python, path.join(runtime.root, "venv", "Scripts", "python.exe"));
    assert.ok((await fs.stat(runtime.python)).isFile());
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

test("pip pins build tools and dependencies before a no-isolation no-deps source install", async () => {
  await withFixture(async (fixture) => {
    const state = harness(fixture);
    await ensureRuntime(state.options);
    const installs = state.calls.filter((call) => call.args[0] === "-m" && call.args[1] === "pip");

    assert.deepEqual(installs.map((call) => call.args), [[
      "-m",
      "pip",
      "install",
      "--disable-pip-version-check",
      "--constraint",
      fixture.constraintsPath,
      "pip",
      "setuptools",
      "wheel",
      "mcp",
      "mitmproxy",
      "Pillow",
      "uiautomator2",
    ], [
      "-m",
      "pip",
      "install",
      "--disable-pip-version-check",
      "--no-build-isolation",
      "--no-deps",
      fixture.packageRoot,
    ]]);
  });
});

test("the exact constraints include the PEP 517 build toolchain", async () => {
  const constraints = await fs.readFile(path.join(__dirname, "..", "..", "runtime", "constraints.txt"), "utf8");
  assert.match(constraints, /^pip==26\.1\.2$/m);
  assert.match(constraints, /^setuptools==83\.0\.0$/m);
  assert.match(constraints, /^wheel==0\.47\.0$/m);
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
