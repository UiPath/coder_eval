# Building `[WorkflowTest]` cases for a C# Flow workflow

Read this file when you need to **author tests** for a workflow you just
wrote, beyond the smoke test the template scaffolds. The scaffolded
`EmptyTriggerRunsClean` only checks "did the workflow run without
erroring" — meaningful regression coverage requires asserting that the
workflow took the right paths and dispatched the right nodes.

The C# Flow runtime is **replay-driven**: every connector call's output
is recorded into a `history.yaml` file, and every subsequent run replays
those recorded outputs back into the workflow instead of dispatching
live. So a test fixture is just a hand-authored `history.yaml` that
pins one path through the workflow; the test runs against that history
and the assertion checks the resulting decisions.

This guide walks you through the **peek-next loop** — the supported way
to incrementally build one history fixture, then pin it into a
`[WorkflowTest]`.

## The loop

```
                  ┌────────────────────────────────────────┐
                  │   csflowrun build → flow-run --peek-next   │
                  └────────────┬───────────────────────────┘
                               │
                               ▼
                  ┌─────────────────────────────┐
                  │ "pending: <node N, input X>" │ ◄──┐
                  └────────────┬────────────────┘    │
                               │                     │
                               ▼                     │
              ┌────────────────────────────────┐     │
              │ author realistic output for N  │     │
              │ append `node: N\n  output: …` │     │
              │ to history.yaml                │     │
              └────────────┬───────────────────┘     │
                           │                         │
                           ▼                         │
                  ┌─────────────────────┐            │
                  │ flow-run --peek-next │ ───────────┘
                  │       --resume       │
                  └────────┬────────────┘
                           │
                  ┌────────▼────────┐
                  │ "completed"     │
                  └─────────────────┘
                           │
                           ▼
              ┌────────────────────────────────┐
              │ history.yaml is the path fixture│
              │ pin it via [WorkflowTest].History│
              └────────────────────────────────┘
```

Each pass through the loop adds ONE decision to the fixture. When
`peek-next` reports `{state: completed}`, the fixture covers a full
path. Each *distinct* path through the workflow (different branch in an
`if`, different value from a `switch`, etc.) is its own fixture.

## Step 1 — get the next pending decision

From the project root (where `appsettings.json` lives):

```bash
# Build first; csflowrun reads the FIL out of the assembly's embedded PDB.
dotnet build

# Then peek. Output is JSON on stdout, log noise on stderr.
node "$FLOW_V2_FLOW_RUN" "$(find . -name "$(basename "$PWD").dll" | head -1 | xargs dirname)" \
  --library "$FLOW_V2_LIBRARY_JSON" \
  --peek-next --skip-connection-check 2>/dev/null
```

Output shape:

```json
{
  "state": "pending",
  "history": "/path/to/.flow-run/history.yaml",
  "eventCount": 0,
  "pending": {
    "kind": "executeNode",
    "nodeId": "getEmails",
    "nodeType": "uipath.connector.uipath-microsoft-outlook365.get-email-list",
    "input": "{}"
  }
}
```

The `pending` block tells you exactly what the workflow wants to do
next: `nodeId` is the `[FlowAction]` method name; `nodeType` is the
versioned typeRef; `input` is the JSON the workflow is sending.

`{state: completed}` means the workflow ran to completion against the
existing history — you're done with this path, jump to Step 4.

`{state: error, error: "..."}` means the workflow trapped (e.g. the
authored history fed back a shape the workflow code couldn't handle).
Adjust the history and retry.

## Step 2 — figure out the realistic output shape

Three sources, in order of preference:

1. **Read the workflow's own access pattern.** If `Execute` does
   `result.RootElement.GetProperty("subject").GetString()`, the output
   needs a top-level `subject` string. This is fastest and most
   reliable — the workflow tells you exactly which fields it reads.

2. **Check the connector library entry.** The high-level shape lives
   at `$FLOW_V2_LIBRARY_JSON/<connectorKey>/<operation>@<version>.json`,
   under `.outputDefinition.output.type` (`object` / `array` / `file`).
   Most entries don't carry a full response schema, so use this only
   for "should it be an array vs an object."

3. **Run it live once.** When the workflow uses a less obvious shape,
   the surest source is a real dispatch:
   ```bash
   uip is resources execute list uipath-microsoft-outlook365 ListEmails \
     --connection-id <real-id> --output json
   ```
   Copy the response into history.yaml as-is.

## Step 3 — append the output to history.yaml

`history.yaml` is plain text with this shape:

```yaml
# flow-run history
runId: agent-authored
flowId: <flow id from the FIL>
flowName: <flow name>
startedAt: 2026-06-05T00:00:00.000Z
# events follow — one per dispatched/handled decision
node: <nodeId>
  output: <minified JSON on one line>
node: <nodeId2>
  output: ...
```

Notes:
- The leading metadata block (`runId` / `flowId` / `flowName` /
  `startedAt`) is required but the values aren't load-bearing — any
  string is fine.
- Each `node:` entry consumes the workflow's next `executeNode` call
  whose `nodeId` matches. The `output:` is what gets replayed back to
  the workflow as that call's result.
- Output must be **minified** (single line). Multi-line JSON is a
  parse error.
- For `Promise.all` / `Promise.any` (Task.WhenAll / Task.WhenAny in
  C#), use `all: <N>` / `race: <N>` markers followed by N `node:`
  entries. See `flow-run/tests/fixtures/` for examples.

Re-run `--peek-next` after each append. The `eventCount` in the output
should grow by 1 each iteration.

## Step 4 — pin the fixture in a `[WorkflowTest]`

When you've built a complete fixture (state: completed), save it to
the project — convention is `fixtures/<scenario>.history.yaml`. Then
point a `[WorkflowTest]` at it:

```csharp
[WorkflowTest]
public static WorkflowTest UnreadEmailPostsToOrders() => new()
{
    History = "fixtures/unread-email.history.yaml",
    Assert = r => {
        // r.Decisions is the decisions JsonDocument; walk to confirm
        // the workflow took the expected branch.
        var arr = r.Decisions.RootElement.GetProperty("decisions");
        foreach (var d in arr.EnumerateArray())
        {
            if (d.GetProperty("nodeId").GetString() == "notifyOrders") return true;
        }
        return false;
    },
    AssertMessage = "expected notifyOrders dispatch on the unread-email path",
};
```

`csflowrun` resolves `History` relative to the project root, copies it
into the per-test temp dir, and tells `flow-run` to `--resume` from
it. The workflow runs as if the fixture's decisions had really
happened.

## Branch coverage

When the workflow has a conditional, each branch needs its own
fixture. Example for an inbox check:

| Fixture | First `getEmails` response | Expected end state |
|---|---|---|
| `unread-email.history.yaml` | `[{ subject: "Order #42", … }]` | dispatched `notifyOrders` |
| `empty-inbox.history.yaml`  | `[]`                            | completed without dispatch |

Author the empty-inbox path the same way: run `--peek-next` with an
empty history → first decision is `getEmails` → write
`node: getEmails\n  output: []` → `--peek-next` should now report
`{state: completed}`. That's the entire fixture.

A `[WorkflowTest]` per fixture, one assertion per scenario.

## Common pitfalls

- **Output shape mismatch** → workflow indexes into a field that
  doesn't exist → WASM trap. `--peek-next` reports
  `{state: error, error: "memory access out of bounds …"}`. Fix the
  fixture's most recent `output:` block.
- **JSON not minified** → `failed to parse history file …`. Run
  `jq -c .` on your output JSON before pasting.
- **Two fixtures for "the same path"** → consolidate. If two fixtures
  produce identical `decisions` arrays, the second one isn't earning
  its keep.
- **Live response copied verbatim with sensitive UUIDs** → scrub
  connection IDs and tokens before committing the fixture.
