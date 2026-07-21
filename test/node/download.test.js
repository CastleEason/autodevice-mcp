const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { Readable } = require("node:stream");
const { downloadVerified } = require("../../lib/download");

function response(chunks, { statusCode = 200, headers = {} } = {}) {
  return { statusCode, headers, body: Readable.from(chunks) };
}

function assetFor(content) {
  return {
    url: "https://github.com/owner/repo/releases/download/v1/runtime.tar.gz",
    sha256: crypto.createHash("sha256").update(content).digest("hex"),
    bytes: content.length,
    archiveType: "tar.gz",
  };
}

async function withTempFile(run) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "autodevice-download-"));
  try {
    await run(path.join(directory, "runtime.tar.gz"));
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
}

test("streams a verified artifact into a mode 0600 destination", async () => {
  await withTempFile(async (file) => {
    const content = Buffer.from("verified-runtime");
    const asset = assetFor(content);

    await downloadVerified(asset, file, {
      request: async () => response([content.subarray(0, 4), content.subarray(4)]),
      randomUUID: () => "fixed",
    });

    assert.deepEqual(await fs.readFile(file), content);
    assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
    assert.deepEqual(await fs.readdir(path.dirname(file)), [path.basename(file)]);
  });
});

test("rejects a checksum mismatch and removes the temporary file", async () => {
  await withTempFile(async (file) => {
    const expected = Buffer.from("right");
    const depsWithWrongDigest = {
      request: async () => response([Buffer.from("wrong")]),
      randomUUID: () => "fixed",
    };

    await assert.rejects(() => downloadVerified(assetFor(expected), file, depsWithWrongDigest), {
      code: "runtime_checksum_mismatch",
    });
    await assert.rejects(fs.stat(file), { code: "ENOENT" });
    assert.deepEqual(await fs.readdir(path.dirname(file)), []);
  });
});

test("stops a response once it exceeds the pinned byte ceiling", async () => {
  await withTempFile(async (file) => {
    const content = Buffer.from("12345");
    const oversizedDeps = {
      request: async () => response([content]),
      randomUUID: () => "fixed",
    };

    await assert.rejects(
      () => downloadVerified({ ...assetFor(content), bytes: 4 }, file, oversizedDeps),
      { code: "runtime_download_failed" },
    );
    assert.deepEqual(await fs.readdir(path.dirname(file)), []);
  });
});

test("rejects redirects to hosts outside the download allowlist", async () => {
  await withTempFile(async (file) => {
    let requests = 0;
    const asset = assetFor(Buffer.from("runtime"));

    await assert.rejects(
      () => downloadVerified(asset, file, {
        request: async () => {
          requests += 1;
          return response([], {
            statusCode: 302,
            headers: { location: "https://attacker.example/runtime.tar.gz" },
          });
        },
        randomUUID: () => "fixed",
      }),
      { code: "runtime_download_failed" },
    );
    assert.equal(requests, 1);
  });
});

test("follows a bounded redirect to objects.githubusercontent.com", async () => {
  await withTempFile(async (file) => {
    const content = Buffer.from("runtime");
    const visited = [];

    await downloadVerified(assetFor(content), file, {
      request: async (url) => {
        visited.push(url.href);
        if (url.hostname === "github.com") {
          return response([], {
            statusCode: 302,
            headers: { location: "https://objects.githubusercontent.com/runtime.tar.gz" },
          });
        }
        return response([content]);
      },
      randomUUID: () => "fixed",
    });

    assert.deepEqual(visited, [
      "https://github.com/owner/repo/releases/download/v1/runtime.tar.gz",
      "https://objects.githubusercontent.com/runtime.tar.gz",
    ]);
  });
});

test("follows GitHub's official release-assets CDN redirect", async () => {
  await withTempFile(async (file) => {
    const content = Buffer.from("runtime");
    const visited = [];

    await downloadVerified(assetFor(content), file, {
      request: async (url) => {
        visited.push(url.hostname);
        if (url.hostname === "github.com") {
          return response([], {
            statusCode: 302,
            headers: { location: "https://release-assets.githubusercontent.com/runtime.tar.gz" },
          });
        }
        return response([content]);
      },
      randomUUID: () => "fixed",
    });

    assert.deepEqual(visited, ["github.com", "release-assets.githubusercontent.com"]);
  });
});
