---
name: uipath-flow-v2
description: "UiPath Flow v2 — author flows as FIL (a TypeScript subset) plus a small bindings file. Inverse of `.flow` JSON: program logic AND per-node config in FIL, connection IDs and process resource bindings in bindings. Verify locally with `./verify.sh`; use `./convert.sh + uip maestro flow validate` only as a final compatibility check before deploy."
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# UiPath Flow v2

A v2 flow project is **two files**:

| File             | Role                                                                                                                              |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `<Name>.fil`     | FIL source — async TypeScript-subset describing the flow's logic, the flow identity, and the nodes (actions + triggers) it uses   |
| `bindings.json`  | Connection IDs (folder, connector, agent process keys) referenced by the FIL action/trigger declarations                          |

**Primary verifier:** `./verify.sh` parses the FIL, validates connector bindings against your tenant (via `uip is connections list`), validates process-resource bindings from `bindings.json`, validates inline Agent `rawInputs` shape, and executes supported node decisions. Connector calls go through `uip is resources execute`; Agent nodes are validated and dry-run only until live Agent dispatch is wired. It records every dispatch in `decisions.json` so you can see what was sent and what came back. Use it as the inner loop while authoring.

**Final compatibility check (deploy gate):** `./convert.sh <Name>` produces `<Name>.flow` (the v1 form), then `uip maestro flow validate <Name>.flow` cross-checks against the v1 schema. Run this before declaring the flow shippable, but don't depend on it for iteration — `./verify.sh`'s errors are far more actionable than `convert + validate`'s output.

## Authoring loop

1. Decide what the flow does. Sketch the node sequence on paper (or in a comment block).
2. Look up the connector node types you need with `uip maestro flow registry search "<keyword>"` — returns matching `NodeType + DisplayName + Description`. Filter to `NodeType` starting with `uipath.connector.` (other matches are agent-tool wrappers you don't want). See "Looking up connectors" below.
3. Write the FIL source (`<Name>.fil`):
   - A top-level `flow <kebab-id> { name: "…", version: "…" };` declaration (required).
   - One `trigger <name>: <type>;` declaration per entry point (usually one).
   - One `action <name>: <type> { … };` declaration per node you'll call.
   - An `async function main()` body that wires the actions together via `await executeNode(<name>, …)`.
4. Write `bindings.json`. Look up the real ConnectionId / FolderKey upfront — `verify.sh` refuses to dispatch while any binding is a stub UUID. **Use `--output-filter` (JMESPath, built into the uip CLI) — NOT a pipe to `jq` or hand-written Python.** It filters in-process, returns smaller output, and applies the expression directly to `.Data` (so write `[*]` or `[?…]`, not `Data[*]`):
   ```bash
   # All connections, projected to the fields you need
   uip is connections list --output-filter "[*].{Id:Id,ConnectorKey:ConnectorKey,FolderKey:FolderKey,Name:Name}"

   # One connector
   uip is connections list --output-filter "[?ConnectorKey=='uipath-microsoft-outlook365'].{Id:Id,FolderKey:FolderKey}"
   ```
   Each result has `Id` (the ConnectionId UUID), `ConnectorKey`, `FolderKey`, and `Name`. Match each `bindings.json` binding's `propertyAttribute` (`ConnectionId` or `FolderKey`) to the corresponding field. Stub UUIDs (`00000000-…`) survive `parse-fil.sh` and the static converter but verify.sh blocks them before dispatch, so wiring up reals on the first write saves a round trip.
5. **Verify with `./verify.sh`** (the inner authoring loop):
   - `./verify.sh` — parses FIL, resolves bindings, lists what would be dispatched. No connector calls. Surfaces compile errors with file:line:col, missing/stub bindings, unknown node types.
   - Replace stub UUIDs with real connection IDs from `verify.sh`'s "candidates for X:" hints (or from `uip is connections list` directly).
   - `./verify.sh --live` — runs supported live nodes for real. Each connector call goes through `uip is resources execute`; HTTP nodes via `fetch()`. Do not use `--live` for Agent-bearing flows yet; live Agent dispatch fails explicitly. After completion, read `.flow-run/decisions.json` to see exact inputs and outputs per node.
   - Iterate. Compile errors → fix the FIL. Connector failures → adjust the `inputs` you pass via `executeNode(...)` or the action's `rawInputs` (the failure envelope is preserved in `decisions.json`).
6. **Final compatibility check** (only when the flow is otherwise done):
   - `./convert.sh <Name>` → writes `<Name>.flow`.
   - `uip maestro flow validate <Name>.flow`.
   - `uip maestro flow tidy <Name>.flow` → auto-layout so the v1 file is openable in the designer.

## FIL — the language

FIL is a parseable subset of TypeScript. Anything that parses as TypeScript and uses only the supported features compiles. **No imports, no classes, no first-class functions, no closures.**

### Top-level declarations

Every FIL program has three kinds of top-level entries:

```typescript
// (1) Flow identity — required. The identifier is the kebab-case flow id.
flow order-notify {
  name: "Order Notify",
  version: "1.0.0",
};

// (2) Triggers — at least one per flow.
trigger manualStart: start;            // short alias for core.trigger.manual

// (3) Actions — one per node you intend to invoke. Body fields are optional;
//     every field below is per-instance v1 node metadata.
action getEmails: uipath.connector.uipath-microsoft-outlook365.get-email-list@1.0.0 {
  binding: "bOutlook",         // ConnectionId — refers to a bindings.json entry
  folderBinding: "bFolderKey", // FolderKey — same
  label: "Get Emails",         // optional display label
};

action notifyTeam: uipath.connector.uipath-salesforce-slack.send-message-to-channel@1.0.0 {
  binding: "bSlack",
  folderBinding: "bFolderKey",
  rawInputs: { send_as: "bot" },   // static defaults; merged with the executeNode call's input
};

async function main(): Promise<void> {
  // …
}
```

### Type aliases for common node types

Short aliases are accepted in `action` and `trigger` type slots and expand to the canonical `core.*` type:

| Alias       | Canonical type             | Notes                                                        |
| ----------- | -------------------------- | ------------------------------------------------------------ |
| `start`     | `core.trigger.manual`      | Manual entry-point                                            |
| `http`      | `core.action.http`         | Raw HTTP — no connector needed                                |
| `script`    | `core.action.script`       | JavaScript snippet evaluated in the v1 sandbox                |
| `transform` | `core.action.transform`    | Transform/jq-style operations                                 |
| `decision`  | `core.logic.decision`      | Usually auto-generated from FIL `if`/`else`                   |
| `merge`     | `core.logic.merge`         | Auto-generated                                                |
| `loop`      | `core.logic.loop`          | Usually auto-generated from FIL `for`/`while`                 |
| `delay`     | `core.logic.delay`         | Usually auto-generated from `await executeTimer(…)`           |
| `end`       | `core.control.end`         | Usually auto-generated from `return`                          |
| `mock`      | `core.logic.mock`          | Test fixture                                                  |
| `subflow`   | `core.subflow`             | Subflow reference                                             |

Fully-qualified node types pass through verbatim (e.g. `uipath.connector.uipath-microsoft-outlook365.send-email@1.0.0`). The `@<version>` suffix is optional; the converter resolves to a matching library entry.

### Action body fields

The `{ … }` block on an `action` or `trigger` declaration accepts these fields, all optional:

| Field                  | Description                                                                                                                |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `id`                   | Override the v1 node id (e.g. `id: "send-email"` lets you use a non-JS identifier as the protocol id). Default: the FIL identifier. |
| `label`                | Display name; defaults to the canonical-library label for integration nodes                                                |
| `binding`              | Symbolic id of a `bindings.json` entry whose `propertyAttribute` is `ConnectionId`. Required for connector nodes.          |
| `folderBinding`        | Symbolic id of a `bindings.json` entry whose `propertyAttribute` is `FolderKey`. Required for connector nodes.             |
| `resource`             | Process-resource metadata for published Agent/API/RPA-style nodes                                                          |
| `resourceBindings`     | Maps process-resource properties such as `name` and `folderPath` to `bindings.json` ids                                    |
| `rawInputs`            | Static defaults baked into the v1 node's `inputs`. Merged with the `executeNode` call's input at runtime; call wins on key collisions. |
| `inputs`               | Top-level inputs that aren't in `rawInputs` (rare; mostly for legacy round-trips)                                          |
| `configuration`        | Distilled `inputs.detail.configuration` payload for integration nodes (per-instance field values)                           |
| `configurationExtras`  | Catch-all for fields that diverge from canonical-library defaults                                                          |
| `outputs`              | Override the library's default output schema (the v1 node's output-port → flow-variable bindings — NOT the value the node returns at runtime) |
| `fixture`              | Dry-run value returned by fixture-aware nodes such as `core.logic.mock`, published Agents, inline Agents, and HITL quickform. Any JSON shape is allowed. |

Fields are constant-folded — literals, nested objects/arrays, and untagged template literals survive into the runtime manifest. Identifiers or arithmetic in a body field are dropped (use the runtime path via `executeNode` arguments for computed values).

## OOTB Non-IS actions: HITL quickform

HITL quickform is an OOTB node, not a connector. It does not use
`binding`/`folderBinding`; put the quickform metadata directly in the action
body and include a `fixture` for dry-run verification:

```typescript
action reviewExpense: uipath.human-in-the-loop@1.0 {
  label: "Review Expense",
  rawInputs: {
    type: "quick",
    schema: {
      schemaId: "11111111-2222-3333-4444-555555555555",
      fields: [
        { id: "amount", label: "Amount", type: "number", direction: "input", binding: "vars.manualStart.output.amount" },
        { id: "approved", label: "Approved", type: "boolean", direction: "output", variable: "vars.approved" },
        { id: "comments", label: "Comments", type: "text", direction: "output", variable: "vars.comments" },
      ],
      outcomes: [
        { id: "approve", name: "Approve", type: "string", isPrimary: true, action: "Continue" },
        { id: "reject", name: "Reject", type: "string", isPrimary: false, action: "Continue" },
      ],
    },
    recipient: { channels: ["ActionCenter"], connections: {}, assignee: { type: "group" } },
    priority: "Low",
  },
  outputs: {
    output: {
      type: "object",
      source: "=result",
      var: "output",
      properties: {
        approved: { type: "boolean" },
        comments: { type: "string" },
        Action: { type: "string", enum: ["Approve", "Reject"], default: "Approve" },
      },
    },
    status: { type: "string", source: "=result.Action", var: "status" },
  },
  fixture: { approved: true, comments: "Looks good", Action: "Approve" },
};
```

`./verify.sh` supports HITL quickform in dry-run only. Live Action Center
dispatch must use the product debug/runtime path.

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

Reading a field off a parsed `json` value returns `json` (the field's runtime type isn't known at compile time). To pull it into a concrete type, declare the target's type and let the compiler insert a runtime coercion:

```typescript
const parsed: json = JSON.parse(rawResponse);
const tag: string = parsed.tag_name;    // auto-coerced from json → string
const count: string = parsed.count;     // works for numbers, bools, null, etc.
const n: i32 = parsed.count;            // json → i32 also auto-coerces (truncates)
```

The same coercion is also available as the explicit `"" + jsonExpr` form (older code uses it; both compile to the same runtime call). Inside a function-argument position where there's no annotation target — e.g. `JSON.parse(response.body)` where `response.body` is `json` — keep the explicit `"" +` form (or hoist into a typed local first). `f64` works the same way: `const t: f64 = parsed.temperature` coerces a json number to a float.

Do **not** use `as string` for json fields — it's a compile-time-only assertion and emits no conversion, so non-string tags produce garbage at runtime.

`const` vs `let` is a style choice. `const` is the idiomatic default for derived values; `let` is fine too, even for values you never reassign. Both end up the same in the v1 form when never mutated.

### Functions and the `main` entry

Every flow has exactly one `main`:

```typescript
async function main(): Promise<void> {
  // …
}
```

Helper async functions become **subflows** when converted to v1. Helper sync functions named `__script_<id>` become **script** nodes.

### Calling a node

`executeNode(<actionRef>, inputJson)` runs a flow node. The first argument is the **identifier** of a declared `action`, not a string. Always `await` it.

```typescript
action getEmails: uipath.connector.uipath-microsoft-outlook365.get-email-list@1.0.0 {
  binding: "bOutlook",
  folderBinding: "bFolderKey",
};

async function main(): Promise<void> {
  const raw: string = await executeNode(getEmails, JSON.stringify({
    parentFolderId: "inbox",
    unReadOnly: true,
  }));
  const emails: json = JSON.parse(raw);
  // …
}
```

The compiler verifies the identifier resolves to a declared action. String-literal node names are rejected — typos turn into compile errors instead of silent runtime failures.

### Control flow

```typescript
if (priority === "urgent") {
  await executeNode(notifyTeam, "{}");
} else {
  await executeNode(logSilently, "{}");
}

await executeNode(stepOne, "{}");
await executeNode(stepTwo, "{}");

for (const item of items) {
  await executeNode(process, JSON.stringify(item));
}
```

### Timers and SLA-style waits

```typescript
await executeTimer(TimeSpan.fromMinutes(15n));

const idx = await Promise.any([
  executeNode(longRunning, "{}"),
  executeTimer(DateTime.now().add(TimeSpan.fromHours(1n))),
]);
if (idx === 1) {
  await executeNode(notifyTimeout, "{}");
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

## Process-resource actions: published and inline Agents

Published and inline Agent nodes are declared directly in FIL action bodies. Do not create a `<Name>.manifest.flow` sidecar and do not add `embeddedDefinitions`; `v2-to-v1` synthesizes the v1 definitions from the action metadata.

For **published/in-solution Agent nodes** (`uipath.core.agent.<key>@1.0.0`), declare the Agent as an action, call the action identifier, and put process-resource metadata in `resource` / `resourceBindings`:

```typescript
action classifyIntent: uipath.core.agent.93f09b44-e635-40f9-8cab-44ca29e748ed@1.0.0 {
  label: "Classify Intent",
  resource: {
    resource: "process",
    resourceSubType: "Agent",
    resourceKey: "93f09b44-e635-40f9-8cab-44ca29e748ed",
    orchestratorType: "agent",
    serviceType: "Orchestrator.StartAgentJob",
    section: "Published",
  },
  resourceBindings: {
    name: "bAgentName",
    folderPath: "bAgentFolder",
  },
  fixture: { response: { intent: "billing" } },
};

async function main(): Promise<void> {
  const raw: string = await executeNode(classifyIntent, JSON.stringify({ in_text: "Customer asked for pricing" }));
  const result: json = JSON.parse(raw);
  const response: json = result.response;
  const intent: string = response.intent;
  // branch on intent
}
```

`fixture` is optional, but recommended for eval tasks and local dry-runs. `./verify.sh` returns it as the Agent output because it runs `flow-run --dry-run` by default. Live `flow-run` rejects published Agent dispatch for now because the supported direct `Orchestrator.StartAgentJob` CLI/API path is not confirmed.

For **inline low-code Agent nodes** (`uipath.agent.autonomous@1.0`), keep the existing CLI-compatible sidecar layout next to the v2 files: `<source>/agent.json` plus any inline-agent `resources/` or `features/` folders. The action body keeps `source` and the validator mirror fields under `rawInputs`; it does not use published-Agent `resourceBindings`:

```typescript
action draftReplyAgent: uipath.agent.autonomous@1.0 {
  label: "Draft Reply Agent",
  rawInputs: {
    source: "11111111-2222-3333-4444-555555555555",
    systemPrompt: "You write concise customer email replies.",
    userPrompt: "Draft a reply for the provided email.",
    model: "gpt-4o-2024-11-20",
    agentInputVariables: [
      { id: "emailText", type: "string", binding: "=js:$vars.manualTrigger1.output.emailText" },
    ],
    agentOutputVariables: [
      { id: "content", type: "string" },
    ],
  },
  fixture: { response: { content: "Draft reply text" } },
};
```

`./verify.sh` validates `rawInputs.source`, prompt/model placeholders, and input/output variable arrays, then returns `fixture`. Live inline Agent dispatch is intentionally unsupported.

## Bindings — `bindings.json`

`bindings.json` lists the connection-related UUIDs and process resource values the FIL's `binding`, `folderBinding`, and `resourceBindings` fields point at. Validation requires:

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
      "id": "bOutlook",
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

The FIL action declarations reference the symbolic ids (`"bOutlook"`, `"bFolderKey"`); `verify.sh` / `convert.sh` resolve them to the real UUIDs at load time.

**Stub UUIDs vs real ones.** `parse-fil.sh` and the v2-to-v1 static converter don't care whether a UUID resolves — they just want structural well-formedness. **`verify.sh` (flow-run) DOES care** and refuses to dispatch with stubs. The deploy-gate `uip maestro flow validate` also doesn't dispatch and accepts stubs. So stubs are a write-time scaffolding tool only; before running `verify.sh`, fill them in with `uip is connections list --output-filter "[*].{Id:Id,ConnectorKey:ConnectorKey,FolderKey:FolderKey,Name:Name}"` (`--output-filter` is JMESPath applied directly to `.Data` — write `[*]` / `[?…]`, not `Data[*]`).

For connector nodes, the FIL action body never holds real connection UUIDs — that's exclusively `bindings.json`. If `verify.sh` reports `binding "bOutlook" is not declared in bindings.json`, you have a typo or missing entry in `bindings.json`. Agent `resource.resourceKey` is a process resource key and intentionally stays in the action body so v2 can rebuild the v1 Agent model.

Published Agent bindings are process resource bindings, not connector bindings. They must use `resource: "process"`, `resourceSubType: "Agent"`, and `propertyAttribute` values `name` and `folderPath`:

```json
{
  "id": "bAgentName",
  "name": "name",
  "type": "string",
  "resource": "process",
  "resourceKey": "93f09b44-e635-40f9-8cab-44ca29e748ed",
  "default": "ClassifyIntent",
  "propertyAttribute": "name",
  "resourceSubType": "Agent"
}
```

`v2-to-v1` ships a built-in catalog of v1 `definitions[]` entries for the standard control-flow types (`core.trigger.manual`, `core.control.end`, `core.logic.decision`, `core.logic.merge`, `core.action.script`, `core.subflow`, …). You don't need to ship `embeddedDefinitions` for these — the converter fills them in. Connector definitions come from the canonical library cache. Published Agent definitions are synthesized from `resource` / `resourceBindings`; inline Agent definitions are synthesized from the `uipath.agent.autonomous` node shape. Embedded Agent definitions from a v1 conversion are still preserved and take precedence. Add `embeddedDefinitions` only for custom node types the converter cannot synthesize.

## Worked example

| File | What it shows |
| --- | --- |
| `example/OrderConfirm.fil` | Minimal 2-connector flow: list emails, branch on count, send a Slack DM. Good for first reading. |
| `example/CommitMonitor.fil` | **Multi-connector + HTTP + loop + decision + counter + digest.** Closely mirrors the structure of most real flows: hardcoded array, for-of, HTTP fetch with string-concat URL, JSON.parse double-pattern for the HTTP response, string-contains decision, two connector calls in different branches, final digest email with `"count: " + i32` string-concat. Adapt this when you need any of these patterns. |
| `example/bindings.json` | Stub bindings the example FIL references. |

## Verifying with `./verify.sh`

`./verify.sh` is the primary local verifier. It does what convert+validate can't: catches FIL compile errors with line numbers, validates bindings against the connections actually in your tenant, validates process-resource bindings, and executes supported connector/HTTP calls so you see real success/failure.

```bash
./verify.sh          # dry-run: parses, verifies, no connector calls
./verify.sh --live   # actually executes nodes against your tenant
```

Outputs (under `.flow-run/`, gitignored automatically):
- `history.yaml` — replay log; consumed by a re-run with `--resume`.
- `decisions.json` — one record per dispatched node, with parsed input + output JSON, the connector key + verb + object name, and dispatch timing. **This is what you read to debug.**

What verify.sh catches that convert+validate doesn't:

| Symptom | output |
| --- | --- |
| FIL syntax error | `file.fil:12:34: [parse] <message>` with the offending source line and a caret |
| stub UUID in `bindings.json` | `binding is a stub UUID (00000000-...). Replace with a real ConnectionId from \`uip is connections list\`.` |
| binding UUID not in your tenant | error names the binding + lists candidate real connection IDs for that connector |
| connector returned an error | full provider envelope preserved in `decisions.json` (e.g. Slack `channel_not_found`, Outlook `Id is malformed`, etc.) |
| node type not recognized | error names the node id and the missing connector library entry |

verify.sh dispatches:

- **Connector nodes** (CRUD: `Retrieve`/`Create`/`Update`/`Delete`/`Replace`/`List`) via `uip is resources execute`.
- **HTTP nodes** (`core.action.http`, `core.action.http.v2`) via Node `fetch()`. Output: `{ body, headers, statusCode }`.
- **Mock nodes** (`core.logic.mock`) — return the action's `fixture` body field, or `{}` if unconfigured. Example: `action fetchUser: mock { fixture: { id: 1, name: "Alice" } };`
- **Published Agent nodes** (`uipath.core.agent.<key>`) — validate `process`/`Agent` resource bindings and return the action's `fixture` in dry-run. Live Agent dispatch is intentionally unsupported for now.
- **Inline Agent nodes** (`uipath.agent.autonomous`) — validate `rawInputs.source`, prompt/model placeholders, and variable arrays; return the action's `fixture` in dry-run. Keep `<source>/agent.json` beside the v2 files for conversion/package compatibility.
- **Timer decisions** (`executeTimer`) — record the deadline immediately. Pass `--real-time` to actually sleep.
- **Promise.all** — every entry dispatched concurrently, all must complete; results recorded in entry order.
- **Promise.any** (race) — entries dispatched concurrently, first to resolve wins.

Not yet supported: the 21 connector activities whose `operation.name` is `Download` or `Upload`, and live published/inline Agent dispatch. verify.sh aborts on these with a specific message and points you at `convert.sh + uip maestro flow validate` or the Flow/Studio Web debug path until support lands.

## Final compatibility check (deploy gate)

After verify.sh is happy, run the v1 converter + validator + tidy to confirm the flow round-trips into the v1 schema cleanly and is openable in the designer:

```bash
./convert.sh OrderNotify                       # writes OrderNotify.flow
uip maestro flow validate OrderNotify.flow     # final compatibility check
uip maestro flow tidy OrderNotify.flow         # auto-layout for the designer view
```

Conversion produces stdout like:

```
Wrote OrderNotify.flow
Definitions: 5
Nodes: 7
Edges: 6
```

Validation prints `"Result": "Success"` on a clean flow, or `"Found N error(s)"` with the offending node id and the structural problem. **Don't iterate against this — iterate against `verify.sh` first; this step is a final gate.**

## Looking up connectors

`uip maestro flow registry search "<keyword>"` is the way to discover available connectors. It searches `NodeType + Category + Tags + DisplayName + Description` and returns matching nodes. The registry is pre-pulled — you don't need to run `pull`.

```bash
uip maestro flow registry search "send email" --output json \
  | jq '.Data[] | select(.NodeType | startswith("uipath.connector.")) | {NodeType, DisplayName, Description}'
```

If you also need the input schema for a specific node, `uip maestro flow registry get <NodeType> --output json` returns the full `Data.Node` — `connectorMethodInfo.parameters[]` is the parameter list.

## Common authoring patterns

**Pattern: HTTP request (no connector).**

Use `http` (alias for `core.action.http`) for raw API calls — no connector needed. Pass the URL via either `rawInputs` (static) or the `executeNode` call's input (dynamic). The two merge at dispatch with call-side values winning on collision.

```typescript
action fetchTemperature: http {
  rawInputs: {
    method: "GET",
    timeout: "PT15M",
  },
};

async function main(): Promise<void> {
  const raw: string = await executeNode(fetchTemperature, JSON.stringify({
    url: "https://api.open-meteo.com/v1/forecast?latitude=39.7392&longitude=-104.9903&current=temperature_2m&temperature_unit=fahrenheit",
  }));
  const response: json = JSON.parse(raw);
  const bodyText: string = response.body;
  const data: json = JSON.parse(bodyText);
  // …
}
```

The HTTP response shape is `{ body: <json-text>, headers: { … }, statusCode: <number> }`. Body is JSON-text — double-parse if you want field access.

**Pattern: script node — non-deterministic, replay-safe.**

Declare an `action` whose `rawInputs.script` holds the body. The host runs it once, captures the result in history, and subsequent replays read the captured value.

```typescript
action roll: script {
  label: "Roll Die",
  rawInputs: { script: "return Math.floor(Math.random() * 6) + 1;" },
};

async function main(): Promise<void> {
  const rolledRaw: string = await executeNode(roll, "{}");
  // rolledRaw is preserved in .flow-run/history.yaml
}
```

**Script-action calling convention.** The body of a `script` action invoked via `executeNode` sees exactly one variable: **`$vars`** (dollar-prefixed), a snapshot of every prior node's output keyed by node id — same shape as v1's `=js:` runtime. The `executeNode(name, inputJson)` second argument is *ignored* for script actions today; pass `"{}"` and have the script read what it needs from `$vars.<priorNodeId>.output.<...>`. Neither `inputs.X` nor bare `X` are defined — use `$vars` only.

```typescript
action fetchWeather: http { rawInputs: { method: "GET", url: "https://api.open-meteo.com/v1/forecast?…" } };

action formatSummary: script {
  rawInputs: {
    // Read the prior fetchWeather node's output directly via $vars.
    script: "const t = $vars.fetchWeather.output.body.current.temperature_2m; return t > 60 ? { message: 'nice day' } : { message: 'bring a jacket' };",
  },
};

async function main(): Promise<void> {
  await executeNode(fetchWeather, "{}");
  const resultRaw: string = await executeNode(formatSummary, "{}");   // "{}" — input ignored
}
```

For deterministic per-replay scripts (extracting fields from a previous node's output), use a sync helper `function __script_<id>(): json { return <expr>; }` — it inlines into the WASM as a `core.action.script` node automatically.

**Pattern: branch on a node's output.**

```typescript
const raw: string = await executeNode(getStatus, "{}");
const status: json = JSON.parse(raw);
if (status.healthy) {
  await executeNode(ok, "{}");
} else {
  await executeNode(alert, "{}");
}
```

**Pattern: variables that the user/runtime supplies.**

```typescript
async function main(customerId: string): Promise<void> {
  await executeNode(lookup, JSON.stringify({ id: customerId }));
}
```

`customerId` becomes a v1 flow `in` variable.

## Pitfalls

### Authoring

- The first argument to `executeNode(…)` is an **action identifier**, not a string. Add an `action <name>: <type>;` declaration for it. String-literal first-args are a typecheck error.
- The action's identifier must be a valid JS identifier (camelCase). Use the `id` field in the body if you need a non-identifier node id at the protocol layer: `action sendMail: … { id: "send-mail" };`.
- Trigger nodes live in a top-level `trigger <name>: <type>;` declaration. End nodes are auto-emitted from FIL `return` statements; you don't declare them.
- Control-flow ids (`decision1`, `merge1`, `end1`, `setvar_<name>_1`) are generated by the converter; you don't declare them.
- Connector actions need BOTH `binding` and `folderBinding`, even when stubbed. Validation rejects connectors missing the FolderKey binding.
- Every entry in `bindings.json` needs an `id` field. Missing it triggers schema validation failure.
- `await` outside an async function won't parse; every async helper needs `Promise<void>` (or `Promise<json>`, etc.) as its return type.
- Mutating an object's field (`obj.foo = 1`) doesn't survive v1 conversion — replace with `obj = { ...obj, foo: 1 }`.

### Non-determinism in `__script_<id>` helpers is a typechecker error

`__script_<id>` sync helpers run on **every** FIL replay, so referencing `Math.random()`, `Date.now()`, or other entropy sources inside one would silently drift on replay. The typechecker rejects these calls with a clear error message. The fix is to declare an `action … : script { rawInputs: { script: "<body>" } };` and dispatch through `executeNode` — the host runs it once per real execution and captures the result in history, so replays read the recorded value. `DateTime.now()` (the FIL builtin) is the exception — it's host-imported and pinned for the run, so it's safe inside script helpers.

### If `verify.sh` reports a `WebAssembly.compile()` error in compiler-generated `$__*` locals

This means a FIL emitter bug, not a logic error in your flow. All known cases were fixed; if you hit a new one, the wat output is in `.flow-run/` and the error message names the failing local. Surface the FIL + the error rather than thrashing on rewrites — the bug won't be cleared by reshaping the surrounding code.
