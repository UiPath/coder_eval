---
name: uipath-flow-csharp
description: "UiPath Flow workflow as a C# project. Author in C# with full IDE/Roslyn-analyzer feedback, build and verify locally with csflowrun (cs2fil + flow-run --dry-run), then convert directly to a Flow v1 project (`<Name>.flow` + `bindings.json`) for the deploy gate."
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# UiPath C# Flow workflow

A C# Flow workflow project is a normal `.NET` console app whose `Workflow.cs`
follows a strict subset of C#. Roslyn analyzers (CS2FIL001 onward) flag every
incompatibility at edit time — your inner loop is `dotnet build` and
`./verify.sh <Name>`.

Dispatch goes through the **action model**: every connector call is
declared as a `[FlowAction]` method that spreads the connector's
`Action()` template, and you invoke it via
`Flow.ExecuteNode(connector, methodName(), inputJson)`. The legacy
`SomeConnector.Execute(connector, ...)` wrapper no longer exists.

## Four phases

```
1. Scaffold        dotnet new csflow --name <Name>
2. Author + test   edit Workflow.cs (analyzers gate); dotnet build; ./verify.sh <Name>
3. Convert to v1   ./convert-to-v1.sh <Name>     → <Name>.flow + bindings.json
4. Validate        ./validate.sh <Name>
```

The `csflow` template is pre-installed in this environment — go straight to
phase 1.

Intermediate build artifacts live under `<Name>/.cs2v1/` during conversion
and are not part of your mental model — you don't manipulate them. The C#
project is the source of truth; the final `<Name>.flow` + `bindings.json`
are the deployable output.

## Phase 1 — scaffold

```bash
dotnet new csflow --name CalendarReminder
cd CalendarReminder
ls
# .nuget/  appsettings.json  CalendarReminder.csproj  nuget.config
# Options.cs  Program.cs  TestConnector.cs  Workflow.cs  agents.md
```

The scaffolded project is **self-contained** — its own `.nuget/` directory
ships the analyzer plus the activity harness plus 1209 connector classes.
`dotnet restore` resolves everything offline.

Read the generated `agents.md` — it's the per-project authoring guide.

## Phase 2 — author + test

The `Workflow` class shape:

```csharp
[Workflow]
public class Workflow(IConnector connector)
{
    [FlowAction]
    internal FlowAction sendEmail() => SendEmail.Action() with
    {
        binding = "outlook",      // matches an `id` in appsettings Bindings
    };

    public async Task Execute()
    {
        // Body translates to a UiPath Flow workflow. Analyzers reject anything
        // outside the supported C# subset.
    }
}
```

### Calling a connector

```csharp
// Capture the response as a JsonDocument for field access.
using JsonDocument result = await Flow.ExecuteNode(connector, sendEmail(),
    """{ "toRecipients": "alice@example.com", "subject": "Hello", "body": "Hi" }""");
string status = result.RootElement.GetProperty("status").GetString()!;

// Fire-and-forget — discard the raw string response.
_ = await Flow.ExecuteNodeRaw(connector, sendEmail(),
    """{ "toRecipients": "alice@example.com", "subject": "Hello", "body": "Hi" }""");
```

The `[FlowAction]` method name (here `sendEmail`) doubles as the per-call
`nodeId` in the produced flow. Each `[FlowAction]` method must be called
**exactly once** in the workflow body (CS2FIL022) — declare one per call
site.

For typed input construction, declare the record and call
`JsonSerializer.Serialize(...)` — the converter renders it as a JSON object
keyed by `JsonPropertyName`:

```csharp
SendEmail.Inputs inputs = new(
    toRecipients: "alice@example.com",
    subject: "Hello",
    body: "Hi");
_ = await Flow.ExecuteNodeRaw(connector, sendEmail(), JsonSerializer.Serialize(inputs));
```

#### When the generated `Inputs` record is too thin

Some real connectors carry more JSON fields than the generated record
exposes. For example, `AtlassianJira.CreateIssue.Inputs` only has
`fields.project.key` and `fields.issuetype.id` — but real Jira issue
creation needs summary, description, and assignee. In that case, drop
the typed path and call the raw connector method with hand-built JSON,
using the connector's `NodeType` constant:

```csharp
using UiPath.Connectors.AtlassianJira;

string issueJson = $$"""
{
  "fields": {
    "project":   { "key": "MAESTRO" },
    "issuetype": { "id": "10004" },
    "summary":     "{{summary}}",
    "description": "{{description}}",
    "assignee":    { "accountId": "{{assigneeId}}" }
  }
}
""";
string raw = await connector.ExecuteNodeAsync(
    CreateIssue.NodeType, "createIssue1", "<bJira>", issueJson);
```

The `Flow.ExecuteNode(connector, action(), …)` route is preferred when
the typed shape covers what you need. Fall back to
`connector.ExecuteNodeAsync` only when it doesn't.

The same method also dispatches the **synthetic node types** that have no
generated wrapper class — `core.action.http`, `core.action.script`, and
`core.logic.mock`. For those, pass the node-type string literally as the
first argument and an empty `bindingId` (synthetic nodes carry no
connection):

```csharp
// HTTP against a public API (no generated connector class):
string respRaw = await connector.ExecuteNodeAsync(
    "core.action.http", "fetchWeather", "",
    """{ "url": "https://api.open-meteo.com/v1/forecast?...", "method": "GET" }""");

// Inline JS via a script node:
string scriptOut = await connector.ExecuteNodeAsync(
    "core.action.script", "multiply", "", """{ "script": "return 7 * 6;" }""");
```

The 2nd arg is the v1 `nodeId`; the 3rd is the `bindingId` (`""` for
synthetic nodes). See the dry-run output table below for what each
synthetic kind returns.

#### Reading JSON outputs — property names are camelCased

When you access fields on a `JsonDocument` / `JsonElement` returned by
`Flow.ExecuteNode`, or on a record passed to `Flow.ExecuteNode<T>`, the
analyzer maps each property name to lowerCamelCase
(`release.TagName` → `release.tagName`). For snake_case JSON sources
(GitHub's `tag_name`, `published_at`, …) override the lowering with
`[JsonPropertyName("...")]`:

```csharp
public record GitHubRelease(
    [property: JsonPropertyName("tag_name")]     string TagName,
    [property: JsonPropertyName("published_at")] string PublishedAt,
    [property: JsonPropertyName("html_url")]     string HtmlUrl);

// Flow.ExecuteNode<T> with a complex T deserialises via JSON.parse(s) —
// the result is opaque to FIL, but property access via the attribute names
// works:
GitHubRelease release = await Flow.ExecuteNode<GitHubRelease>(
    connector, fetchRelease(), httpInputJson);
string tag = release.TagName;       // → release.tag_name in the emitted FIL
```

For primitive `T` (`string` / `int` / `double` / `bool`),
`Flow.ExecuteNode<T>` lowers to `JSON.parse(s) as T`. For complex `T`,
the result is opaque `json` — you only get the fields you read.

### Discovering connectors

```bash
# Dump the registry; pipe into grep / fzf to find the action you need.
dotnet run --project ~/src/flow-v2/csharp/src/CS2FIL -- dump-registry \
  --csharp-root ~/src/flow-v2/csharp 2>/dev/null \
  | python3 -c "import json,sys; [print(f'{c[\"shortClassName\"]:35s} {c[\"nodeType\"]}') for c in json.load(sys.stdin)]" \
  | grep -i calendar
```

Or look for the C# class in the embedded namespace `UiPath.Connectors.<KeyPascalCased>` — IntelliSense in any IDE that loads the project will surface every action.

### Supported control flow

`if`/`else`, `for`, `foreach`, `while`, `switch`/`case`/`default`,
`try`/`catch`/`finally`, `throw new Exception(msg)`, `break`, `continue`,
early `return`. Single `catch` clause. For parallel calls, give each
branch its own `Task<JsonDocument>` from `Flow.ExecuteNode` and combine
with `await Task.WhenAll(...)`; for race-to-first use `await
WhenAny.Index(...)`. Timers: `await FlowTimer.WaitAsync(connector, "id",
TimeSpan.FromMinutes(15))`.

**Unsupported expressions worth knowing about up front:**

- **`x?.y` null-conditional** is NOT supported. Rewrite as
  `x != null ? x.y : fallback`, or use the null-forgiving
  `x!.y` (the `!` is stripped at lowering and has no runtime effect).
- **`x ?? throw …`** is not supported.
- **Lambdas** are allowed ONLY as args to LINQ-style array methods
  (`Select`, `Where`, `Aggregate`, `Any`, `All`, `Find`,
  `FindIndex`, `FirstOrDefault`, `ForEach`). Anywhere else fires
  CS2FIL008.
- **`dynamic`** declarations fire CS2FIL002.

### Stdlib

Math, String, Number, JSON, `List<T>`, DateTime, TimeSpan — the supported
subset is documented in `agents.md` of any scaffolded project. The analyzer
rejects anything that doesn't translate.

### Bindings (appsettings.json)

`appsettings.json` ships with example bindings (no real UUIDs). Each entry's
`id` matches the `binding = "..."` / `folderBinding = "..."` value set
inside a `[FlowAction]` method's `with { ... }` body. For local dev:

```bash
dotnet user-secrets set "Bindings:0:resourceKey" "<connection-uuid>"
```

The verify gate (`./verify.sh`, see below) runs `csflowrun --dry-run`, which
never makes a real connector call — the runtime substitutes a placeholder UUID
internally, so empty / placeholder `resourceKey` values are fine there. But each
`binding = "..."` must still resolve to an entry in `appsettings.json`. Real
UUIDs are only needed for the deploy gate (`./convert-to-v1.sh` + `./validate.sh`).

(The scaffold's `agents.md` also documents a manual `dotnet run -- --test` smoke
run against the canned-JSON `TestConnector`; that is a local-dev affordance, not
the verify gate this skill uses.)

### Test loop

There are two modes — pick based on where you are in the cycle.

**Inner loop (fast, ~1-2 s per iteration):**

```bash
csflowrun <Name>/Workflow.cs --dry-run
```

Skips `dotnet build` entirely. Stages your `.cs` into a temp dir,
runs cs2fil in-process (every CS2FIL001-033 analyzer still fires),
then runs flow-run. CS2FIL violations surface with `file:line:col`
on your original source; runtime errors come through the
`enrichWasmStack` hint mechanism. Use this for the
edit-then-check-then-edit cycle.

**Trade-offs:** the source-mode pipeline doesn't do `Assembly.LoadFrom`,
so `[WorkflowTest]` `Assert` lambdas (the compiled predicates) don't
run — only the workflow itself executes as one smoke check. Sibling
`.cs` files (custom record types in another file) aren't picked up.
Use the full path below for both of those.

**Deploy gate (full, ~10-15 s):**

```bash
dotnet build              # CS2FIL diagnostics fire here if anything's off
./verify.sh <Name>        # builds the workflow + runs every [WorkflowTest]
```

`./verify.sh` discovers `[WorkflowTest]` methods declared on the Workflow
class (the scaffolded template ships with a smoke test plus a decision-count
assertion — keep or extend them) and runs each one through the C# Flow
runtime in dry-run mode, with stub UUIDs for every binding declared in
`appsettings.json`. The verification passes if the workflow builds cleanly,
the bindings resolve to existing entries, and each `[WorkflowTest]`'s
`Assert` predicate returns true.

A typical `[WorkflowTest]` shape:

```csharp
[WorkflowTest]
public static WorkflowTest EmptyTriggerRunsClean() => new()
{
    Inputs = "{}",
};

[WorkflowTest]
public static WorkflowTest AtLeastOneDecision() => new()
{
    Inputs = "{}",
    Assert = r => r.DecisionCount >= 1,
    AssertMessage = "expected at least one decision recorded",
};
```

For workflows with branching or multi-step dispatches, the meaningful
tests need to pin specific paths. Read [`build-tests.md`](./build-tests.md)
for the **peek-next loop** — incrementally build a history fixture per
path with `flow-run --peek-next`, then anchor each fixture via
`[WorkflowTest] { History = "fixtures/<scenario>.history.yaml" }`.

#### What connector calls return in dry-run

`verify.sh` runs the workflow in dry-run mode, which means real
connector dispatch is bypassed and the runtime substitutes a stub
response. The shape of that stub depends on the node kind. Knowing
this up front avoids writing assertions that can't be satisfied
without a history fixture:

| Node kind                | Dry-run output                                           |
| ------------------------ | -------------------------------------------------------- |
| `core.action.http`(.v2)  | `{ "body": {}, "headers": {}, "statusCode": 200 }`       |
| `core.action.script`     | `{}` (object literal)                                    |
| Generated connector node | `{}` (empty object)                                      |
| `core.logic.mock`        | The `fixture` set on the action via `rawInputs.fixture`  |
| `FlowTimer.WaitAsync`    | resolves immediately with `null`                         |

If your workflow branches on a field of the response (e.g.
`response.RootElement.GetArrayLength() > 0`), an empty dry-run output
will pick one specific branch. Either (a) accept that the smoke test
only covers that branch and rely on `[WorkflowTest] { History = … }`
fixtures for the other paths, or (b) pin the response with a fixture
and use the peek-next loop in `build-tests.md` to author it.

CS2FIL019 = "this method call isn't supported in C# Flow workflows" —
typically means you need to route through
`Flow.ExecuteNode(connector, action(), inputJson)` with a matching
`[FlowAction]` declaration, or pick something from the supported stdlib.

## Phase 3 — convert to v1 .flow

```bash
./convert-to-v1.sh <Name>
ls <Name>.flow bindings.json
```

`<Name>` is your project name (matches the directory `dotnet new csflow`
created). The script:

1. Builds the workflow from C# (analyzer or build errors abort here).
2. Runs the project with `--emit-bindings` to resolve `bindings.json` from the
   merged configuration (appsettings.json + user-secrets + env).
3. Writes `<Name>.flow` and `bindings.json` at the project root.

Intermediate artifacts live under `<Name>/.cs2v1/` — useful for debugging
conversion errors but not part of the deployable artifact set.

## Phase 4 — validate + tidy

```bash
uip maestro flow validate <Name>.flow
uip maestro flow format <Name>.flow
```

`validate` cross-checks against the v1 schema. `tidy` auto-lays out the nodes
so the file opens cleanly in the designer. Both must succeed before deploy.

## Common analyzer rules

| Diagnostic | Fix |
| --- | --- |
| **CS2FIL001** `var` requires resolvable type | `var x = null` doesn't resolve — pick a specific type. `var x = SomeConnector.Action() with { ... }` is fine; Roslyn infers `FlowAction`. |
| **CS2FIL008** lambda not allowed | Lambdas are allowed only as args to `Select/Where/Aggregate/Any/All/Find/FindIndex/FirstOrDefault/ForEach`. Inline the lambda there. |
| **CS2FIL011** unsupported statement (`do/while`, `using` (block form), …) | Rewrite as a `while` loop or pull resource lifetime out of the workflow body. `using JsonDocument` *declarations* are fine for capturing `Flow.ExecuteNode` results. |
| **CS2FIL019** disallowed method call | The method isn't supported in C# Flow workflows — route through `Flow.ExecuteNode(connector, action(), inputJson)`, use a helper from `UiPath.ActivityHarness` (`FlowTimer`, `WhenAny`), or pick from the supported stdlib. |
| **CS2FIL020** disallowed object creation | Allowed: connector `Inputs`/nested-record types, `List<T>`, `T[]`, `Exception`, `CancellationTokenSource`, the `FlowAction` / `WorkflowTest` records. For an arbitrary JSON payload, build a `string` and use `connector.ExecuteNodeAsync(...)` rather than a custom record. |
| **CS2FIL021** non-deterministic call in sync helper | Inside a `private string Foo()` (no `async`), don't call `Random.Next`, `Guid.NewGuid`, `DateTimeOffset.UtcNow`, `Stopwatch.GetTimestamp`, `Environment.TickCount`. Move the call into an async helper or use `DateTime.UtcNow` (host-pinned). |
| **CS2FIL022** `[FlowAction]` must appear at exactly one call site | Counts SYNTACTIC `Flow.ExecuteNode(connector, methodName(), …)` call sites, not runtime invocations. Inside a `foreach`/`for`/`while`/`if` the rule is still satisfied as long as `methodName()` appears once in the source. Need to dispatch the same action under multiple conditions? Wrap one `Flow.ExecuteNode` call in the conditional rather than duplicating it. |
| **CS2FIL023** action arg must be a direct method invocation | The second arg of `Flow.ExecuteNode` must literally be `methodName()` or `this.methodName()` — no variables, no chained accessors. |
| **CS2FIL029** helper with `Flow.ExecuteNode` may be invoked at most once | A private helper containing a `Flow.ExecuteNode` call gets inlined; calling it twice would clone the action and break CS2FIL022. Split into separate helpers. |

## Troubleshooting

- **`dotnet build` fails with NU1101 (package not found)** → the project's
  `.nuget/` dir is missing — re-scaffold with `dotnet new csflow`.
- **`./convert-to-v1.sh` fails during the build step** → analyzer
  diagnostics are printed to stderr; fix `Workflow.cs` and re-run.
- **`uip maestro flow validate` complains about a missing binding** → fill
  the `resourceKey`/`default` UUIDs in `appsettings.json` (or via
  `dotnet user-secrets set Bindings:N:resourceKey <uuid>`) and re-run
  `convert-to-v1.sh`.
