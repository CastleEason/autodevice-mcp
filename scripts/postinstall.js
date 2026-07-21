"use strict";

const { ensureRuntime } = require("../lib/bootstrap");

async function main() {
  await ensureRuntime({
    bestEffort: true,
    logger: (message) => process.stderr.write(`${message}\n`),
  });
}

main().catch((error) => {
  process.stderr.write(`Runtime bootstrap skipped: ${error.message || String(error)}\n`);
  process.exitCode = 0;
});
