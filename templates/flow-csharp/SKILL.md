---
name: uipath-flow-csharp
description: "UiPath Flow workflow as a C# project. Author in C# with full IDE/Roslyn-analyzer feedback, build and test in-process via TestConnector, then convert to Flow v2 (FIL + manifest + bindings) and on to v1 .flow for the deploy gate."
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# UiPath C# Flow workflow

A C# Flow workflow project is a normal `.NET` console app whose `Workflow.cs`
follows a strict subset of C# that the `cs2fil` converter recognizes. Roslyn
analyzers (CS2FIL001–021) flag every FIL incompatibility at edit time — your
inner loop is `dotnet build` and `dotnet run -- --test`, not the FIL
converter.

## Five phases

```
1. Scaffold       dotnet new csflow --name <Name>
2. Author + test  edit Workflow.cs (analyzers gate); dotnet build; dotnet run -- --test
3. Convert to v2  ./convert-to-v2.sh <Name>     → <Name>.fil, .manifest.flow, .bindings.flow
4. Convert to v1  ./convert-to-v1.sh <Name>     → <Name>.flow
5. Validate       uip maestro flow validate <Name>.flow && uip maestro flow tidy <Name>.flow
```

The `csflow` template is pre-installed in this environment — go straight to
phase 1.

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
public class Workflow(IConnector connector)
{
    public async Task Execute()
    {
        // Body converts to FIL. Analyzers (CS2FIL001–021) reject anything
        // outside the FIL-convertible subset.
    }
}
```

### Calling a connector

```csharp
SendEmail.Inputs inputs = new(
    toRecipients: "alice@example.com",
    subject: "Hello",
    body: "Hi there");

SendEmail.Outputs result = await SendEmail.Execute(
    connector,
    nodeId: "sendEmail1",       // stable per-call id; lands in manifest
    bindingId: "outlook",       // points at appsettings Bindings entry
    inputs);
```

The fourth `Execute` arg is the typed `Inputs` record — IntelliSense shows
required fields. The generated record's parameter order matches the connector's
input schema.

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
early `return`. Single `catch` clause. Use `await Task.WhenAll(...)` for
parallel calls; `await WhenAny.Index(...)` for race-to-first; `await
FlowTimer.WaitAsync(connector, "id", TimeSpan.FromMinutes(15))` for timers.

### Stdlib

Math, String, Number, JSON, `List<T>`, DateTime, TimeSpan — the
FIL-convertible subset is documented in `agents.md` of any scaffolded
project. The analyzer rejects anything that doesn't translate.

### Bindings (appsettings.json)

`appsettings.json` ships with example bindings (no real UUIDs). Each entry's
`id` matches a `bindingId` you pass to `Execute(...)`. For local dev:

```bash
dotnet user-secrets set "Bindings:0:resourceKey" "<connection-uuid>"
```

For test mode (`--test`), bindings are ignored — `TestConnector` returns canned
JSON.

### Test loop

```bash
dotnet build              # CS2FIL001–021 errors here if anything's off
dotnet run -- --test      # runs the workflow against TestConnector
```

CS2FIL019 = "this method call has no FIL equivalent" — typically means you
need to switch to the connector-class API (e.g. `SendEmail.Execute(...)`)
or pick something from the supported stdlib.

## Phase 3 — convert to v2 (FIL + manifest + bindings)

```bash
./convert-to-v2.sh <Name>
ls <Name>.fil <Name>.manifest.flow <Name>.bindings.flow
```

`<Name>` is your project name (matches the directory `dotnet new csflow`
created). The script invokes `cs2fil cs-to-fil` against the project and writes
all three files.

The emitted `<Name>.bindings.flow` is a **stub** — same shape as
`appsettings.json`'s `Bindings` section but `resourceKey` and `default` blank.
For `flow-run` or production, the user's `appsettings.json` (with secrets
filled in via `dotnet user-secrets`) is the authoritative version; copy
those values into `<Name>.bindings.flow` before deploying.

## Phase 4 — convert to v1 .flow

```bash
./convert-to-v1.sh <Name>      # writes <Name>.flow
```

Wraps the `v2-to-v1` CLI. Reads the three v2 files; produces the v1 JSON.

## Phase 5 — validate + tidy

```bash
uip maestro flow validate <Name>.flow
uip maestro flow tidy <Name>.flow
```

`validate` cross-checks against the v1 schema. `tidy` auto-lays out the nodes
so the file opens cleanly in the designer. Both must succeed before deploy.

## Common analyzer rules

| Diagnostic | Fix |
| --- | --- |
| **CS2FIL001** `var` not allowed | Use the explicit type: `SendEmail.Inputs inputs = new(...)` |
| **CS2FIL008** lambda not allowed | Lambdas are allowed only as args to `Select/Where/Aggregate/Any/All/Find/FindIndex/FirstOrDefault/ForEach`. Inline the lambda there. |
| **CS2FIL011** unsupported statement (`do/while`, `using`, …) | Rewrite as a `while` loop or pull resource lifetime out of the workflow body. |
| **CS2FIL019** disallowed method call | The method has no FIL equivalent — use the connector class API or a helper from `UiPath.ActivityHarness` (`FlowTimer`, `WhenAny`). |
| **CS2FIL020** disallowed object creation | Allowed: connector `Inputs`, `List<T>`, `Exception`. Anything else → rewrite. |
| **CS2FIL021** non-deterministic call in sync helper | Inside a `private string Foo()` (no `async`), don't call `Random.Next`, `Guid.NewGuid`, `DateTimeOffset.UtcNow`, `Stopwatch.GetTimestamp`, `Environment.TickCount`. Move the call into an async helper or use `DateTime.UtcNow` (host-pinned). |

## Troubleshooting

- **`dotnet build` fails with NU1101 (package not found)** → the project's
  `.nuget/` dir is missing — re-scaffold with `dotnet new csflow`.
- **`./convert-to-v2.sh` succeeds but `<Name>.fil` is empty** → check stderr
  for analyzer errors. `cs2fil to-fil` runs the analyzers before emission.
- **`uip maestro flow validate` complains about a missing binding** → fill
  the `resourceKey`/`default` UUIDs in `<Name>.bindings.flow`.
