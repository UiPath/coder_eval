// delegate_host.mjs — a stdio JSON-Lines host wrapping @uipath/delegate-sdk's
// DelegateAgent class, standing in for the internal (non-public)
// @uipath/delegate-stdio package the framework's UiPath-only sibling used to
// drive. This host is first-party to coder_eval (there is no vendor-provided
// equivalent to point at once @uipath/delegate-sdk is the only public piece),
// so IT defines the wire protocol below rather than reverse-engineering one.
//
// See src/coder_eval/agents/delegate_agent.py's module docstring for the
// Python-side consumer of this exact protocol, and docs/agents/DELEGATE.md
// for the end-to-end setup story.
//
// Usage: node delegate_host.mjs <absolute-path-to-delegate-sdk's-dist/index.mjs>
//
// Protocol (newline-delimited JSON, UTF-8, one object per line):
//
// stdin (coder_eval -> host):
//   {"cmd": "init", "options": {...}}          -- forwarded verbatim to DelegateAgent.initialize()
//   {"cmd": "send", "prompt": str, "sessionId": str|null}
//   {"cmd": "destroy"}
//
// stdout (host -> coder_eval):
//   {"type": "init_ok"}
//   {"type": "init_error", "message": str}
//   {"type": "send_ok", "result": <sendMessage()'s resolved value, or null>}
//   {"type": "send_error", "message": str}
//   {"type": "destroy_error", "message": str}
//   {"type": "protocol_error", "message": str} -- malformed/unknown stdin command; host keeps running
//   {"type": "fatal", "message": str}          -- unrecoverable; the process exits non-zero right after
//   {"type": <event.type>, ...event}           -- every DelegateAgent.onEvent() callback,
//     forwarded with ALL of its own enumerable fields spread onto the line, `type`
//     included. This host does no filtering or renaming: the SDK's own event
//     vocabulary (session_start / thinking / message / tool_call / tool_result /
//     error / done / step / ... at last check) and per-event field names are
//     whatever the installed @uipath/delegate-sdk version emits. A schema change
//     on a future SDK release surfaces to the Python side as an unrecognized
//     `type` string or missing field, never as a silently dropped event.
//
// CONFIRMED LIVE (installing @uipath/delegate-sdk@0.1.12 and driving this host
// against it directly): importing the SDK can itself write a non-JSON diagnostic
// line to STDOUT before any of this host's own output (observed:
// "[backendUrl] Module loaded - VITE_USE_CLOUD_URL: ..."), not stderr, so the
// Python reader must skip a non-JSON stdout line rather than treat it as a
// protocol violation -- mirroring every other agent's established resilience
// pattern for interleaved non-JSON CLI notices (see opencode_agent.py's
// `_handle_line`). This host does not attempt to suppress the SDK's own stdout
// writes (fragile across SDK versions); the tolerance lives on the read side.
"use strict";

import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const sdkEntryPath = process.argv[2];
if (!sdkEntryPath) {
  process.stderr.write("delegate_host.mjs: missing required argv[1] (path to delegate-sdk's dist/index.mjs)\n");
  process.exit(2);
}

function writeLine(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

let agent = null;
let initialized = false;

async function handleInit(msg) {
  const { DelegateAgent } = await import(pathToFileURL(sdkEntryPath).href);
  agent = new DelegateAgent();
  agent.onEvent((event) => {
    try {
      writeLine({ ...event });
    } catch (err) {
      // A non-JSON-serializable event field (e.g. a circular reference) must not
      // kill the host silently -- surface it as fatal so the Python side sees a
      // clear crash instead of hanging on a line that will never arrive. Exits,
      // like every other `fatal` site: the Python side's crash-handling treats
      // ALL `fatal` messages as "the host is exiting", so this one must too.
      writeLine({ type: "fatal", message: `event serialization failed: ${err}` });
      process.exit(1);
    }
  });
  await agent.initialize(msg.options || {});
  initialized = true;
  writeLine({ type: "init_ok" });
}

async function handleSend(msg) {
  if (!initialized || !agent) {
    writeLine({ type: "send_error", message: "not initialized -- send {\"cmd\":\"init\"} first" });
    return;
  }
  const result = await agent.sendMessage(msg.prompt, msg.sessionId || undefined);
  writeLine({ type: "send_ok", result: result ?? null });
}

async function handleDestroy() {
  if (agent) {
    await agent.destroy();
  }
  process.exit(0);
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });

rl.on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;

  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch (err) {
    writeLine({ type: "protocol_error", message: `invalid JSON on stdin: ${err}` });
    return;
  }

  const cmd = msg && msg.cmd;
  if (cmd !== "init" && cmd !== "send" && cmd !== "destroy") {
    writeLine({ type: "protocol_error", message: `unknown cmd: ${cmd}` });
    return;
  }

  const dispatch = cmd === "init" ? handleInit(msg) : cmd === "send" ? handleSend(msg) : handleDestroy();
  const errorKind = { init: "init_error", send: "send_error", destroy: "destroy_error" }[cmd];
  dispatch.catch((err) => {
    writeLine({ type: errorKind, message: String((err && err.message) || err) });
    // destroy is the teardown path: a failed agent.destroy() must still end
    // the process, or coder_eval's bounded wait burns its full timeout before
    // falling back to SIGKILL on every task.
    if (cmd === "destroy") {
      process.exit(1);
    }
  });
});

// coder_eval closing stdin (its own exit, or a SIGKILL) must end this process
// too, or the host -- and the interop child it may have spawned -- outlives it.
rl.on("close", () => {
  process.exit(0);
});

// Both handlers below write a `fatal` line before exiting so the Python side's
// read loop never hangs waiting for a line that will never arrive -- the
// original failure mode this design exists to avoid (see
// .claude/notes/agents.md § Delegate agent).
process.on("unhandledRejection", (err) => {
  writeLine({ type: "fatal", message: `unhandled rejection: ${(err && err.message) || err}` });
  process.exit(1);
});

process.on("uncaughtException", (err) => {
  writeLine({ type: "fatal", message: `uncaught exception: ${(err && err.message) || err}` });
  process.exit(1);
});
