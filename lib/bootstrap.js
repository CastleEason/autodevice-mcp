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
const SOURCE_ENTRIES = ["pyproject.toml", "README.md", "LICENSE", "mobile_auto_mcp"];

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

/** Waits for exclusive ownership of the sibling bootstrap lock directory. */
async function acquireLock(lockPath, { fileSystem, sleep, lockTimeoutMs }) {
  const started = Date.now();
  while (true) {
    try {
      await fileSystem.mkdir(lockPath, { mode: 0o700 });
      return;
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      if (Date.now() - started >= lockTimeoutMs) {
        throw runtimeError("runtime_lock_timeout", `Timed out waiting for runtime lock: ${lockPath}`);
      }
      await sleep(50);
    }
  }
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
async function publishRuntime(root, temporaryRoot, backupRoot, fileSystem, logger) {
  let movedExisting = false;
  try {
    await fileSystem.rename(root, backupRoot);
    movedExisting = true;
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  try {
    await fileSystem.rename(temporaryRoot, root);
  } catch (error) {
    if (movedExisting) await fileSystem.rename(backupRoot, root).catch(() => {});
    throw error;
  }
  if (movedExisting) {
    await fileSystem.rm(backupRoot, { recursive: true, force: true }).catch((error) => {
      logger(`Could not remove superseded runtime cache: ${error.message}`);
    });
  }
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
    if (ready) {
      logger(`Using cached runtime at ${root}`);
      return ready;
    }

    await deps.fileSystem.mkdir(path.dirname(root), { recursive: true, mode: 0o700 });
    const lockPath = `${root}.lock`;
    await acquireLock(lockPath, {
      ...deps,
      lockTimeoutMs: options.lockTimeoutMs ?? 120_000,
    });
    try {
      const afterLock = await inspectReady(root, expected, deps);
      if (afterLock) return afterLock;

      const id = deps.randomUUID();
      const temporaryRoot = `${root}.tmp-${id}`;
      const backupRoot = `${root}.previous-${id}`;
      let temporaryCreated = false;
      try {
        logger(`Bootstrapping runtime at ${root}`);
        await deps.fileSystem.mkdir(temporaryRoot, { mode: 0o700 });
        temporaryCreated = true;
        const manifest = await buildRuntime(context, temporaryRoot);
        await publishRuntime(root, temporaryRoot, backupRoot, deps.fileSystem, logger);
        temporaryCreated = false;
        return {
          python: path.join(root, manifest.pythonRelative),
          root,
          manifest,
        };
      } finally {
        if (temporaryCreated) {
          await deps.fileSystem.rm(temporaryRoot, { recursive: true, force: true }).catch(() => {});
        }
      }
    } finally {
      await deps.fileSystem.rm(lockPath, { recursive: true, force: true }).catch(() => {});
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
