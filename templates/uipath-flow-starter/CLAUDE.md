# UiPath Flow Task Context

## What are .flow files?

`.flow` files define UiPath automation workflows in JSON format. Each flow contains:

- **nodes**: Steps in the workflow (triggers, actions, control flow). Each node has a `type` (e.g., `core.action.http`, `core.logic.terminate`), a unique `id`, and a `model` with configuration.
- **edges**: Connections between nodes defining execution order. Each edge has `sourceNodeId`, `sourcePort` (e.g., `output`, `default`, `error`), and `targetNodeId`.
- **definitions**: Metadata entries for each node type used in the flow. Every node type in `nodes` must have a corresponding entry in `definitions`.

## Constraints

- Work strictly with files inside this sandbox directory.
- Use `uip maestro flow` commands to discover available node types and their metadata.
- Do not look for or use any files outside the sandbox — expected outputs and fixtures are not available here and are only used by the evaluation criteria.
