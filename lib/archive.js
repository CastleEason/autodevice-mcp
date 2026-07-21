const crypto = require("node:crypto");
const { constants } = require("node:fs");
const fs = require("node:fs/promises");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);
const MAX_LISTING_BYTES = 16 * 1024 * 1024;
const ZIP_LIST_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  "Add-Type -AssemblyName System.IO.Compression.FileSystem",
  "$archive = [IO.Compression.ZipFile]::OpenRead($args[0])",
  "try {",
  "  @($archive.Entries | ForEach-Object {",
  "    $mode = ($_.ExternalAttributes -shr 16) -band 0xF000",
  "    $type = if ($mode -eq 0xA000) { 'symlink' } elseif ($_.FullName.EndsWith('/')) { 'directory' } else { 'file' }",
  "    [PSCustomObject]@{ path = $_.FullName; type = $type }",
  "  }) | ConvertTo-Json -Compress",
  "} finally { $archive.Dispose() }",
].join("; ");
const ZIP_EXTRACT_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  "Expand-Archive -LiteralPath $args[0] -DestinationPath $args[1] -Force",
].join("; ");

function runtimeError(message, cause) {
  return Object.assign(new Error(message, cause ? { cause } : undefined), {
    code: "runtime_extract_failed",
  });
}

function isInsideRoot(candidate, root) {
  return candidate === root || candidate.startsWith(`${root}${path.sep}`);
}

function resolveArchivePath(value, base, root, label, { rejectParent = false } = {}) {
  if (typeof value !== "string" || value.length === 0 || value.includes("\0")) {
    throw runtimeError(`Archive ${label} is invalid`);
  }
  const portable = value.replaceAll("\\", "/");
  if (path.posix.isAbsolute(portable) || path.win32.isAbsolute(value)) {
    throw runtimeError(`Archive ${label} is absolute: ${value}`);
  }
  const parts = portable.split("/");
  if (rejectParent && parts.includes("..")) {
    throw runtimeError(`Archive ${label} contains parent traversal: ${value}`);
  }
  const resolved = path.resolve(base, ...parts);
  if (!isInsideRoot(resolved, root)) {
    throw runtimeError(`Archive ${label} escapes the extraction root: ${value}`);
  }
  return resolved;
}

function assertSafeArchiveEntries(entries, root) {
  if (!Array.isArray(entries)) {
    throw runtimeError("Archive entry listing is invalid");
  }
  const resolvedRoot = path.resolve(root);

  for (const entry of entries) {
    const entryPath = typeof entry === "string" ? entry : entry?.path;
    const resolvedEntry = resolveArchivePath(entryPath, resolvedRoot, resolvedRoot, "entry", {
      rejectParent: true,
    });
    const type = typeof entry === "object" ? entry.type : undefined;
    if (type === "hardlink" || type === "link") {
      throw runtimeError(`Archive hardlinks are not supported: ${entryPath}`);
    }
    if (type !== undefined && !["file", "directory", "dir", "symlink"].includes(type)) {
      throw runtimeError(`Archive entry type is not supported: ${type}`);
    }
    if (type === "symlink") {
      if (typeof entry.target !== "string") {
        throw runtimeError(`Archive link target is unavailable: ${entryPath}`);
      }
      resolveArchivePath(entry.target, path.dirname(resolvedEntry), resolvedRoot, "link target");
    }
  }
}

function nonemptyLines(output) {
  return String(output).split(/\r?\n/).filter((line) => line.length > 0);
}

function parseTarEntries(namesOutput, verboseOutput) {
  const names = nonemptyLines(namesOutput);
  const details = nonemptyLines(verboseOutput);
  if (names.length !== details.length) {
    throw runtimeError("Tar entry listings do not agree");
  }

  return names.map((entryPath, index) => {
    const detail = details[index];
    switch (detail[0]) {
      case "-":
        return { path: entryPath, type: "file" };
      case "d":
        return { path: entryPath, type: "directory" };
      case "l": {
        const marker = ` ${entryPath} -> `;
        const position = detail.lastIndexOf(marker);
        return {
          path: entryPath,
          type: "symlink",
          target: position === -1 ? undefined : detail.slice(position + marker.length),
        };
      }
      case "h":
        return { path: entryPath, type: "hardlink" };
      case "p":
        return { path: entryPath, type: "fifo" };
      case "b":
        return { path: entryPath, type: "block-device" };
      case "c":
        return { path: entryPath, type: "character-device" };
      case "s":
        return { path: entryPath, type: "socket" };
      default:
        return { path: entryPath, type: "unknown" };
    }
  });
}

async function defaultRunFile(command, args) {
  return execFileAsync(command, args, {
    encoding: "utf8",
    maxBuffer: MAX_LISTING_BYTES,
    windowsHide: true,
  });
}

async function listTarEntries(archivePath, runFile) {
  const names = await runFile("tar", ["-tzf", archivePath]);
  const details = await runFile("tar", ["-tvzf", archivePath]);
  return parseTarEntries(names.stdout, details.stdout);
}

async function listZipEntries(archivePath, runFile) {
  const result = await runFile("powershell.exe", [
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    ZIP_LIST_SCRIPT,
    archivePath,
  ]);
  const parsed = JSON.parse(result.stdout || "[]");
  return Array.isArray(parsed) ? parsed : [parsed];
}

async function defaultListEntries(asset, archivePath, runFile) {
  if (asset.archiveType === "tar.gz") {
    return listTarEntries(archivePath, runFile);
  }
  if (asset.archiveType === "zip") {
    return listZipEntries(archivePath, runFile);
  }
  throw runtimeError(`Unsupported runtime archive type: ${asset.archiveType}`);
}

async function defaultExtract(asset, archivePath, destination, runFile) {
  if (asset.archiveType === "tar.gz") {
    await runFile("tar", ["-xzf", archivePath, "-C", destination]);
    return;
  }
  if (asset.archiveType === "zip") {
    await runFile("powershell.exe", [
      "-NoLogo",
      "-NoProfile",
      "-NonInteractive",
      "-Command",
      ZIP_EXTRACT_SCRIPT,
      archivePath,
      destination,
    ]);
    return;
  }
  throw runtimeError(`Unsupported runtime archive type: ${asset.archiveType}`);
}

function validateArchiveAsset(asset) {
  if (
    !asset
    || !Number.isSafeInteger(asset.bytes)
    || asset.bytes <= 0
    || typeof asset.sha256 !== "string"
    || !/^[a-fA-F0-9]{64}$/.test(asset.sha256)
    || !["tar.gz", "zip"].includes(asset.archiveType)
  ) {
    throw runtimeError("Runtime archive metadata is invalid");
  }
}

async function writeAll(handle, buffer) {
  let offset = 0;
  while (offset < buffer.length) {
    const result = await handle.write(buffer, offset, buffer.length - offset, null);
    const bytesWritten = result?.bytesWritten;
    if (!Number.isInteger(bytesWritten) || bytesWritten <= 0 || bytesWritten > buffer.length - offset) {
      throw runtimeError("Runtime archive staging write was incomplete");
    }
    offset += bytesWritten;
  }
}

async function digestHandle(handle, bytes) {
  const hash = crypto.createHash("sha256");
  const buffer = Buffer.allocUnsafe(Math.min(64 * 1024, bytes));
  let position = 0;
  while (position < bytes) {
    const length = Math.min(buffer.length, bytes - position);
    const result = await handle.read(buffer, 0, length, position);
    const bytesRead = result?.bytesRead;
    if (!Number.isInteger(bytesRead) || bytesRead <= 0 || bytesRead > length) {
      throw runtimeError("Runtime archive staging copy could not be verified");
    }
    hash.update(buffer.subarray(0, bytesRead));
    position += bytesRead;
  }
  return hash.digest();
}

async function copyVerifiedArchive(asset, archivePath, privateArchivePath, fileSystem) {
  const sourceInfo = await fileSystem.lstat(archivePath);
  if (sourceInfo.isSymbolicLink() || !sourceInfo.isFile()) {
    throw runtimeError("Runtime archive source must be a regular file");
  }

  let source;
  let target;
  try {
    const noFollow = constants.O_NOFOLLOW ?? 0;
    source = await fileSystem.open(archivePath, constants.O_RDONLY | noFollow);
    const openedSourceInfo = await source.stat();
    if (!openedSourceInfo.isFile()) {
      throw runtimeError("Runtime archive source must remain a regular file");
    }

    target = await fileSystem.open(privateArchivePath, "wx+", 0o600);
    const sourceHash = crypto.createHash("sha256");
    const buffer = Buffer.allocUnsafe(64 * 1024);
    let copied = 0;
    while (true) {
      const result = await source.read(buffer, 0, buffer.length, null);
      if (result.bytesRead === 0) break;
      copied += result.bytesRead;
      if (copied > asset.bytes) {
        throw runtimeError("Runtime archive exceeds the pinned byte size while staging");
      }
      const chunk = buffer.subarray(0, result.bytesRead);
      sourceHash.update(chunk);
      await writeAll(target, chunk);
    }

    if (copied !== asset.bytes) {
      throw runtimeError(`Runtime archive size changed before extraction: expected ${asset.bytes}, copied ${copied}`);
    }
    const expectedDigest = Buffer.from(asset.sha256, "hex");
    if (!crypto.timingSafeEqual(sourceHash.digest(), expectedDigest)) {
      throw runtimeError("Runtime archive checksum changed before extraction");
    }

    await target.chmod(0o600);
    await target.sync();
    const targetInfo = await target.stat();
    if (targetInfo.size !== asset.bytes) {
      throw runtimeError("Runtime archive staging copy has an unexpected persisted size");
    }
    if (!crypto.timingSafeEqual(await digestHandle(target, asset.bytes), expectedDigest)) {
      throw runtimeError("Runtime archive staging copy has an unexpected persisted checksum");
    }
  } finally {
    await source?.close().catch(() => {});
    await target?.close().catch(() => {});
  }
}

async function lstatIfPresent(fileSystem, value) {
  try {
    return await fileSystem.lstat(value);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

function isTrustedSystemSymlink(info) {
  const currentUid = process.getuid?.();
  return currentUid !== undefined && currentUid !== 0 && info.uid === 0;
}

async function ensureSafeDirectoryPath(fileSystem, directory) {
  const resolved = path.resolve(directory);
  const root = path.parse(resolved).root;
  let current = root;
  for (const part of path.relative(root, resolved).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    let info = await lstatIfPresent(fileSystem, current);
    if (!info) {
      await fileSystem.mkdir(current, { mode: 0o700 });
      info = await fileSystem.lstat(current);
    }
    if (info.isSymbolicLink()) {
      if (isTrustedSystemSymlink(info)) continue;
      throw runtimeError(`Runtime extraction ancestor is unsafe: ${current}`);
    }
    if (!info.isDirectory()) {
      throw runtimeError(`Runtime extraction ancestor is unsafe: ${current}`);
    }
  }
  return resolved;
}

async function assertDestinationAbsent(fileSystem, destination) {
  if (await lstatIfPresent(fileSystem, destination)) {
    throw runtimeError(`Runtime extraction destination already exists: ${destination}`);
  }
}

async function extractArchive(asset, archivePath, destination, deps = {}) {
  validateArchiveAsset(asset);
  const runFile = deps.runFile ?? defaultRunFile;
  const fileSystem = deps.fs ?? fs;
  const resolvedDestination = path.resolve(destination);
  const parent = path.dirname(resolvedDestination);
  let workspace;
  try {
    await ensureSafeDirectoryPath(fileSystem, parent);
    await assertDestinationAbsent(fileSystem, resolvedDestination);
    workspace = await fileSystem.mkdtemp(path.join(parent, ".autodevice-extract-"));
    const privateArchivePath = path.join(
      workspace,
      asset.archiveType === "zip" ? "artifact.zip" : "artifact.tar.gz",
    );
    const extractionRoot = path.join(workspace, "payload");
    await fileSystem.mkdir(extractionRoot, { mode: 0o700 });
    await copyVerifiedArchive(asset, archivePath, privateArchivePath, fileSystem);

    const entries = deps.listEntries
      ? await deps.listEntries(asset, privateArchivePath)
      : await defaultListEntries(asset, privateArchivePath, runFile);
    assertSafeArchiveEntries(entries, extractionRoot);
    if (deps.extract) {
      await deps.extract(asset, privateArchivePath, extractionRoot);
    } else {
      await defaultExtract(asset, privateArchivePath, extractionRoot, runFile);
    }

    const extractedRootInfo = await fileSystem.lstat(extractionRoot);
    if (extractedRootInfo.isSymbolicLink() || !extractedRootInfo.isDirectory()) {
      throw runtimeError("Runtime extraction staging root was replaced");
    }
    await ensureSafeDirectoryPath(fileSystem, parent);
    await assertDestinationAbsent(fileSystem, resolvedDestination);
    await fileSystem.rename(extractionRoot, resolvedDestination);
    await fileSystem.rm(workspace, { recursive: true, force: true }).catch(() => {});
    workspace = undefined;
  } catch (error) {
    if (workspace) {
      await fileSystem.rm(workspace, { recursive: true, force: true }).catch(() => {});
    }
    if (error?.code === "runtime_extract_failed") {
      throw error;
    }
    throw runtimeError("Runtime archive extraction failed", error);
  }
}

module.exports = { assertSafeArchiveEntries, extractArchive };
