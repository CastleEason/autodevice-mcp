"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const pkg = require("../package.json");
const managedManifest = require("../runtime/python-build-standalone.json");
const { extractArchive } = require("./archive");
const { downloadVerified } = require("./download");
const { normalizePlatform, runtimeRoot } = require("./platform");
const { findPython312 } = require("./python");
const { selectRuntimeAsset } = require("./runtime-manifest");

const execFileAsync = promisify(execFile);
const READY_FILE = ".ready.json";
const LOCK_OWNER_FILE = "owner.json";
const SOURCE_ENTRIES = ["pyproject.toml", "README.md", "LICENSE", "mobile_auto_mcp"];

function lockLeaseFile(token) {
  return `lease-${token}`;
}

function runtimeError(code, message, cause) {
  return Object.assign(new Error(message, cause ? { cause } : undefined), { code });
}

/** Runs a command represented by a literal executable and argv array. */
async function defaultRunFile(command, args) {
  const values = Array.isArray(command) ? command : [command];
  return execFileAsync(values[0], [...values.slice(1), ...args], {
    encoding: "utf8",
    windowsHide: true,
  });
}

/** Produces the Python executable location within an isolated runtime root. */
function isolatedPython(root, platform, managed = false) {
  if (managed) {
    return platform === "win32"
      ? path.join(root, "managed", "python", "python.exe")
      : path.join(root, "managed", "python", "bin", "python3");
  }
  return platform === "win32"
    ? path.join(root, "venv", "Scripts", "python.exe")
    : path.join(root, "venv", "bin", "python");
}

/** Parses only complete stable Python 3.12 version output. */
function python312Version(value) {
  const match = /^(?:Python\s+)?(3\.12\.\d+)$/.exec(String(value).trim());
  return match?.[1] ?? null;
}

/** Hashes bytes with an unambiguous path/length envelope. */
function updateDigest(hash, relative, content) {
  hash.update(`${relative.length}:${relative}:${content.length}:`);
  hash.update(content);
}

/** Adds a regular source tree to a deterministic package-source digest. */
async function hashSourceEntry(hash, packageRoot, relative, fileSystem) {
  const absolute = path.join(packageRoot, relative);
  let info;
  try {
    info = await fileSystem.lstat(absolute);
  } catch (error) {
    if (error?.code === "ENOENT") return;
    throw error;
  }
  if (info.isSymbolicLink()) {
    throw runtimeError("runtime_source_invalid", `Package source contains a symlink: ${relative}`);
  }
  if (info.isFile()) {
    updateDigest(hash, relative.split(path.sep).join("/"), await fileSystem.readFile(absolute));
    return;
  }
  if (!info.isDirectory()) {
    throw runtimeError("runtime_source_invalid", `Package source is not a regular file or directory: ${relative}`);
  }
  const entries = await fileSystem.readdir(absolute, { withFileTypes: true });
  entries.sort((left, right) => left.name.localeCompare(right.name));
  for (const entry of entries) {
    await hashSourceEntry(hash, packageRoot, path.join(relative, entry.name), fileSystem);
  }
}

/** Binds the installed Python package to the exact npm-shipped Python sources. */
async function sourceDigest(packageRoot, fileSystem) {
  const hash = crypto.createHash("sha256");
  for (const entry of SOURCE_ENTRIES) {
    await hashSourceEntry(hash, packageRoot, entry, fileSystem);
  }
  return hash.digest("hex");
}

/** Reads and validates a ready cache against all expected version bindings. */
async function inspectReady(root, expected, { fileSystem, runFile }) {
  let rootInfo;
  try {
    rootInfo = await fileSystem.lstat(root);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
  if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) return null;

  try {
    const manifest = JSON.parse(await fileSystem.readFile(path.join(root, READY_FILE), "utf8"));
    for (const [key, value] of Object.entries(expected)) {
      if (manifest[key] !== value) return null;
    }
    if (python312Version(manifest.pythonVersion) !== manifest.pythonVersion) return null;
    if (typeof manifest.pythonRelative !== "string" || path.isAbsolute(manifest.pythonRelative)) return null;
    const python = path.resolve(root, manifest.pythonRelative);
    if (python !== root && !python.startsWith(`${root}${path.sep}`)) return null;
    const result = await runFile(python, ["--version"]);
    if (python312Version(result.stdout || result.stderr) !== manifest.pythonVersion) return null;
    return { python, root, manifest };
  } catch {
    return null;
  }
}

/** Lists sibling generations retained by interrupted or incomplete publication. */
async function recoverableGenerations(root, fileSystem) {
  const parent = path.dirname(root);
  const prefixes = [
    `${path.basename(root)}.previous-`,
    `${path.basename(root)}.tmp-`,
  ];
  let entries;
  try {
    entries = await fileSystem.readdir(parent, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
  return entries
    .filter((entry) => prefixes.some((prefix) => entry.name.startsWith(prefix)))
    .map((entry) => path.join(parent, entry.name))
    .sort();
}

/** Removes obsolete generations only after a canonical generation is validated. */
async function cleanupGenerations(paths, fileSystem, logger) {
  for (const generation of paths) {
    await fileSystem.rm(generation, { recursive: true, force: true }).catch((error) => {
      logger(`Could not remove superseded runtime generation ${generation}: ${error.message}`);
    });
  }
}

/** Restores a deterministic validated previous generation when canonical state is unusable. */
async function recoverRuntime(root, expected, deps, logger, assertOwned) {
  const canonical = await inspectReady(root, expected, deps);
  const previous = await recoverableGenerations(root, deps.fileSystem);
  if (canonical) {
    await assertOwned();
    await cleanupGenerations(previous, deps.fileSystem, logger);
    return canonical;
  }

  let selected = null;
  for (const candidate of previous) {
    if (await inspectReady(candidate, expected, deps)) {
      selected = candidate;
      break;
    }
  }
  if (!selected) return null;

  const displaced = `${root}.corrupt-${deps.randomUUID()}`;
  let movedCanonical = false;
  try {
    await assertOwned();
    await deps.fileSystem.rename(root, displaced);
    movedCanonical = true;
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  try {
    await assertOwned();
    await deps.fileSystem.rename(selected, root);
  } catch (recoveryError) {
    if (movedCanonical) {
      try {
        await assertOwned();
        await deps.fileSystem.rename(displaced, root);
      } catch (restoreError) {
        throw runtimeError(
          "runtime_recovery_restore_failed",
          `Runtime recovery failed (${recoveryError.message}) and canonical restoration failed (${restoreError.message})`,
          new AggregateError([recoveryError, restoreError]),
        );
      }
    }
    throw recoveryError;
  }

  const recovered = await inspectReady(root, expected, deps);
  if (!recovered) {
    throw runtimeError("runtime_recovery_failed", "Recovered runtime generation did not remain valid");
  }
  logger(`Recovered runtime generation from ${selected}`);
  await assertOwned();
  await cleanupGenerations(
    [...previous.filter((candidate) => candidate !== selected), ...(movedCanonical ? [displaced] : [])],
    deps.fileSystem,
    logger,
  );
  return recovered;
}

/** Checks whether an owner PID still denotes a live local process. */
function defaultIsProcessAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

/** Reads lock ownership and freshness without trusting partial metadata. */
async function readLockOwner(lockPath, fileSystem) {
  const ownerPath = path.join(lockPath, LOCK_OWNER_FILE);
  try {
    const [lockInfo, ownerInfo, value] = await Promise.all([
      fileSystem.stat(lockPath),
      fileSystem.stat(ownerPath),
      fileSystem.readFile(ownerPath, "utf8"),
    ]);
    const owner = JSON.parse(value);
    if (
      !Number.isSafeInteger(owner.pid)
      || owner.pid <= 0
      || !Number.isFinite(owner.createdAt)
      || typeof owner.token !== "string"
      || !/^[A-Za-z0-9_-]{1,128}$/.test(owner.token)
    ) {
      return { owner: null, updatedAt: lockInfo.mtimeMs };
    }
    let leaseInfo;
    try {
      leaseInfo = await fileSystem.stat(path.join(lockPath, lockLeaseFile(owner.token)));
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    return { owner, updatedAt: Math.max(owner.createdAt, (leaseInfo ?? ownerInfo).mtimeMs) };
  } catch (error) {
    if (error?.code === "ENOENT" || error instanceof SyntaxError) {
      try {
        return { owner: null, updatedAt: (await fileSystem.stat(lockPath)).mtimeMs };
      } catch (statError) {
        if (statError?.code === "ENOENT") return null;
        throw statError;
      }
    }
    throw error;
  }
}

/** Atomically moves a stale lock aside after confirming its ownership snapshot. */
async function reclaimStaleLock(lockPath, deps, lockStaleMs, logger) {
  const snapshot = await readLockOwner(lockPath, deps.fileSystem);
  if (!snapshot) return true;
  const expired = deps.now() - snapshot.updatedAt >= lockStaleMs;
  const dead = snapshot.owner ? !(await deps.isProcessAlive(snapshot.owner.pid)) : false;
  if (!expired && !dead) return false;

  const confirmation = await readLockOwner(lockPath, deps.fileSystem);
  if (!confirmation) return true;
  if (
    confirmation.owner?.token !== snapshot.owner?.token
    || confirmation.updatedAt !== snapshot.updatedAt
  ) {
    return false;
  }

  const quarantine = `${lockPath}.stale-${deps.randomUUID()}`;
  try {
    await deps.fileSystem.rename(lockPath, quarantine);
  } catch (error) {
    if (error?.code === "ENOENT") return true;
    throw error;
  }
  logger(`Reclaimed stale runtime lock owned by PID ${snapshot.owner?.pid ?? "unknown"}`);
  await deps.fileSystem.rm(quarantine, { recursive: true, force: true }).catch((error) => {
    logger(`Could not remove quarantined stale lock ${quarantine}: ${error.message}`);
  });
  return true;
}

/** Waits for exclusive ownership and persists PID/time/token lock metadata. */
async function acquireLock(lockPath, deps, { lockTimeoutMs, lockStaleMs, logger }) {
  const started = deps.now();
  while (true) {
    try {
      await deps.fileSystem.mkdir(lockPath, { mode: 0o700 });
      const owner = {
        pid: deps.processId,
        createdAt: deps.now(),
        token: deps.randomUUID(),
      };
      try {
        await deps.fileSystem.writeFile(
          path.join(lockPath, LOCK_OWNER_FILE),
          `${JSON.stringify(owner)}\n`,
          { mode: 0o600 },
        );
        await deps.fileSystem.writeFile(
          path.join(lockPath, lockLeaseFile(owner.token)),
          "",
          { mode: 0o600 },
        );
      } catch (initializationError) {
        const abandoned = `${lockPath}.abandoned-${owner.token}`;
        try {
          await deps.fileSystem.rename(lockPath, abandoned);
        } catch (cleanupError) {
          throw runtimeError(
            "runtime_lock_initialization_cleanup_failed",
            `Runtime lock initialization failed (${initializationError.message}) and cleanup failed (${cleanupError.message})`,
            new AggregateError([initializationError, cleanupError]),
          );
        }
        await deps.fileSystem.rm(abandoned, { recursive: true, force: true }).catch((cleanupError) => {
          logger(`Could not remove abandoned runtime lock ${abandoned}: ${cleanupError.message}`);
        });
        throw initializationError;
      }
      return owner;
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      if (await reclaimStaleLock(lockPath, deps, lockStaleMs, logger)) continue;
      if (deps.now() - started >= lockTimeoutMs) {
        throw runtimeError("runtime_lock_timeout", `Timed out waiting for runtime lock: ${lockPath}`);
      }
      await deps.sleep(50);
    }
  }
}

/** Records lock loss once so heartbeat and publication share the same fencing result. */
function markLockLost(lockPath, lease) {
  if (!lease.lostError) {
    lease.lostError = runtimeError(
      "runtime_lock_lost",
      `Runtime lock ownership changed: ${lockPath}`,
    );
  }
  return lease.lostError;
}

/** Fences every cache mutation against the token currently stored in the lock. */
async function assertLockOwnership(lockPath, lease, deps) {
  if (lease.lostError) throw lease.lostError;
  const current = await readLockOwner(lockPath, deps.fileSystem);
  if (!current || current.owner?.token !== lease.owner.token) {
    throw markLockLost(lockPath, lease);
  }
}

/** Keeps a live lock fresh without ever touching a replacement owner's lease file. */
function startLockHeartbeat(lockPath, lease, deps, lockStaleMs, logger) {
  const interval = Math.max(1000, Math.min(30_000, Math.floor(lockStaleMs / 3)));
  const leasePath = path.join(lockPath, lockLeaseFile(lease.owner.token));
  let refreshing = false;
  const timer = deps.setInterval(async () => {
    if (refreshing || lease.lostError) return;
    refreshing = true;
    try {
      await assertLockOwnership(lockPath, lease, deps);
      const now = new Date(deps.now());
      await deps.fileSystem.utimes(leasePath, now, now);
    } catch (error) {
      if (error?.code === "ENOENT" || error?.code === "runtime_lock_lost") {
        const lost = markLockLost(lockPath, lease);
        logger(`Runtime lock heartbeat stopped: ${lost.message}`);
      } else {
        logger(`Runtime lock heartbeat failed: ${error.message}`);
      }
    } finally {
      refreshing = false;
    }
  }, interval);
  timer.unref?.();
  return () => deps.clearInterval(timer);
}

/** Releases only the caller's token-owned lock and frees the canonical lock path first. */
async function releaseLock(lockPath, lease, deps, logger) {
  await assertLockOwnership(lockPath, lease, deps);
  const released = `${lockPath}.released-${lease.owner.token}`;
  await deps.fileSystem.rename(lockPath, released);
  await deps.fileSystem.rm(released, { recursive: true, force: true }).catch((error) => {
    logger(`Could not remove released runtime lock ${released}: ${error.message}`);
  });
}

/** Creates an isolated interpreter and installs the npm package using exact constraints. */
async function buildRuntime(context, temporaryRoot) {
  const {
    asset,
    constraintsPath,
    deps,
    expected,
    logger,
    packageRoot,
    platform,
    systemPython,
  } = context;
  const { fileSystem, runFile } = deps;
  let python;
  let expectedPythonVersion;

  const useManagedPython = async () => {
    logger(`Downloading managed Python ${managedManifest.pythonVersion}`);
    const archivePath = path.join(temporaryRoot, `python.${asset.archiveType.replace(".", "-")}`);
    await deps.downloadVerified(asset, archivePath);
    await deps.extractArchive(asset, archivePath, path.join(temporaryRoot, "managed"));
    await fileSystem.rm(archivePath, { force: true });
    python = isolatedPython(temporaryRoot, platform, true);
    expectedPythonVersion = managedManifest.pythonVersion;
  };

  if (systemPython) {
    logger(`Creating Python ${systemPython.version} environment`);
    try {
      await runFile(systemPython.executable, ["-m", "venv", path.join(temporaryRoot, "venv")]);
      python = isolatedPython(temporaryRoot, platform, false);
      expectedPythonVersion = systemPython.version;
    } catch (error) {
      logger(`System Python could not create an isolated environment; using managed Python: ${error.message}`);
      await fileSystem.rm(path.join(temporaryRoot, "venv"), { recursive: true, force: true });
      await useManagedPython();
    }
  } else {
    await useManagedPython();
  }

  logger("Installing autodevice-mcp Python dependencies");
  await runFile(python, [
    "-m",
    "pip",
    "install",
    "--disable-pip-version-check",
    "--constraint",
    constraintsPath,
    "pip",
    "setuptools",
    "wheel",
    "mcp",
    "mitmproxy",
    "Pillow",
    "uiautomator2",
  ]);
  await runFile(python, [
    "-m",
    "pip",
    "install",
    "--disable-pip-version-check",
    "--no-build-isolation",
    "--no-deps",
    packageRoot,
  ]);
  if (await sourceDigest(packageRoot, fileSystem) !== expected.sourceDigest) {
    throw runtimeError("runtime_source_changed", "Package source changed during bootstrap");
  }
  const installedConstraintDigest = crypto.createHash("sha256")
    .update(await fileSystem.readFile(constraintsPath))
    .digest("hex");
  if (installedConstraintDigest !== expected.constraintDigest) {
    throw runtimeError("runtime_constraints_changed", "Python constraints changed during bootstrap");
  }
  const verified = await runFile(python, ["--version"]);
  const pythonVersion = python312Version(verified.stdout || verified.stderr);
  if (pythonVersion !== expectedPythonVersion) {
    throw runtimeError("python_not_found", `Isolated runtime did not provide Python ${expectedPythonVersion}`);
  }

  const manifest = {
    ...expected,
    pythonVersion,
    pythonRelative: path.relative(temporaryRoot, python),
  };
  await fileSystem.writeFile(
    path.join(temporaryRoot, READY_FILE),
    `${JSON.stringify(manifest, null, 2)}\n`,
    { mode: 0o600 },
  );
  return manifest;
}

/** Replaces a stale cache only after its complete successor is ready. */
async function publishRuntime(root, temporaryRoot, backupRoot, fileSystem, assertOwned) {
  let movedExisting = false;
  try {
    await assertOwned();
    await fileSystem.rename(root, backupRoot);
    movedExisting = true;
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  try {
    await assertOwned();
    await fileSystem.rename(temporaryRoot, root);
  } catch (publishError) {
    if (movedExisting) {
      try {
        await assertOwned();
        await fileSystem.rename(backupRoot, root);
      } catch (restoreError) {
        let preservationError = null;
        try {
          await assertOwned();
          await fileSystem.rename(temporaryRoot, `${backupRoot}-staged`);
        } catch (error) {
          preservationError = error;
        }
        const error = runtimeError(
          "runtime_publish_restore_failed",
          `Runtime publication failed (${publishError.message}) and previous generation restoration failed (${restoreError.message})`,
          new AggregateError([publishError, restoreError, ...(preservationError ? [preservationError] : [])]),
        );
        error.preserveTemporary = Boolean(preservationError);
        throw error;
      }
    }
    throw publishError;
  }
  return movedExisting;
}

/** Restores the previous generation when canonical-path validation fails after rename. */
async function rollbackInvalidPublication(
  root,
  backupRoot,
  failedRoot,
  movedExisting,
  fileSystem,
  assertOwned,
) {
  if (!movedExisting) return;
  await assertOwned();
  await fileSystem.rename(root, failedRoot);
  try {
    await assertOwned();
    await fileSystem.rename(backupRoot, root);
  } catch (restoreError) {
    let failedGenerationRestore = null;
    try {
      await assertOwned();
      await fileSystem.rename(failedRoot, root);
    } catch (error) {
      failedGenerationRestore = error;
    }
    throw runtimeError(
      "runtime_publish_validation_restore_failed",
      `Published runtime validation failed and previous generation restoration failed (${restoreError.message})`,
      new AggregateError([restoreError, ...(failedGenerationRestore ? [failedGenerationRestore] : [])]),
    );
  }
  await assertOwned();
  await fileSystem.rm(failedRoot, { recursive: true, force: true });
}

/** Resolves host, digest, cache, and injected dependency state for one operation. */
async function resolveContext(options) {
  const packageRoot = path.resolve(options.packageRoot ?? path.join(__dirname, ".."));
  const constraintsPath = path.resolve(options.constraintsPath ?? path.join(packageRoot, "runtime", "constraints.txt"));
  const platform = options.platform ?? process.platform;
  const arch = options.arch ?? process.arch;
  const version = options.version ?? pkg.version;
  const env = options.env ?? process.env;
  const homedir = options.homedir ?? os.homedir();
  const logger = options.logger ?? ((message) => process.stderr.write(`${message}\n`));
  const provided = options.deps ?? {};
  const deps = {
    fileSystem: provided.fs ?? fs,
    runFile: provided.runFile ?? defaultRunFile,
    findPython312: provided.findPython312 ?? findPython312,
    downloadVerified: provided.downloadVerified ?? downloadVerified,
    extractArchive: provided.extractArchive ?? extractArchive,
    randomUUID: provided.randomUUID ?? crypto.randomUUID,
    sleep: provided.sleep ?? ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))),
    now: provided.now ?? Date.now,
    processId: provided.processId ?? process.pid,
    isProcessAlive: provided.isProcessAlive ?? defaultIsProcessAlive,
    setInterval: provided.setInterval ?? setInterval,
    clearInterval: provided.clearInterval ?? clearInterval,
  };
  const host = normalizePlatform(platform, arch);
  const root = runtimeRoot({ env, homedir, version, platformKey: host.key });
  const constraintDigest = crypto.createHash("sha256")
    .update(await deps.fileSystem.readFile(constraintsPath))
    .digest("hex");
  const packageDigest = await sourceDigest(packageRoot, deps.fileSystem);
  const systemPython = await deps.findPython312({ env });
  const asset = selectRuntimeAsset(managedManifest, host.key);
  const expected = {
    npmVersion: version,
    sourceDigest: packageDigest,
    constraintDigest,
  };
  return {
    asset,
    constraintsPath,
    deps,
    expected,
    host,
    logger,
    packageRoot,
    platform,
    root,
    systemPython,
  };
}

/** Ensures one versioned, platform-specific Python MCP runtime is ready. */
async function ensureRuntime(options = {}) {
  const logger = options.logger ?? ((message) => process.stderr.write(`${message}\n`));
  try {
    const context = await resolveContext({ ...options, logger });
    const { deps, expected, root } = context;
    const ready = await inspectReady(root, expected, deps);
    if (ready && (await recoverableGenerations(root, deps.fileSystem)).length === 0) {
      logger(`Using cached runtime at ${root}`);
      return ready;
    }

    await deps.fileSystem.mkdir(path.dirname(root), { recursive: true, mode: 0o700 });
    const lockPath = `${root}.lock`;
    const lockStaleMs = options.lockStaleMs ?? 15 * 60_000;
    const owner = await acquireLock(lockPath, deps, {
      lockTimeoutMs: options.lockTimeoutMs ?? 120_000,
      lockStaleMs,
      logger,
    });
    const lease = { owner, lostError: null };
    const assertOwned = () => assertLockOwnership(lockPath, lease, deps);
    const stopHeartbeat = startLockHeartbeat(lockPath, lease, deps, lockStaleMs, logger);
    try {
      const afterLock = await recoverRuntime(root, expected, deps, logger, assertOwned);
      if (afterLock) return afterLock;

      const id = deps.randomUUID();
      const temporaryRoot = `${root}.tmp-${id}`;
      const backupRoot = `${root}.previous-${id}`;
      let temporaryCreated = false;
      try {
        logger(`Bootstrapping runtime at ${root}`);
        await deps.fileSystem.mkdir(temporaryRoot, { mode: 0o700 });
        temporaryCreated = true;
        await buildRuntime(context, temporaryRoot);
        let movedExisting;
        try {
          movedExisting = await publishRuntime(
            root,
            temporaryRoot,
            backupRoot,
            deps.fileSystem,
            assertOwned,
          );
        } catch (error) {
          if (error?.preserveTemporary) temporaryCreated = false;
          throw error;
        }
        temporaryCreated = false;
        const published = await inspectReady(root, expected, deps);
        if (!published) {
          await rollbackInvalidPublication(
            root,
            backupRoot,
            `${root}.failed-${id}`,
            movedExisting,
            deps.fileSystem,
            assertOwned,
          );
          throw runtimeError(
            "runtime_publish_validation_failed",
            "Published runtime did not validate at its canonical path",
          );
        }
        if (movedExisting) {
          await assertOwned();
          await cleanupGenerations([backupRoot], deps.fileSystem, logger);
        }
        return published;
      } finally {
        if (temporaryCreated) {
          await deps.fileSystem.rm(temporaryRoot, { recursive: true, force: true }).catch(() => {});
        }
      }
    } finally {
      stopHeartbeat();
      await releaseLock(lockPath, lease, deps, logger);
    }
  } catch (error) {
    if (!options.bestEffort) throw error;
    logger(`Runtime bootstrap skipped: ${error.message || String(error)}`);
    return null;
  }
}

/** Reports interpreter and cache readiness without mutating the runtime cache. */
async function doctor(options = {}) {
  const context = await resolveContext(options);
  const runtime = await inspectReady(context.root, context.expected, context.deps);
  return {
    npmVersion: context.expected.npmVersion,
    platform: context.host.key,
    root: context.root,
    ready: Boolean(runtime),
    python: runtime?.python ?? null,
    pythonVersion: runtime?.manifest.pythonVersion
      ?? context.systemPython?.version
      ?? managedManifest.pythonVersion,
    sourceDigest: context.expected.sourceDigest,
    constraintDigest: context.expected.constraintDigest,
    interpreter: context.systemPython ? "system" : "managed",
  };
}

module.exports = { doctor, ensureRuntime };
