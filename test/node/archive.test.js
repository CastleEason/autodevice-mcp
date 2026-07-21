const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { assertSafeArchiveEntries, extractArchive } = require("../../lib/archive");

function assetFor(content) {
  return {
    sha256: crypto.createHash("sha256").update(content).digest("hex"),
    bytes: content.length,
    archiveType: "tar.gz",
  };
}

async function withTempDirectory(run) {
  const created = await fs.mkdtemp(path.join(os.tmpdir(), "autodevice-archive-"));
  const directory = await fs.realpath(created);
  try {
    await run(directory);
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
}

test("accepts entries that remain beneath the extraction root", () => {
  const root = path.resolve("runtime-root");
  assert.doesNotThrow(() => assertSafeArchiveEntries([
    "python/",
    "python/bin/python3",
    { path: "python/bin/python", type: "symlink", target: "python3" },
  ], root));
});

test("rejects parent traversal", () => {
  const root = path.resolve("runtime-root");
  for (const entry of ["../../escape", "python/../escape"]) {
    assert.throws(() => assertSafeArchiveEntries([entry], root), {
      code: "runtime_extract_failed",
    });
  }
});

test("rejects absolute POSIX and Windows paths", () => {
  const root = path.resolve("runtime-root");
  for (const entry of ["/absolute", "C:\\absolute", "\\\\server\\share\\file"]) {
    assert.throws(() => assertSafeArchiveEntries([entry], root), {
      code: "runtime_extract_failed",
    });
  }
});

test("rejects a symlink whose target escapes the extraction root", () => {
  const root = path.resolve("runtime-root");
  assert.throws(() => assertSafeArchiveEntries([
    { path: "python/bin/python", type: "symlink", target: "../../../escape" },
  ], root), { code: "runtime_extract_failed" });
});

test("accepts a parent-relative symlink target that remains beneath the root", () => {
  const root = path.resolve("runtime-root");
  assert.doesNotThrow(() => assertSafeArchiveEntries([
    { path: "python/bin/python", type: "symlink", target: "../lib/python" },
  ], root));
});

test("rejects hardlinks even when their target is lexically safe", () => {
  const root = path.resolve("runtime-root");
  assert.throws(() => assertSafeArchiveEntries([
    { path: "python/bin/python", type: "hardlink", target: "python3" },
  ], root), { code: "runtime_extract_failed" });
});

test("rejects special and unknown archive entry types", () => {
  const root = path.resolve("runtime-root");
  for (const type of ["fifo", "block-device", "character-device", "socket", "unknown"]) {
    assert.throws(() => assertSafeArchiveEntries([
      { path: `python/${type}`, type },
    ], root), { code: "runtime_extract_failed" });
  }
});

test("lists and validates tar entries before extracting with argument arrays", async () => {
  await withTempDirectory(async (directory) => {
    const destination = path.join(directory, "runtime");
    const archivePath = path.join(directory, "runtime.tar.gz");
    const content = Buffer.from("archive-under-test");
    const calls = [];
    await fs.writeFile(archivePath, content, { mode: 0o600 });

    await extractArchive(
      assetFor(content),
      archivePath,
      destination,
      {
        runFile: async (command, args) => {
          calls.push({ command, args });
          if (args.includes("-tzf")) {
            return { stdout: "python/\npython/bin/python3\npython/bin/python\n" };
          }
          if (args.includes("-tvzf")) {
            return {
              stdout: [
                "drwxr-xr-x  0 root root 0 Jan 1 00:00 python/",
                "-rwxr-xr-x  0 root root 1 Jan 1 00:00 python/bin/python3",
                "lrwxr-xr-x  0 root root 0 Jan 1 00:00 python/bin/python -> python3",
              ].join("\n"),
            };
          }
          return { stdout: "" };
        },
      },
    );

    const listedArchive = calls[0].args[1];
    const extractedArchive = calls.at(-1).args[1];
    const extractionRoot = calls.at(-1).args[3];
    assert.notEqual(listedArchive, archivePath);
    assert.equal(extractedArchive, listedArchive);
    assert.notEqual(extractionRoot, destination);
    assert.equal(calls.at(-1).command, "tar");
    assert.deepEqual(calls.at(-1).args, ["-xzf", listedArchive, "-C", extractionRoot]);
    assert.ok((await fs.stat(destination)).isDirectory());
  });
});

test("does not invoke extraction after an unsafe listed entry", async () => {
  await withTempDirectory(async (directory) => {
    const content = Buffer.from("archive-under-test");
    const archivePath = path.join(directory, "runtime.tar.gz");
    const destination = path.join(directory, "runtime");
    await fs.writeFile(archivePath, content, { mode: 0o600 });
    let extracted = false;

    await assert.rejects(
      () => extractArchive(assetFor(content), archivePath, destination, {
        listEntries: async () => ["../escape"],
        extract: async () => { extracted = true; },
      }),
      { code: "runtime_extract_failed" },
    );
    assert.equal(extracted, false);
    await assert.rejects(fs.stat(destination), { code: "ENOENT" });
  });
});

test("rejects an existing destination symlink without writing through it", async () => {
  await withTempDirectory(async (directory) => {
    const content = Buffer.from("archive-under-test");
    const archivePath = path.join(directory, "runtime.tar.gz");
    const outside = path.join(directory, "outside");
    const destination = path.join(directory, "runtime");
    await fs.writeFile(archivePath, content, { mode: 0o600 });
    await fs.mkdir(outside);
    await fs.symlink(outside, destination, "dir");
    let extracted = false;

    await assert.rejects(
      () => extractArchive(assetFor(content), archivePath, destination, {
        listEntries: async () => ["python/file"],
        extract: async () => { extracted = true; },
      }),
      { code: "runtime_extract_failed" },
    );
    assert.equal(extracted, false);
    assert.deepEqual(await fs.readdir(outside), []);
  });
});

test("rejects a symlinked destination ancestor without writing through it", async () => {
  await withTempDirectory(async (directory) => {
    const content = Buffer.from("archive-under-test");
    const archivePath = path.join(directory, "runtime.tar.gz");
    const outside = path.join(directory, "outside");
    const linkedParent = path.join(directory, "linked-parent");
    const destination = path.join(linkedParent, "runtime");
    await fs.writeFile(archivePath, content, { mode: 0o600 });
    await fs.mkdir(outside);
    await fs.symlink(outside, linkedParent, "dir");
    let extracted = false;

    await assert.rejects(
      () => extractArchive(assetFor(content), archivePath, destination, {
        listEntries: async () => ["python/file"],
        extract: async () => { extracted = true; },
      }),
      { code: "runtime_extract_failed" },
    );
    assert.equal(extracted, false);
    assert.deepEqual(await fs.readdir(outside), []);
  });
});

test("allows a temp path behind root-owned system symlink ancestors", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "autodevice-system-link-"));
  try {
    const content = Buffer.from("archive-under-test");
    const archivePath = path.join(directory, "runtime.tar.gz");
    const destination = path.join(directory, "runtime");
    await fs.writeFile(archivePath, content, { mode: 0o600 });

    await extractArchive(assetFor(content), archivePath, destination, {
      listEntries: async () => ["python/"],
      extract: async (_asset, _archive, root) => {
        await fs.mkdir(path.join(root, "python"));
      },
    });

    assert.ok((await fs.stat(path.join(destination, "python"))).isDirectory());
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});

test("lists and extracts the same immutable private archive copy", async () => {
  await withTempDirectory(async (directory) => {
    const original = Buffer.from("verified-archive");
    const replacement = Buffer.from("replaced-after-listing");
    const archivePath = path.join(directory, "runtime.tar.gz");
    const destination = path.join(directory, "runtime");
    await fs.writeFile(archivePath, original, { mode: 0o600 });
    let listedArchive;
    let extractedArchive;
    let extractedContent;

    await extractArchive(assetFor(original), archivePath, destination, {
      listEntries: async (_asset, value) => {
        listedArchive = value;
        await fs.writeFile(archivePath, replacement);
        return ["python/"];
      },
      extract: async (_asset, value, root) => {
        extractedArchive = value;
        extractedContent = await fs.readFile(value);
        await fs.mkdir(path.join(root, "python"));
      },
    });

    assert.notEqual(listedArchive, archivePath);
    assert.equal(extractedArchive, listedArchive);
    assert.deepEqual(extractedContent, original);
    assert.ok((await fs.stat(path.join(destination, "python"))).isDirectory());
  });
});

test("removes private staging state when extraction fails", async () => {
  await withTempDirectory(async (directory) => {
    const content = Buffer.from("archive-under-test");
    const archivePath = path.join(directory, "runtime.tar.gz");
    const destination = path.join(directory, "runtime");
    await fs.writeFile(archivePath, content, { mode: 0o600 });

    await assert.rejects(
      () => extractArchive(assetFor(content), archivePath, destination, {
        listEntries: async () => ["python/file"],
        extract: async (_asset, _archive, root) => {
          await fs.writeFile(path.join(root, "partial"), "partial");
          throw new Error("extract failed");
        },
      }),
      { code: "runtime_extract_failed" },
    );

    await assert.rejects(fs.stat(destination), { code: "ENOENT" });
    assert.deepEqual(await fs.readdir(directory), ["runtime.tar.gz"]);
  });
});
