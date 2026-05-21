---
name: uipath-flow-v2-process-resources
description: "UiPath Flow v2 process-resource authoring — FIL plus bindings.json for published API Workflow, RPA Workflow, and Agentic Process nodes. Verify with flow-run dry-run; live process dispatch is intentionally blocked until the CLI/API path is confirmed."
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# UiPath Flow v2 Process Resources

A current Flow v2 project is two files:

| File | Role |
| --- | --- |
| `<Name>.fil` | FIL source: TypeScript-subset logic plus `flow`, `trigger`, and `action` declarations |
| `bindings.json` | Connector IDs and process-resource name/folder bindings referenced by the FIL declarations |

Do not create a `.manifest.flow` file for these tasks. The action declarations in FIL carry the node metadata.

## Authoring Loop

1. Write `<Name>.fil` with a top-level `flow`, a manual `trigger`, process-resource `action` declarations, mock actions for branch endpoints, and `async function main()`.
2. Write `bindings.json` with process `name` and `folderPath` bindings.
3. Run `./parse-fil.sh <Name>.fil`.
4. Run `./verify.sh`. This uses `flow-run --dry-run`, validates process bindings, and returns each process node's `fixture`.
5. Run `./convert.sh <Name>`.
6. Run `uip maestro flow validate <Name>.flow` and `uip maestro flow format <Name>.flow`.

Do not run `./verify.sh --live` for API Workflow, RPA Workflow, or Agentic Process nodes. Live dispatch is blocked until the direct Orchestrator CLI/API path is confirmed.

## FIL Basics

Every project needs these top-level declarations:

```typescript
flow api-age-lookup {
  name: "API Age Lookup",
  version: "1.0.0",
};

trigger manualStart: start;

action adultRoute: mock {
  label: "Adult Route",
  fixture: { route: "adult" },
};

async function main(): Promise<void> {
  await executeNode(adultRoute, "{}");
}
```

`executeNode(<actionRef>, inputJson)` takes an action identifier, not a string. Use `await executeNode(nameToAge, JSON.stringify({ name: "tomasz" }))`, not `executeNode("nameToAge", ...)`.

**Name await-result locals `<nodeId>Data`.** The `const` that captures an await lowers to `$vars.<nodeId>.output` in the v1 form; your local's name is otherwise lost on round-trip. Using `nameToAgeData` (matching v1-to-v2's reconstruction convention) keeps v1 ↔ v2 round-trips deterministic. Example: `const nameToAgeData: string = await executeNode(nameToAge, …);`.

**One action per call site.** Each `action` declaration must be invoked by exactly one `executeNode` call — actions aren't functions, they lower to single v1 nodes that can't be reused across distinct call sites. One call site inside a loop or one branch of an `if/else` is fine; the same identifier appearing at two lexical positions is a compile error. If you need to invoke a published process from two places (e.g., a retry path), declare a second action (e.g., `nameToAgeRetry`) with its own `resourceBindings`. Likewise, declaring an action you never invoke is a compile error — remove it or wire it in. Prefer operation-shape names (`fetchAdult`, `routeToAdult`) over variable-shape names (`process`, `result`) since each name describes one call site.

Parse node outputs as JSON, then coerce fields into concrete types when needed:

```typescript
const raw: string = await executeNode(nameToAge, JSON.stringify({ name: "tomasz" }));
const result: json = JSON.parse(raw);
const response: json = result.response;
const age: i32 = response.age;
```

`if` / `else` branches convert into v1 decision nodes. `mock` actions are useful deterministic endpoints for eval tasks.

## API Workflow Action

Published API Workflow nodes use `uipath.core.api-workflow.<key>@1.0.0` and process resource bindings with `resourceSubType: "Api"`.

```typescript
action nameToAge: uipath.core.api-workflow.346b8959-c126-48d3-9c46-942abcf944d7@1.0.0 {
  label: "Name To Age",
  rawInputs: { name: "tomasz" },
  resource: {
    resource: "process",
    resourceSubType: "Api",
    resourceKey: "Shared/test-darwin-apis.API Workflow",
    orchestratorType: "api",
    serviceType: "Orchestrator.ExecuteApiWorkflowAsync",
    section: "Published",
  },
  resourceBindings: {
    name: "bApiName",
    folderPath: "bApiFolder",
  },
  outputs: {
    error: {
      type: "object",
      description: "Error information if the node fails",
      source: "=Error",
      var: "error",
    },
  },
  fixture: { response: { age: 42 } },
};
```

## RPA Workflow Action

Published RPA Workflow nodes use `uipath.core.rpa-workflow.<key>@1.0.0` and process resource bindings with `resourceSubType: "Process"`.

```typescript
action runRobot: uipath.core.rpa-workflow.7648307a-b180-467c-81f3-06f49a87313b@1.0.0 {
  label: "RPA Workflow12Feb",
  rawInputs: { newArgument: "" },
  resource: {
    resource: "process",
    resourceSubType: "Process",
    resourceKey: "Shared/sol_with_all_projects.RPA Workflow12Feb",
    orchestratorType: "process",
    serviceType: "Orchestrator.StartJob",
    section: "Published",
  },
  resourceBindings: {
    name: "bRpaName",
    folderPath: "bRpaFolder",
  },
  outputs: {
    error: {
      type: "object",
      description: "Error information if the node fails",
      source: "=Error",
      var: "error",
    },
  },
  fixture: { output: { jobId: "dry-run-job" } },
};
```

## Agentic Process Action

Published Agentic Process nodes use `uipath.core.agentic-process.<key>@1.0.0` and process resource bindings with `resourceSubType: "ProcessOrchestration"`. The current Flow Workbench shape uses `Orchestrator.StartAgenticProcessAsync`; some registry docs may show `Orchestrator.StartAgenticProcess`, so preserve the explicit `serviceType` from the task or source flow.

```typescript
action runOrchestration: uipath.core.agentic-process.5f9ad95a-b862-46c7-98c3-a9be2e5b922f@1.0.0 {
  label: "Run Customer Intake",
  rawInputs: { customerName: "Ada Lovelace" },
  resource: {
    resource: "process",
    resourceSubType: "ProcessOrchestration",
    resourceKey: "Shared.Customer Intake Orchestration",
    serviceType: "Orchestrator.StartAgenticProcessAsync",
    section: "Published",
  },
  resourceBindings: {
    name: "bAgenticProcessName",
    folderPath: "bAgenticProcessFolder",
  },
  outputs: {
    output: {
      type: "object",
      description: "The return value of the agentic process",
      source: "=result.response",
      var: "output",
    },
    error: {
      type: "object",
      description: "Error information if the agentic process fails",
      source: "=result.Error",
      var: "error",
    },
  },
  fixture: { response: { status: "accepted", priority: "high" } },
};
```

## Bindings

Process resource bindings use `resource: "process"` and one entry for each resource property.

```json
{
  "schemaVersion": "1",
  "bindings": [
    {
      "id": "bApiName",
      "name": "name",
      "type": "string",
      "resource": "process",
      "resourceKey": "Shared/test-darwin-apis.API Workflow",
      "default": "API Workflow",
      "propertyAttribute": "name",
      "resourceSubType": "Api"
    },
    {
      "id": "bApiFolder",
      "name": "folderPath",
      "type": "string",
      "resource": "process",
      "resourceKey": "Shared/test-darwin-apis.API Workflow",
      "default": "Shared/test-darwin-apis",
      "propertyAttribute": "folderPath",
      "resourceSubType": "Api"
    }
  ]
}
```

For RPA Workflow, use `bRpaName` / `bRpaFolder`, `resourceSubType: "Process"`, and the `Shared/sol_with_all_projects.RPA Workflow12Feb` resource key.

For Agentic Process, use `bAgenticProcessName` / `bAgenticProcessFolder`, `resourceSubType: "ProcessOrchestration"`, and the `Shared.Customer Intake Orchestration` resource key.

## What Verification Means

`./verify.sh` runs `flow-run --dry-run`. It validates the FIL, process-resource action declarations, and `bindings.json`. API Workflow, RPA Workflow, and Agentic Process nodes return their `fixture` values in dry-run. Live dispatch fails intentionally with an explicit unsupported-dispatch error.

`./convert.sh <Name>` calls the v2-to-v1 converter with `<Name>.fil` and writes `<Name>.flow`. The converted v1 flow should validate and format with the UiPath CLI.
