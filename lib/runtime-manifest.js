function runtimeError(code, message) {
  return Object.assign(new Error(message), { code });
}

function selectRuntimeAsset(manifest, platformKey) {
  const asset = manifest?.assets?.[platformKey];
  if (!asset) {
    throw runtimeError("unsupported_platform", `No managed Python runtime is available for ${platformKey}`);
  }

  return {
    url: asset.url,
    sha256: asset.sha256,
    bytes: asset.bytes,
    archiveType: asset.archiveType,
  };
}

module.exports = { selectRuntimeAsset };
