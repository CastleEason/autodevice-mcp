#!/usr/bin/env node
"use strict";

const pkg = require("../package.json");
const { doctor, ensureRuntime } = require("../lib/bootstrap");
const { parseArgs, renderError } = require("../lib/cli");
const { launchMcp } = require("../lib/launch");

async function main() {
  const mode = parseArgs(process.argv.slice(2));
  if (mode.mode === "version") {
    process.stderr.write(`${pkg.version}\n`);
  } else if (mode.mode === "doctor") {
    process.stderr.write(`${JSON.stringify(await doctor(), null, 2)}\n`);
  } else {
    const runtime = await ensureRuntime({ bestEffort: false });
    if (mode.mode === "serve") {
      process.exitCode = await launchMcp(runtime, { stdio: "inherit" });
    }
  }
}

main().catch((error) => {
  process.stderr.write(`${renderError(error)}\n`);
  process.exitCode = 1;
});
