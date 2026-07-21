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
  "    [PSCustomObject]@{ path = $_.FullName; type = $(if ($mode -eq 0xA000) { 'symlink' } else { 'file' }) }",
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
    if (type === "symlink" || type === "hardlink" || type === "link") {
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
    if (detail.startsWith("l")) {
      const marker = ` ${entryPath} -> `;
      const position = detail.lastIndexOf(marker);
      return {
        path: entryPath,
        type: "symlink",
        target: position === -1 ? undefined : detail.slice(position + marker.length),
      };
    }
    if (detail.startsWith("h")) {
      const marker = ` ${entryPath} link to `;
      const position = detail.lastIndexOf(marker);
      return {
        path: entryPath,
        type: "hardlink",
        target: position === -1 ? undefined : detail.slice(position + marker.length),
      };
    }
    return { path: entryPath, type: "file" };
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

async function extractArchive(asset, archivePath, destination, deps = {}) {
  const runFile = deps.runFile ?? defaultRunFile;
  try {
    const entries = deps.listEntries
      ? await deps.listEntries(asset, archivePath)
      : await defaultListEntries(asset, archivePath, runFile);
    assertSafeArchiveEntries(entries, destination);
    await fs.mkdir(destination, { recursive: true });
    if (deps.extract) {
      await deps.extract(asset, archivePath, destination);
    } else {
      await defaultExtract(asset, archivePath, destination, runFile);
    }
  } catch (error) {
    if (error?.code === "runtime_extract_failed") {
      throw error;
    }
    throw runtimeError("Runtime archive extraction failed", error);
  }
}

module.exports = { assertSafeArchiveEntries, extractArchive };
