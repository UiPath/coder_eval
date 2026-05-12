---
name: uipath-flow-v2
description: "UiPath Flow v2 — author flows as FIL (a TypeScript subset) plus a manifest and bindings. Inverse of `.flow` JSON: program logic in FIL, per-node config in a manifest, connection IDs in bindings. Verify locally with `flow-run` (compiles the FIL, checks bindings against your tenant, executes nodes against real connectors); use `convert.sh + uip maestro flow validate` only as a final compatibility check before deploy."
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# UiPath Flow v2

A v2 flow project is **three files**:

| File                | Role                                                                                  |
| ------------------- | ------------------------------------------------------------------------------------- |
| `<Name>.fil`     | FIL source — async TypeScript-subset describing the flow's logic and call sequence    |
| `<Name>.manifest.flow`  | Manifest — per-node config (typeRef, label, connector inputs)                         |
| `bindings.json`     | Connection IDs (folder, connector, agent process keys) referenced by the manifest     |

**Primary verifier:** `flow-run <project-dir>` compiles the FIL, validates every connector binding against your tenant (via `uip is connections list`), and executes each pending node decision via `uip is resources execute`. It records every dispatch in `decisions.json` so you can see what was sent to each connector and what came back. Use it as the inner loop while authoring.

**Final compatibility check (deploy gate):** `./convert.sh <Name>` produces `<Name>.flow` (the v1 form), then `uip maestro flow validate <Name>.flow` cross-checks against the v1 schema. Run this before declaring the flow shippable, but don't depend on it for iteration — `flow-run`'s errors are far more actionable than `convert + validate`'s output.

## Authoring loop

1. Decide what the flow does. Sketch the node sequence on paper (or in a comment block).
2. Look up the connector node types you need with `uip maestro flow registry search "<keyword>"` — returns matching `NodeType + DisplayName + Description`. Filter to `NodeType` starting with `uipath.connector.` (other matches are agent-tool wrappers you don't want). See "Looking up connectors" below.
3. Write the FIL source (`<Name>.fil`).
4. Write the manifest (`<Name>.manifest.flow`) with one entry per FIL `executeNode` call plus the trigger/end/control-flow nodes.
5. Write `bindings.json`. Stub UUIDs (`00000000-…`) are fine for the first pass; flow-run's pre-flight will tell you which need real values and offer candidates from your tenant.
6. **Verify with `./verify.sh`** (the inner authoring loop):
   - `./verify.sh` — compiles FIL, resolves bindings, lists what would be dispatched. No connector calls. Surfaces compile errors with file:line:col, missing/stub bindings, unknown node types.
   - Replace stub UUIDs with real connection IDs from `verify.sh`'s "candidates for X:" hints (or from `uip is connections list` directly).
   - `./verify.sh --live` — runs for real. Each connector call goes through `uip is resources execute`; HTTP nodes via `fetch()`. After completion, read `.flow-run/decisions.json` to see exact inputs and outputs per node.
   - Iterate. Compile errors → fix the FIL. Connector failures → adjust the `inputs` you pass via `executeNode(...)` (the failure envelope is preserved in `decisions.json`).
7. **Final compatibility check** (only when the flow is otherwise done):
   - `./convert.sh <Name>` → writes `<Name>.flow`.
   - `uip maestro flow validate <Name>.flow`.
   - `uip maestro flow tidy <Name>.flow` → auto-layout so the v1 file is openable in the designer.
   - Validation errors typically point at manifest fields; conversion errors at FIL syntax.

## FIL — the language

FIL is a parseable subset of TypeScript. Anything that parses as TypeScript and uses only the supported features compiles. **No imports, no classes, no first-class functions, no closures.**

### Types

```typescript
let n: i32 = 42;          // 32-bit integer
let big: i64 = 5n;        // 64-bit integer (BigInt syntax)
let pi: f64 = 3.14;       // 64-bit float
let ok: bool = true;      // boolean
let s: string = "hi";     // string
let j: json = ({ a: 1 }); // JSON object/array — stored as a string internally
const t: DateTime = getDateTime();
const dur: TimeSpan = TimeSpan.fromMinutes(5n);
```

`json` values are wrapped/parsed via `JSON.stringify(...)` and `JSON.parse(...)`. Use `JSON.parse` to read fields off a node's output; use `JSON.stringify` to build a node's input.

### Functions and the `main` entry

Every flow has exactly one `main`:

```typescript
async function main(): Promise<void> {
  // …
}
```

Helper async functions become **subflows** when converted to v1. Helper sync functions named `__script_<id>` become **script** nodes.

### Calling a node

`executeNode(nodeType, inputJson)` runs a flow node. Always `await` it.

```typescript
const raw = await executeNode("uipath.connector.uipath-microsoft-outlook365.get-email-list", JSON.stringify({
  parentFolderId: "inbox",
  unReadOnly: true,
}));
const emails = JSON.parse(raw);
```

The `nodeType` string MUST be the v1 node type — e.g. `uipath.connector.<connector>.<action>`, `core.action.script`, `core.logic.decision`. (For connectors, look it up with `uip maestro flow registry search`.) The `inputJson` is whatever the node expects, JSON-stringified.

### Control flow

```typescript
// Branch
if (priority === "urgent") {
  await executeNode("...", "{}");
} else {
  await executeNode("...", "{}");
}

// Sequential await
await executeNode("a", "{}");
await executeNode("b", "{}");

// Loop (pass the item directly — see "Object literals" caveat below)
for (const item of items) {
  await executeNode("process", JSON.stringify(item));
}
```

### Timers and SLA-style waits

```typescript
// Fixed delay
await executeTimer(TimeSpan.fromMinutes(15n));

// Race a node against a deadline
const idx = await Promise.any([
  executeNode("longRunning", "{}"),
  executeTimer(DateTime.now().add(TimeSpan.fromHours(1n))),
]);
if (idx === 1) {
  await executeNode("notifyTimeout", "{}");
}
```

### Variables

A `let` (or typed `const`) at function scope becomes a v1 flow variable. References to it from inside script bodies and connector input expressions resolve to `vars.<name>` in v1. Function parameters become `in` flow variables.

```typescript
async function main(orderId: string): Promise<void> {
  let attempts: i32 = 0;
  // … attempts++ inside a body becomes a setvar script node + variableUpdate
}
```

## Manifest — `<Name>.manifest.flow`

The manifest is a flat dictionary of nodes keyed by node id. Every executeNode call in FIL needs a corresponding manifest entry; the trigger and end (and any control-flow nodes the converter generates) also live here.

Minimum shape:

```json
{
  "schemaVersion": "1",
  "flowId": "order-notify",
  "flowName": "Order Notify",
  "flowVersion": "1.0.0",
  "nodes": {
    "start": {
      "type": "core.trigger.manual@1.0.0",
      "label": "Start",
      "rawInputs": {}
    },
    "getEmails": {
      "type": "uipath.connector.uipath-microsoft-outlook365.get-email-list@1.0.0",
      "label": "Get Emails",
      "binding": "bOutlook",
      "folderBinding": "bFolderKey",
      "configuration": {}
    },
    "end1": {
      "type": "core.control.end@1.0.0",
      "label": "End",
      "rawInputs": {}
    }
  }
}
```

### Node entry fields

| Field             | Required | Description                                                                                                                |
| ----------------- | -------- | -------------------------------------------------------------------------------------------------------------------------- |
| `type`            | yes      | `<nodeType>@<version>` — must match the v1 node type the agent meant                                                       |
| `ui.position`     | no       | `{x, y}` for layout (auto-laid-out if absent)                                                                              |
| `label`           | no       | Display name; defaults to library label for integration nodes                                                              |
| `rawInputs`       | for non-integration nodes | Raw v1 `inputs` blob (control-flow, script, agent nodes carry this verbatim)                                  |
| `binding`         | for integration nodes | Symbolic id of a `bindings.json` entry whose `propertyAttribute` is `ConnectionId`. `flow-run` resolves it to the real UUID. |
| `folderBinding`   | for integration nodes | Symbolic id of a `bindings.json` entry whose `propertyAttribute` is `FolderKey`. Required.                                  |
| `configuration`   | for integration nodes | Per-instance field values (anything not deterministically known from the canonical library)                                |
| `inputs`          | sometimes | Top-level inputs (`queryParameters`, etc.) the connector expects but that aren't in the configuration blob                 |
| `outputs`         | no       | Override the library's default output schema                                                                               |

For **control-flow nodes** (`core.logic.decision`, `core.logic.merge`, `core.control.end`, `core.trigger.manual`, `core.logic.loop`, `core.logic.delay`), the converter auto-generates them from FIL's `if`/`for`/`return`/`executeTimer` — but they MUST appear in the manifest with at least `type` and `rawInputs: {}` (or the right `inputs` for decision/loop).

For **integration nodes**, list the per-instance config fields under `configuration` (e.g. `{ "messageToSend": "Hello!" }`). The conversion path will fill in Tier-A defaults, library-derived statics, and the cached `fieldsContainer`.

For **script nodes** (`core.action.script@1.0.0`), set `rawInputs: { "script": "<v1 JS body>" }`. The FIL `__script_<id>` helper functions wrap into this automatically — but the manifest entry must still exist.

## Bindings — `bindings.json`

`bindings.json` is the **single source of truth for connection UUIDs**. The manifest references each binding by symbolic `id` (e.g. `"bOutlook"`); `flow-run` resolves the id against `bindings.json` at load time to find the real UUID. Storing UUIDs in one place keeps the flow portable: ship the FIL + manifest as-is and the next person just fills in their own `bindings.json`.

Required structure:

1. Every connector node has BOTH a `binding` (symbolic id pointing to a `propertyAttribute: "ConnectionId"` entry) AND a `folderBinding` (symbolic id pointing to a `propertyAttribute: "FolderKey"` entry).
2. Every binding entry has `id` (short symbolic name), `name`, `type: "string"`, `resource`, `resourceKey`, `default`, and `propertyAttribute`. Missing fields trigger `Schema validation failed`.

### Where each UUID comes from

Run once at the start of authoring:

```bash
uip is connections list --output json
```

Each `Data[]` entry has two fields you need:

| `uip` output field | bindings.json field          | What it identifies         |
| ------------------ | ---------------------------- | -------------------------- |
| `Id`               | `resourceKey` + `default` on a `propertyAttribute: "ConnectionId"` entry | A specific connector connection |
| `FolderKey`        | `resourceKey` + `default` on the `propertyAttribute: "FolderKey"` entry  | The folder all your connections live in (usually one per project) |

### Template

```json
{
  "schemaVersion": "1",
  "bindings": [
    {
      "id": "bFolderKey",
      "name": "FolderKey",
      "type": "string",
      "resource": "Connection",
      "resourceKey": "<folder-key>",
      "default": "<folder-key>",
      "propertyAttribute": "FolderKey"
    },
    {
      "id": "bOutlook",
      "name": "Outlook connection",
      "type": "string",
      "resource": "Connection",
      "resourceKey": "<outlook-connection-id>",
      "default": "<outlook-connection-id>",
      "propertyAttribute": "ConnectionId"
    }
  ]
}
```

`flow-run` rejects entries where `resourceKey`/`default` is still in `<...>` placeholder form (or the legacy `00000000-...` stub).

### Manifest references bindings by id, not UUID

```json
"getEmails": {
  "type": "uipath.connector.uipath-microsoft-outlook365.get-email-list@1.0.0",
  "binding": "bOutlook",
  "folderBinding": "bFolderKey",
  "configuration": {}
}
```

The manifest never holds real UUIDs — that's exclusively `bindings.json`. If `flow-run` reports `binding "bOutlook" is not declared in bindings.json`, you have a typo or missing entry in `bindings.json`.

## Definitions — handled automatically

`v2-to-v1` ships a built-in catalog of v1 `definitions[]` entries for the standard control-flow types (`core.trigger.manual`, `core.control.end`, `core.logic.decision`, `core.logic.merge`, `core.action.script`, `core.subflow`, …). You don't need to ship `embeddedDefinitions` for these — the converter fills them in. Connector definitions come from the canonical library on disk (`integrations/library/<connector>/<action>@<version>.v1def.json`). Add `embeddedDefinitions` to your manifest only if you reference a custom node type the canonical library doesn't carry (e.g. a tenant-specific `uipath.core.agent.<uuid>`).

## Worked examples

Two reference flows ship in `example/`. **Read them before authoring** — they're the fastest way to pattern-match the structure you need.

| File | Shows |
| ---- | ----- |
| `example/OrderConfirm.{fil,manifest.flow}` | Minimal 2-connector flow: list emails, branch on count, send a Slack DM. Good for first reading. |
| `example/CommitMonitor.{fil,manifest.flow}` | **Multi-connector + HTTP + loop + decision + counter + digest.** Closely mirrors the structure of most real flows: hardcoded array, for-of, HTTP fetch with string-concat URL, JSON.parse double-pattern for the HTTP response, string-contains decision, two connector calls in different branches, final digest email with `"count: " + i32` string-concat. Adapt this when you need any of these patterns. |

Both share `example/bindings.json` (Outlook, Slack, FolderKey).

## Verifying with flow-run

`flow-run` is the primary local verifier. It does what convert+validate can't: catches FIL compile errors with line numbers, validates bindings against the connections actually in your tenant, and executes each connector call so you see real success/failure.

```bash
flow-run <project-dir>            # compiles, verifies, runs
flow-run <project-dir> --dry-run  # compiles + verifies; no connector calls
flow-run <project-dir> --verbose  # log every step
flow-run <project-dir> --resume   # continue from a previous partial run
```

Outputs (under `<dir>/.flow-run/`, gitignored automatically):
- `history.yaml` — replay log; consumed by `--resume`.
- `decisions.json` — one record per dispatched node, with parsed input + output JSON, the connector key + verb + object name, and dispatch timing. **This is what you read to debug.**

What flow-run catches that convert+validate doesn't:

| Symptom | flow-run output |
| --- | --- |
| FIL syntax error | `file.fil:12:34: [parse] <message>` with the offending source line and a caret |
| stub UUID in `bindings.json` | `binding is a stub UUID (00000000-...). Replace with a real ConnectionId from \`uip is connections list\`.` |
| binding UUID not in your tenant | error names the binding + lists candidate real connection IDs for that connector |
| connector returned an error | full provider envelope preserved in `decisions.json` (e.g. Slack `channel_not_found`, Outlook `Id is malformed`, etc.) |
| node type not recognized | error names the node id and the missing connector library entry |

### STOP on WASM runtime errors — do not work around

If `verify.sh` reports a `WebAssembly.RuntimeError` (typically `memory access out of bounds` or `unreachable executed`), that is a **FIL compiler bug**, not an authoring mistake. **Stop immediately**:

1. Copy the offending FIL file to `WASM_BUG_REPRO.fil` (verbatim, no edits).
2. Write a one-paragraph `WASM_BUG_NOTE.md` describing: the source pattern you used, the exact runtime error, and the line of the FIL the error points at.
3. Stop. Do not rewrite the pattern, do not switch to a `__script_` helper, do not work around with `JSON.stringify`, do not change types. The compiler should be fixed, not the workflow.

Working around a compiler bug burns iterations and hides the root cause. The maintainers prefer one halted run with a clean repro over twelve "successful" runs that each hit a different workaround for the same underlying bug. Compile errors and binding errors are normal authoring feedback — iterate on those. WASM runtime errors are not — halt and report.

flow-run dispatches:

- **Connector nodes** (CRUD: `Retrieve`/`Create`/`Update`/`Delete`/`Replace`/`List`) via `uip is resources execute`.
- **HTTP nodes** (`core.action.http`, `core.action.http.v2`) via Node `fetch()`. Output: `{ body, headers, statusCode }`.
- **Mock nodes** (`core.logic.mock`) — return the manifest's configured fixture, or `{}` if unconfigured.
- **Timer decisions** (`executeTimer` from `await executeTimer(...)` or `core.logic.delay`) — record the deadline immediately. Pass `--real-time` to actually sleep.
- **Promise.all** — every entry dispatched concurrently, all must complete; results recorded in entry order.
- **Promise.any** (race) — entries dispatched concurrently, first to resolve wins. Losers recorded as `nodeCancelled:` / `timerCancelled:`. Without `--real-time`, timers resolve their deadline immediately (typically beats real I/O); with `--real-time`, timers actually sleep and the fastest entry wins.

Not yet supported: the 21 connector activities whose `operation.name` is `Download` or `Upload`. flow-run aborts on these with a specific message and points you at `convert.sh + uip maestro flow validate` until support lands.

See `flow-run/README.md` for the full flag reference.

## Final compatibility check (deploy gate)

After flow-run is happy, run the v1 converter + validator + tidy to confirm the flow round-trips into the v1 schema cleanly and is openable in the designer:

```bash
./convert.sh OrderNotify                   # writes OrderNotify.flow
uip maestro flow validate OrderNotify.flow # final compatibility check
uip maestro flow tidy OrderNotify.flow     # auto-layout for the designer view
```

Conversion produces stdout like:

```
Wrote OrderNotify.flow
Definitions: 5
Nodes: 7
Edges: 6
```

Validation prints `"Result": "Success"` on a clean flow, or `"Found N error(s)"` with the offending node id and the structural problem. Errors of the form `[FIELD_REQUIRED]` mean a `configuration.<key>` is missing; check the canonical library entry to see what fields are mandatory. **Don't iterate against this — iterate against `flow-run` first; this step is a final gate.**

## Looking up connectors

`uip maestro flow registry search "<keyword>"` is the way to discover available connectors. It searches `NodeType + Category + Tags + DisplayName + Description` and returns matching nodes. The registry is pre-pulled — you don't need to run `pull`.

```bash
# Find a connector action by description.
# Filter to uipath.connector.* — other matches are agent-tool wrappers.
uip maestro flow registry search "send email" --output json \
  | jq '.Data[] | select(.NodeType | startswith("uipath.connector.")) | {NodeType, DisplayName, Description}'
```

### Getting the input schema for a node — use `--output-filter`

`uip maestro flow registry get <NodeType> --output json` ships ~11–40 KB per call: BPMN model, designer form schema, telemetry, icon URLs — none of which you need when authoring a flow. Use the CLI's built-in **`--output-filter`** (a JMESPath expression applied to the `Data` field) to slice out just the part you actually use. Four filters cover almost every authoring need; **start with #1** to see the call shape, then drill in as needed:

```bash
# 1. The call shape — query vs body vs path parameters. Run this FIRST.
#    If you see {name: "body", type: "body", dataType: "object"} that
#    connector takes a freeform JSON body — see the note below.
uip maestro flow registry get <NodeType> --output json \
  --output-filter 'Node.connectorMethodInfo.parameters[*].{name: name, type: type, dataType: dataType, required: required}'

# 2. Required input fields only — the designer-curated "must fill in" list.
uip maestro flow registry get <NodeType> --output json \
  --output-filter 'Node.inputDefinition.fields[?required==`true`].{name: name, type: type, description: description}'

# 3. All input fields (required + optional) when you need optional flags too.
uip maestro flow registry get <NodeType> --output json \
  --output-filter 'Node.inputDefinition.fields[*].{name: name, type: type, required: required, description: description}'

# 4. Response shape — the fields the connector returns. Useful for downstream consumers.
uip maestro flow registry get <NodeType> --output json \
  --output-filter 'Node.outputResponseDefinition.fields[*].{name: name, type: type, description: description}'
```

Typical reductions: full response ~11–40 KB → required-only filter ~300–500 B (95–99% smaller). Don't pipe the full response through `jq` to filter client-side — `--output-filter` runs server-side and only that goes over the wire.

**`type: "body"` is special.** When filter #1 returns a single parameter `{name: "body", type: "body", dataType: "object"}` (Jira `create-issue`, GitHub create-pr, most "Create X" REST connectors), the connector forwards your JSON body **verbatim to the upstream API**. `inputDefinition.fields[]` only lists the 3–10 fields the designer surfaces; the full accepted shape comes from the upstream API docs (e.g. Jira's `POST /rest/api/3/issue` accepts ~50 fields under `fields`). Build the body yourself and pass it as the input:

```typescript
await executeNode("createIssue", "{\"fields\":{\"project\":{\"key\":\"Maestro\"},\"summary\":\"...\",\"description\":\"...\",\"issuetype\":{\"name\":\"Task\"}}}");
```

Don't burn time looking for `summary`/`description` in the registry — they're not there because the connector doesn't curate them.

Fields you usually do NOT need (and should not request): `Node.model` (BPMN engine config), `Node.form` (designer UI form), `Node.handleConfiguration` (BPMN handle positions), `Node.display` (icon URLs), `Node.connectorMethodInfo.design` (designer-only rules). If you ever do need the whole thing, drop `--output-filter` — but try the slim filters first.

## Common authoring patterns

**HTTP request (no connector).** `core.action.http` only needs `url` (and `method`); other fields default. Put the static fields in the manifest's `rawInputs` and the dynamic bits in the FIL `executeNode` call — the dispatcher merges them (headers/queryParams deep-merge; body/scalars FIL-wins).

```typescript
const url: string = ("https://api.open-meteo.com/v1/forecast?latitude=" + lat);
const raw = await executeNode("fetchWeather", JSON.stringify({ url: url }));
const body = JSON.parse(raw).body;  // HTTP response shape: { body, headers, statusCode }
```

```json
"fetchWeather": {
  "type": "core.action.http@1.0.0",
  "rawInputs": { "method": "GET", "timeout": "PT15M" }
}
```

**Output downstream.**

```typescript
const emailsRaw = await executeNode("getEmails", "{}");
const emails = JSON.parse(emailsRaw);
const subject = (emails[0].subject ?? "(no subject)");
await executeNode("slackPost", JSON.stringify({ channel: "#alerts", messageToSend: ("New: " + subject) }));
```

**Branch on output, counters with `+`.** Binary `+` between a string and any numeric/json value coerces the non-string side automatically:

```typescript
let seen: i32 = 0;
const raw = await executeNode("check", "{}");
if (JSON.parse(raw).healthy) { seen++; await executeNode("ok", "{}"); }
await executeNode("log", "seen: " + seen);   // → "seen: 1"
```

**Flow input parameter.** A `main` parameter becomes a v1 flow `in` variable.

```typescript
async function main(customerId: string): Promise<void> {
  await executeNode("lookup", JSON.stringify({ id: customerId }));
}
```

**Script node — extract from prior output.** A top-level `function __script_<id>(): json { return <expr>; }` becomes a `core.action.script` node when called via `__script_<id>()`. The body uses v1's `=js:` runtime (`$vars.X.output.Y`); the manifest's `rawInputs.script` must match the helper body verbatim.

```typescript
function __script_extractTemp(): json {
  return { temp: $vars.fetchWeather.output.body.current.temperature_2m };
}
```

Read the bundled `example/OrderConfirm.{fil,manifest.flow}` for a runnable 2-connector example.

## Pitfalls

- **Node id vs type.** The string in `executeNode("…", …)` is the manifest key (e.g. `getEmails`), not the full type. The manifest entry's `type` field carries `<nodeType>@<version>`.
- **Trigger + end nodes need manifest entries** even when FIL doesn't name them.
- **Control-flow ids are auto-generated.** Don't author manifest entries for `decision1` / `merge1` / `setvar_<name>_1` — the converter handles them.
- **Connectors need both `binding` and `folderBinding`** — validation rejects missing FolderKey.
- **No object-field mutation.** `obj.foo = 1` doesn't survive v1 conversion — use `obj = { ...obj, foo: 1 }`.
- **No `await` outside async.** Every async helper needs `Promise<void>` (or `Promise<json>`, etc.) as its return type.

### Replay safety: non-deterministic calls go through `executeNode`

`__script_<id>` sync helpers run on **every** FIL replay, so anything depending on `Math.random()`, `Date.now()`, an entropy source, or env state drifts each run and breaks replay. Wrap non-determinism in an `executeNode` call against a `core.action.script` node — the host runs it once, captures the result in history, and replays read the recorded value:

```typescript
// ✗ replay-broken — every replay rolls a different number
function __script_roll(): json { return { value: Math.floor(Math.random() * 6) + 1 }; }

// ✓ replay-safe — manifest "roll" is core.action.script with the body in rawInputs.script
const rolled: string = await executeNode("roll", "{}");
```

`DateTime.now()` (the FIL builtin) is the exception — host-imported and pinned per run.

### Known FIL compiler bugs — write around these patterns

The patterns below currently emit malformed WAT. **Do not work around them silently — if `verify.sh` reports a `WebAssembly.RuntimeError`, halt and report per the "STOP on WASM" section above.** These are listed so you can recognize the patterns and pick an alternative shape from the start.

- **`if`/`else` inside `__script_<id>` helpers.** Use a single `return <ternary>;` instead. For real branching, use `if`/`else` in `main()` with an `executeNode` (or separate script helper) per branch.
- **`if`/`else` in `main()` where both branches only do assignments.** Either give each branch an `await executeNode(...)`, or push the conditional value into a script helper using a ternary.
- **Object literals as call arguments where a property value is a member access or needs conversion** — e.g. `JSON.stringify({ id: item.id })` inside `executeNode(...)`. Workarounds: pass the json field directly (`JSON.stringify(item)`), string-concat (`"{\"id\":" + item.id + "}"`), or stash the object in a `const` first.

The authoritative list lives in `~/src/flow-v2/fil/known-issues.md`. If you hit something not there, that's a new compiler bug — halt + report.
