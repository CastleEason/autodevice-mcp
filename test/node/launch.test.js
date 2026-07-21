const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const { launchMcp } = require("../../lib/launch");

function fakeLaunch() {
  const child = new EventEmitter();
  const signals = new EventEmitter();
  const kills = [];
  const spawns = [];
  child.kill = (signal) => { kills.push(signal); return true; };
  const promise = launchMcp(
    { python: "/runtime/python", root: "/runtime", manifest: {} },
    {
      stdio: "inherit",
      signalSource: signals,
      spawn: (command, args, options) => {
        spawns.push({ command, args, options });
        return child;
      },
    },
  );
  return { child, signals, kills, spawns, promise };
}

test("the MCP child inherits stdio and receives SIGTERM", async () => {
  const launch = fakeLaunch();

  assert.deepEqual(launch.spawns, [{
    command: "/runtime/python",
    args: ["-m", "mobile_auto_mcp.server"],
    options: { stdio: "inherit" },
  }]);
  launch.signals.emit("SIGTERM");
  assert.deepEqual(launch.kills, ["SIGTERM"]);
  launch.child.emit("exit", 0, null);
  assert.equal(await launch.promise, 0);
  assert.equal(launch.signals.listenerCount("SIGTERM"), 0);
});

test("the child exit code becomes the launcher exit code", async () => {
  const launch = fakeLaunch();
  launch.child.emit("exit", 23, null);
  assert.equal(await launch.promise, 23);
});

test("SIGINT is forwarded and its listener is removed after exit", async () => {
  const launch = fakeLaunch();
  launch.signals.emit("SIGINT");
  assert.deepEqual(launch.kills, ["SIGINT"]);
  launch.child.emit("exit", null, "SIGINT");
  assert.equal(await launch.promise, 130);
  assert.equal(launch.signals.listenerCount("SIGINT"), 0);
});

test("a spawn error rejects and removes every signal listener", async () => {
  const launch = fakeLaunch();
  const error = new Error("spawn failed");
  launch.child.emit("error", error);

  await assert.rejects(launch.promise, error);
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    assert.equal(launch.signals.listenerCount(signal), 0);
  }
});
