const test = require("node:test");
const assert = require("node:assert/strict");
const { parseArgs, renderError } = require("../../lib/cli");

test("defaults to MCP serve mode", () => {
  assert.deepEqual(parseArgs([]), { mode: "serve" });
});

test("parses supported maintenance modes", () => {
  assert.deepEqual(parseArgs(["--doctor"]), { mode: "doctor" });
  assert.deepEqual(parseArgs(["--bootstrap-only"]), { mode: "bootstrap" });
  assert.deepEqual(parseArgs(["--version"]), { mode: "version" });
});

test("renders stable bootstrap errors without stdout content", () => {
  assert.equal(renderError({ code: "python_not_found", message: "missing" }), "[python_not_found] missing");
});
