const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const https = require("node:https");

const ALLOWED_DOWNLOAD_HOSTS = new Set([
  "github.com",
  "objects.githubusercontent.com",
  "release-assets.githubusercontent.com",
]);
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const MAX_REDIRECTS = 5;

function runtimeError(code, message, cause) {
  return Object.assign(new Error(message, cause ? { cause } : undefined), { code });
}

function validateDownloadUrl(value) {
  let url;
  try {
    url = value instanceof URL ? value : new URL(value);
  } catch (error) {
    throw runtimeError("runtime_download_failed", "The runtime URL is invalid", error);
  }

  if (url.protocol !== "https:" || !ALLOWED_DOWNLOAD_HOSTS.has(url.hostname)) {
    throw runtimeError("runtime_download_failed", `Runtime download host is not allowed: ${url.hostname}`);
  }
  return url;
}

function requestOnce(url) {
  return new Promise((resolve, reject) => {
    const request = https.get(url, {
      headers: {
        Accept: "application/octet-stream",
        "User-Agent": "autodevice-mcp-runtime-bootstrap",
      },
    }, (response) => {
      resolve({
        statusCode: response.statusCode,
        headers: response.headers,
        body: response,
      });
    });
    request.setTimeout(30_000, () => request.destroy(new Error("Runtime download timed out")));
    request.on("error", reject);
  });
}

async function requestFollowingRedirects(url, request, redirects = 0) {
  const safeUrl = validateDownloadUrl(url);
  const response = await request(safeUrl);
  const statusCode = response.statusCode ?? 0;

  if (REDIRECT_STATUSES.has(statusCode)) {
    response.body?.destroy?.();
    if (redirects >= MAX_REDIRECTS) {
      throw runtimeError("runtime_download_failed", "Runtime download exceeded the redirect limit");
    }
    const location = response.headers?.location;
    if (!location) {
      throw runtimeError("runtime_download_failed", "Runtime download redirect had no location");
    }
    return requestFollowingRedirects(new URL(location, safeUrl), request, redirects + 1);
  }

  if (statusCode < 200 || statusCode >= 300 || !response.body) {
    response.body?.destroy?.();
    throw runtimeError("runtime_download_failed", `Runtime download returned HTTP ${statusCode}`);
  }
  return response;
}

function validateAsset(asset) {
  if (
    !asset
    || !Number.isSafeInteger(asset.bytes)
    || asset.bytes <= 0
    || typeof asset.sha256 !== "string"
    || !/^[a-fA-F0-9]{64}$/.test(asset.sha256)
  ) {
    throw runtimeError("runtime_download_failed", "Runtime asset metadata is invalid");
  }
}

async function writeAll(handle, chunk) {
  let offset = 0;
  while (offset < chunk.length) {
    const result = await handle.write(chunk, offset, chunk.length - offset, null);
    const bytesWritten = result?.bytesWritten;
    if (!Number.isInteger(bytesWritten) || bytesWritten <= 0 || bytesWritten > chunk.length - offset) {
      throw runtimeError("runtime_download_failed", "Runtime temporary file write was incomplete");
    }
    offset += bytesWritten;
  }
}

async function digestPersistedFile(handle, bytes) {
  const hash = crypto.createHash("sha256");
  const buffer = Buffer.allocUnsafe(Math.min(64 * 1024, bytes));
  let position = 0;
  while (position < bytes) {
    const length = Math.min(buffer.length, bytes - position);
    const result = await handle.read(buffer, 0, length, position);
    const bytesRead = result?.bytesRead;
    if (!Number.isInteger(bytesRead) || bytesRead <= 0 || bytesRead > length) {
      throw runtimeError("runtime_download_failed", "Runtime temporary file could not be verified");
    }
    hash.update(buffer.subarray(0, bytesRead));
    position += bytesRead;
  }
  return hash.digest();
}

async function downloadVerified(asset, destination, deps = {}) {
  validateAsset(asset);
  const request = deps.request ?? requestOnce;
  const randomUUID = deps.randomUUID ?? crypto.randomUUID;
  const fileSystem = deps.fs ?? fs;
  const temporary = `${destination}.${process.pid}.${randomUUID()}.tmp`;
  let handle;

  try {
    handle = await fileSystem.open(temporary, "wx+", 0o600);
    const response = await requestFollowingRedirects(asset.url, request);
    const declaredLength = Number(response.headers?.["content-length"]);
    if (Number.isFinite(declaredLength) && declaredLength > asset.bytes) {
      response.body.destroy?.();
      throw runtimeError("runtime_download_failed", "Runtime download exceeds the pinned byte ceiling");
    }

    let received = 0;
    for await (const value of response.body) {
      const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
      received += chunk.length;
      if (received > asset.bytes) {
        response.body.destroy?.();
        throw runtimeError("runtime_download_failed", "Runtime download exceeds the pinned byte ceiling");
      }
      await writeAll(handle, chunk);
    }

    if (received !== asset.bytes) {
      throw runtimeError(
        "runtime_download_failed",
        `Runtime download size mismatch: expected ${asset.bytes} bytes, received ${received}`,
      );
    }

    await handle.chmod(0o600);
    await handle.sync();
    const persisted = await handle.stat();
    if (persisted.size !== asset.bytes) {
      throw runtimeError(
        "runtime_download_failed",
        `Runtime temporary file size mismatch: expected ${asset.bytes} bytes, persisted ${persisted.size}`,
      );
    }

    const actualDigest = await digestPersistedFile(handle, asset.bytes);
    const expectedDigest = Buffer.from(asset.sha256, "hex");
    if (!crypto.timingSafeEqual(actualDigest, expectedDigest)) {
      throw runtimeError("runtime_checksum_mismatch", "Runtime download checksum does not match the pinned digest");
    }

    await handle.close();
    handle = undefined;
    await fileSystem.rename(temporary, destination);
  } catch (error) {
    if (handle) {
      await handle.close().catch(() => {});
    }
    await fileSystem.rm(temporary, { force: true }).catch(() => {});
    if (error?.code === "runtime_checksum_mismatch" || error?.code === "runtime_download_failed") {
      throw error;
    }
    throw runtimeError("runtime_download_failed", "Runtime download failed", error);
  }
}

module.exports = { downloadVerified };
