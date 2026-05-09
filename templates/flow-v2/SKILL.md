---
name: uipath-flow-v2
description: "UiPath Flow v2 — author flows as FIL (a TypeScript subset) plus a manifest and bindings. Inverse of `.flow` JSON: program logic in FIL, per-node config in a manifest, connection IDs in bindings. Verify locally with `flow-run` (compiles the FIL, checks bindings against your tenant, executes nodes against real connectors); use `convert.sh + uip maestro flow validate` only as a final compatibility check before deploy."
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# UiPath Flow v2

A v2 flow project is **three files**:

| File                | Role                                                                                  |
| ------------------- | ------------------------------------------------------------------------------------- |
| `<Name>.fil.ts`     | FIL source — async TypeScript-subset describing the flow's logic and call sequence    |
| `<Name>.manifest.flow`  | Manifest — per-node config (typeRef, label, connector inputs)                         |
| `bindings.json`     | Connection IDs (folder, connector, agent process keys) referenced by the manifest     |

**Primary verifier:** `flow-run <project-dir>` compiles the FIL, validates every connector binding against your tenant (via `uip is connections list`), and executes each pending node decision via `uip is resources execute`. It records every dispatch in `decisions.json` so you can see what was sent to each connector and what came back. Use it as the inner loop while authoring.

**Final compatibility check (deploy gate):** `./convert.sh <Name>` produces `<Name>.flow` (the v1 form), then `uip maestro flow validate <Name>.flow` cross-checks against the v1 schema. Run this before declaring the flow shippable, but don't depend on it for iteration — `flow-run`'s errors are far more actionable than `convert + validate`'s output.

## Authoring loop

1. Decide what the flow does. Sketch the node sequence on paper (or in a comment block).
2. Look up the connector node types you need with `uip maestro flow registry search "<keyword>"` — returns matching `NodeType + DisplayName + Description`. Filter to `NodeType` starting with `uipath.connector.` (other matches are agent-tool wrappers you don't want). See "Looking up connectors" below.
3. Write the FIL source (`<Name>.fil.ts`).
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
      "ui": { "position": { "x": 256, "y": 144 } },
      "label": "Start",
      "rawInputs": {}
    },
    "getEmails": {
      "type": "uipath.connector.uipath-microsoft-outlook365.get-email-list@1.0.0",
      "ui": { "position": { "x": 432, "y": 144 } },
      "label": "Get Emails",
      "binding": "outlookConn",
      "configuration": {}
    },
    "end1": {
      "type": "core.control.end@1.0.0",
      "ui": { "position": { "x": 800, "y": 144 } },
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
| `binding`         | for integration nodes | UUID the connector uses as its `ConnectionId` — also referenced (verbatim) in `bindings.json`                              |
| `folderBinding`   | for integration nodes | UUID for the connector's `FolderKey` — required even when stubbed                                                          |
| `configuration`   | for integration nodes | Per-instance field values (anything not deterministically known from the canonical library)                                |
| `inputs`          | sometimes | Top-level inputs (`queryParameters`, etc.) the connector expects but that aren't in the configuration blob                 |
| `outputs`         | no       | Override the library's default output schema                                                                               |

For **control-flow nodes** (`core.logic.decision`, `core.logic.merge`, `core.control.end`, `core.trigger.manual`, `core.logic.loop`, `core.logic.delay`), the converter auto-generates them from FIL's `if`/`for`/`return`/`executeTimer` — but they MUST appear in the manifest with at least `type` and `rawInputs: {}` (or the right `inputs` for decision/loop).

For **integration nodes**, list the per-instance config fields under `configuration` (e.g. `{ "messageToSend": "Hello!" }`). The conversion path will fill in Tier-A defaults, library-derived statics, and the cached `fieldsContainer`.

For **script nodes** (`core.action.script@1.0.0`), set `rawInputs: { "script": "<v1 JS body>" }`. The FIL `__script_<id>` helper functions wrap into this automatically — but the manifest entry must still exist.

## Bindings — `bindings.json`

`bindings.json` lists the connection-related UUIDs the manifest's `binding` and `folderBinding` fields point at. Validation requires:

1. Every connector node MUST have BOTH a `binding` (its `ConnectionId`) AND a `folderBinding` (a folder-key UUID). Connector validation fails with `FolderKey is required for the connection binding` otherwise.
2. Every binding entry MUST have an `id` (any short string), `name`, `type: "string"`, `resource`, `resourceKey`, `default`, and `propertyAttribute`. Missing fields trigger `Schema validation failed`.

Worked example for two connectors sharing a folder:

```json
{
  "schemaVersion": "1",
  "bindings": [
    {
      "id": "bFolderKey",
      "name": "FolderKey",
      "type": "string",
      "resource": "Connection",
      "resourceKey": "00000000-0000-0000-0000-00000000000F",
      "default": "00000000-0000-0000-0000-00000000000F",
      "propertyAttribute": "FolderKey"
    },
    {
      "id": "b00000001",
      "name": "Outlook connection",
      "type": "string",
      "resource": "Connection",
      "resourceKey": "00000000-0000-0000-0000-000000000001",
      "default": "00000000-0000-0000-0000-000000000001",
      "propertyAttribute": "ConnectionId"
    }
  ]
}
```

The manifest then references these UUIDs:

```json
"getEmails": {
  "type": "uipath.connector.uipath-microsoft-outlook365.get-email-list@1.0.0",
  "binding": "00000000-0000-0000-0000-000000000001",
  "folderBinding": "00000000-0000-0000-0000-00000000000F",
  "configuration": {}
}
```

For first-iteration authoring, **stub UUIDs are fine** (`00000000-0000-0000-0000-000000000000`). The validation pass cares about structure, not whether the connection actually exists. Real deployment fills these in.

## Definitions — handled automatically

`v2-to-v1` ships a built-in catalog of v1 `definitions[]` entries for the standard control-flow types (`core.trigger.manual`, `core.control.end`, `core.logic.decision`, `core.logic.merge`, `core.action.script`, `core.subflow`, …). You don't need to ship `embeddedDefinitions` for these — the converter fills them in. Connector definitions come from the canonical library on disk (`integrations/library/<connector>/<action>@<version>.v1def.json`). Add `embeddedDefinitions` to your manifest only if you reference a custom node type the canonical library doesn't carry (e.g. a tenant-specific `uipath.core.agent.<uuid>`).

## Worked example

See `example/OrderConfirm.fil.ts`, `example/OrderConfirm.manifest.flow`, `example/bindings.json` in this directory. It's a 5-node flow that lists emails and sends a Slack DM. Read it before authoring.

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
| FIL syntax error | `file.fil.ts:12:34: [parse] <message>` with the offending source line and a caret |
| stub UUID in `bindings.json` | `binding is a stub UUID (00000000-...). Replace with a real ConnectionId from \`uip is connections list\`.` |
| binding UUID not in your tenant | error names the binding + lists candidate real connection IDs for that connector |
| connector returned an error | full provider envelope preserved in `decisions.json` (e.g. Slack `channel_not_found`, Outlook `Id is malformed`, etc.) |
| node type not recognized | error names the node id and the missing connector library entry |

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

If you also need the input schema for a specific node (parameter names, required/optional, types), `uip maestro flow registry get <NodeType> --output json` returns the full Data.Node — `connectorMethodInfo.parameters[]` is the parameter list.

## Common authoring patterns

**Pattern: HTTP request (no connector).**

Use `core.action.http` for raw API calls — no connector needed. The node has 12+ verbose fields; copy this template and edit `url`/`method`/`body`. The `headers` and `queryParams` accept either an object literal in the manifest's `rawInputs`, or a JSON-stringified empty object inside the FIL `executeNode` argument.

FIL:

```typescript
await executeNode("fetchTemperature", JSON.stringify({
  mode: "manual",
  method: "GET",
  url: "https://api.open-meteo.com/v1/forecast?latitude=39.7392&longitude=-104.9903&current=temperature_2m&temperature_unit=fahrenheit",
  swaggerDefinition: null,
  authenticationType: "manual",
  application: "",
  connection: "",
  headers: JSON.stringify("{}"),
  queryParams: JSON.stringify("{}"),
  body: "",
  contentType: "application/json",
  timeout: "PT15M",
  retryCount: 0,
  branches: JSON.stringify("[]"),
}));
```

Manifest entry:

```json
"fetchTemperature": {
  "type": "core.action.http@1.0.0",
  "ui": { "position": { "x": 432, "y": 144 } },
  "label": "Fetch Denver Temperature",
  "rawInputs": {
    "mode": "manual",
    "method": "GET",
    "url": "https://api.open-meteo.com/v1/forecast?latitude=39.7392&longitude=-104.9903&current=temperature_2m&temperature_unit=fahrenheit",
    "swaggerDefinition": null,
    "authenticationType": "manual",
    "application": "",
    "connection": "",
    "headers": {},
    "queryParams": {},
    "body": "",
    "contentType": "application/json",
    "timeout": "PT15M",
    "retryCount": 0,
    "branches": []
  }
}
```

The HTTP node's response lands at `vars.<nodeId>.output.body.<…>`. Read it from a downstream script node (`return $vars.fetchTemperature.output.body.current.temperature_2m;`) or from another node's input expression.

**Pattern: script node — extract a value from a previous node's output.**

A `function __script_<id>(): json { return …; }` declared as a sync helper at the top of the FIL file becomes a `core.action.script` node when called via `__script_<id>()`. The body uses v1's `=js:` runtime — `$vars.X.output.Y` walks the previous node's output.

```typescript
function __script_extractTemperature(): json {
  return { temp: $vars.fetchTemperature.output.body.current.temperature_2m };
}

async function main(): Promise<void> {
  await executeNode("fetchTemperature", JSON.stringify({ /* … */ }));
  const tempData: json = __script_extractTemperature();
  if ((JSON.parse(tempData).temp > 60)) {
    /* … */
  }
}
```

Manifest entry for the script node (the converter assigns the id `extractTemperature` from `__script_extractTemperature`):

```json
"extractTemperature": {
  "type": "core.action.script@1.0.0",
  "ui": { "position": { "x": 600, "y": 144 } },
  "label": "Extract Temperature",
  "rawInputs": {
    "script": "return { temp: $vars.fetchTemperature.output.body.current.temperature_2m };"
  }
}
```

The `rawInputs.script` body MUST match the FIL helper's body verbatim — that's how the round-trip is preserved.

**Pattern: use a node's output downstream.**

```typescript
const emailsRaw = await executeNode("uipath.connector.uipath-microsoft-outlook365.get-email-list", "{}");
const emails = JSON.parse(emailsRaw);
const subject = (emails[0].subject ?? "(no subject)");
await executeNode("uipath.connector.uipath-salesforce-slack.send-message-to-channel",
  JSON.stringify({ channel: "#alerts", messageToSend: ("New: " + subject) }));
```

**Pattern: branch on a node's output.**

```typescript
const raw = await executeNode("getStatus", "{}");
const status = JSON.parse(raw);
if (status.healthy) {
  await executeNode("ok", "{}");
} else {
  await executeNode("alert", "{}");
}
```

**Pattern: variables that the user/runtime supplies.**

```typescript
async function main(customerId: string): Promise<void> {
  await executeNode("lookup", JSON.stringify({ id: customerId }));
}
```

`customerId` becomes a v1 flow `in` variable.

## Pitfalls

- The string passed to `executeNode("…", …)` is the manifest's NODE ID (the key in `manifest.nodes`). It can be a short id (`getEmails`) or the full nodeType (`uipath.connector.uipath-microsoft-outlook365.get-email-list`); whichever you choose must match the manifest key. The manifest entry's `type` field is always the full `<nodeType>@<version>`.
- Trigger and end nodes still need manifest entries — even if FIL doesn't mention them by name.
- Control-flow ids (`decision1`, `merge1`, `end1`, `setvar_<name>_1`) are generated by the converter; you don't author manifest entries for them. The converter handles their definitions automatically.
- Connector nodes need BOTH `binding` and `folderBinding` UUIDs, even when stubbed. Validation rejects connectors missing the FolderKey binding.
- Every entry in `bindings.json` needs an `id` field (any short string). Missing it triggers schema validation failure.
- `await` outside an async function won't parse; every async helper needs `Promise<void>` (or `Promise<json>`, etc.) as its return type.
- Mutating an object's field (`obj.foo = 1`) doesn't survive v1 conversion — replace with `obj = { ...obj, foo: 1 }`.

### Script-helper restrictions

`__script_<id>` sync helpers must be **single-expression value returners**. The body should be a single `return <expr>;` — typically with a ternary if you need conditional values:

```typescript
// ✓ OK
function __script_buildResult(): json {
  return ($vars.fetchWeather.output.body.current.temperature_2m > 60)
    ? ({ message: "nice day" })
    : ({ message: "bring a jacket" });
}

// ✗ Don't do this — if/else inside a script helper hits a known FIL emitter bug
function __script_buildResult(): json {
  if ($vars.fetchWeather.output.body.current.temperature_2m > 60) {
    return { message: "nice day" };
  } else {
    return { message: "bring a jacket" };
  }
}
```

For real branching, use FIL's `if`/`else` in `main()` between awaits — and put a separate `executeNode("...")` call (or a separate script helper) in each branch:

```typescript
async function main(): Promise<void> {
  await executeNode("fetchWeather", JSON.stringify({ /* … */ }));
  if (JSON.parse(<output>).temp > 60) {
    await executeNode("nice", "{}");      // each branch awaits something
  } else {
    await executeNode("jacket", "{}");
  }
}
```

A FIL `if`/`else` in `main()` whose branches contain *only assignments* (no awaits, no executeNode) currently also hits the emitter bug. If your branches don't await, collapse to a ternary in a single script helper.

### Non-deterministic calls must go through `executeNode`

`__script_<id>` helpers run on **every** FIL replay. If a script body returns a value that depends on `Math.random()`, `Date.now()`, an entropy source, or any environment state, replay drifts: each run gets a different value, downstream branches diverge, and the host's history no longer matches what the WASM emits.

```typescript
// ✗ Replay-broken: each replay rolls a different number
function __script_roll(): json {
  return { value: Math.floor(Math.random() * 6) + 1 };
}

async function main(): Promise<void> {
  const r: json = __script_roll();
}
```

```typescript
// ✓ Replay-safe: the host runs the script once, captures the result in
//   history, and subsequent replays read the captured value.
async function main(): Promise<void> {
  const rolledRaw: string = await executeNode("roll", "{}");
  // rolledRaw is preserved in .flow-run/history.yaml
}
```

In the manifest, the `roll` node is a `core.action.script@1.0.0` whose `rawInputs.script` body contains the non-deterministic expression:

```json
"roll": {
  "type": "core.action.script@1.0.0",
  "label": "Roll Die",
  "rawInputs": {
    "script": "return Math.floor(Math.random() * 6) + 1;"
  }
}
```

`DateTime.now()` (the FIL builtin) is the exception — it's host-imported and pinned for the run, so it's safe inside script helpers.

### Object literals with non-trivial values fail inside calls

A `JSON.stringify({...})` whose object literal contains a value that's a multi-instruction expression — a `Member` access (`item.id`), a function call, or a non-string identifier that needs conversion (`i32` → string) — emits malformed WAT and fails to compile. The literal is fine when its values are simple string literals or already-string identifiers.

```typescript
// ✗ Object literal value is a member access — fails
await executeNode("a", JSON.stringify({ id: item.id }));

// ✗ Object literal value is an i32 that needs conversion — fails
for (const item of items) {
  await executeNode("a", JSON.stringify({ x: item.id }));
}

// ✓ Pass the json field directly (it's already serialized)
for (const item of items) {
  await executeNode("a", JSON.stringify(item));
}

// ✓ Concat strings if you only need a few fields
for (const item of items) {
  await executeNode("a", "{\"id\":" + item.id + "}");
}

// ✓ Constant input when the value isn't required
for (const item of items) {
  await executeNode("a", "{}");
}
```

Object literals with simple string-valued properties (`{ id: "abc" }`) work fine anywhere.
