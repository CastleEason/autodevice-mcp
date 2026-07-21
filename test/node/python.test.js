const test = require("node:test");
const assert = require("node:assert/strict");
const { findPython312 } = require("../../lib/python");

test("prefers an explicit compatible Python", async () => {
  const visited = [];
  const result = await findPython312({
    env: { MOBILE_AUTO_MCP_PYTHON: "/python" },
    candidates: ["python3.12"],
    runVersion: async (value) => {
      visited.push(value);
      return value === "/python" ? "3.12.9" : "3.11.0";
    },
  });

  assert.deepEqual(result, { executable: "/python", version: "3.12.9" });
  assert.deepEqual(visited, ["/python"]);
});

test("falls back after an incompatible explicit Python", async () => {
  const visited = [];
  const result = await findPython312({
    env: { MOBILE_AUTO_MCP_PYTHON: "/python" },
    candidates: ["python3.12", "python3"],
    runVersion: async (value) => {
      visited.push(value);
      return value === "python3.12" ? "Python 3.12.7" : "3.11.0";
    },
  });

  assert.deepEqual(result, { executable: "python3.12", version: "3.12.7" });
  assert.deepEqual(visited, ["/python", "python3.12"]);
});

test("rejects all non-3.12 interpreters", async () => {
  const result = await findPython312({
    env: {},
    candidates: ["python3"],
    runVersion: async () => "3.13.1",
  });

  assert.equal(result, null);
});

test("requires a complete stable major.minor.patch version", async () => {
  for (const version of ["3.12", "3.12.9rc1", "3.12.9 extra", "not a version"]) {
    const result = await findPython312({
      env: {},
      candidates: ["python"],
      runVersion: async () => version,
    });
    assert.equal(result, null, version);
  }
});

test("supports argv-aware candidates and skips commands that cannot run", async () => {
  const launcher = ["py", "-3.12"];
  const result = await findPython312({
    env: {},
    candidates: ["missing", launcher],
    runVersion: async (value) => {
      if (value === "missing") throw new Error("ENOENT");
      return "3.12.4";
    },
  });

  assert.deepEqual(result, { executable: launcher, version: "3.12.4" });
});
