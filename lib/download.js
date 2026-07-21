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

async function downloadVerified(asset, destination, deps = {}) {
  validateAsset(asset);
  const request = deps.request ?? requestOnce;
  const randomUUID = deps.randomUUID ?? crypto.randomUUID;
  const temporary = `${destination}.${process.pid}.${randomUUID()}.tmp`;
  let handle;

  try {
    handle = await fs.open(temporary, "wx", 0o600);
    const response = await requestFollowingRedirects(asset.url, request);
    const declaredLength = Number(response.headers?.["content-length"]);
    if (Number.isFinite(declaredLength) && declaredLength > asset.bytes) {
      response.body.destroy?.();
      throw runtimeError("runtime_download_failed", "Runtime download exceeds the pinned byte ceiling");
    }

    const hash = crypto.createHash("sha256");
    let received = 0;
    for await (const value of response.body) {
      const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
      received += chunk.length;
      if (received > asset.bytes) {
        response.body.destroy?.();
        throw runtimeError("runtime_download_failed", "Runtime download exceeds the pinned byte ceiling");
      }
      hash.update(chunk);
      await handle.write(chunk);
    }

    if (received !== asset.bytes) {
      throw runtimeError(
        "runtime_download_failed",
        `Runtime download size mismatch: expected ${asset.bytes} bytes, received ${received}`,
      );
    }

    const actualDigest = hash.digest();
    const expectedDigest = Buffer.from(asset.sha256, "hex");
    if (!crypto.timingSafeEqual(actualDigest, expectedDigest)) {
      throw runtimeError("runtime_checksum_mismatch", "Runtime download checksum does not match the pinned digest");
    }

    await handle.sync();
    await handle.close();
    handle = undefined;
    await fs.rename(temporary, destination);
    await fs.chmod(destination, 0o600);
  } catch (error) {
    if (handle) {
      await handle.close().catch(() => {});
    }
    await fs.rm(temporary, { force: true }).catch(() => {});
    if (error?.code === "runtime_checksum_mismatch" || error?.code === "runtime_download_failed") {
      throw error;
    }
    throw runtimeError("runtime_download_failed", "Runtime download failed", error);
  }
}

module.exports = { downloadVerified };
