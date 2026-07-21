"use strict";

const os = require("node:os");
const { spawn } = require("node:child_process");

/** Launches the Python MCP over inherited stdio and mirrors termination. */
function launchMcp(runtime, options = {}) {
  const spawnChild = options.spawn ?? spawn;
  const signalSource = options.signalSource ?? process;
  const child = spawnChild(runtime.python, ["-m", "mobile_auto_mcp.server"], {
    stdio: options.stdio ?? "inherit",
  });
  const signals = ["SIGINT", "SIGTERM", "SIGHUP"];
  const handlers = new Map(signals.map((signal) => [signal, () => child.kill(signal)]));
  for (const [signal, handler] of handlers) signalSource.on(signal, handler);

  return new Promise((resolve, reject) => {
    const cleanup = () => {
      for (const [signal, handler] of handlers) signalSource.removeListener(signal, handler);
    };
    child.once("error", (error) => {
      cleanup();
      reject(error);
    });
    child.once("exit", (code, signal) => {
      cleanup();
      resolve(code ?? 128 + (os.constants.signals[signal] ?? 0));
    });
  });
}

module.exports = { launchMcp };
