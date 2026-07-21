"use strict";

const MODES = new Map([
  ["--doctor", "doctor"],
  ["--bootstrap-only", "bootstrap"],
  ["--version", "version"],
]);

function parseArgs(argv) {
  if (argv.length === 0) return { mode: "serve" };
  if (argv.length === 1 && MODES.has(argv[0])) return { mode: MODES.get(argv[0]) };
  const error = new Error(`Unsupported arguments: ${argv.join(" ")}`);
  error.code = "invalid_arguments";
  throw error;
}

function renderError(error) {
  return `[${error.code || "unexpected_error"}] ${error.message || String(error)}`;
}

module.exports = { parseArgs, renderError };
