const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { assertSafeArchiveEntries, extractArchive } = require("../../lib/archive");

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

test("lists and validates tar entries before extracting with argument arrays", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "autodevice-archive-"));
  const destination = path.join(directory, "runtime");
  const calls = [];
  try {
    await extractArchive(
      { archiveType: "tar.gz" },
      path.join(directory, "runtime.tar.gz"),
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

    assert.deepEqual(calls.at(-1), {
      command: "tar",
      args: ["-xzf", path.join(directory, "runtime.tar.gz"), "-C", destination],
    });
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});

test("does not invoke extraction after an unsafe listed entry", async () => {
  let extracted = false;
  await assert.rejects(
    () => extractArchive(
      { archiveType: "tar.gz" },
      "/tmp/runtime.tar.gz",
      "/tmp/runtime-root",
      {
        listEntries: async () => ["../escape"],
        extract: async () => { extracted = true; },
      },
    ),
    { code: "runtime_extract_failed" },
  );
  assert.equal(extracted, false);
});
