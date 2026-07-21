#!/usr/bin/env node
"use strict";

const { parseArgs, renderError } = require("../lib/cli");

try {
  parseArgs(process.argv.slice(2));
  const error = new Error("Runtime bootstrap is not implemented yet");
  error.code = "runtime_unavailable";
  throw error;
} catch (error) {
  process.stderr.write(`${renderError(error)}\n`);
  process.exitCode = 1;
}
