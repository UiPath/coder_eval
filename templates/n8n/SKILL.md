---
name: n8n-workflow
description: "n8n workflow JSON — author, validate, import. Workflow lives in one .json file with `nodes[]` + `connections{}`. Use `n8n import:workflow` to validate structurally. Credentials are referenced by name; the eval doesn't require real credentials to be wired up."
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# n8n workflow authoring

An n8n workflow is a single JSON file. The CLI ships ~880 node types, including Slack, Microsoft Outlook, Jira Software, HTTP Request, IF, Loop Over Items, Code, and Edit Fields.

## File shape

```json
{
  "name": "Workflow Name",
  "id": "00000000-0000-0000-0000-000000000001",
  "nodes": [ /* see below */ ],
  "connections": { /* see below */ }
}
```

Required: top-level `id` (UUID) — `n8n import:workflow` fails with `NOT NULL constraint failed` without it.

## Node anatomy

Every node is an object with these fields:

```json
{
  "parameters": { /* node-specific config */ },
  "type": "n8n-nodes-base.<nodeName>",
  "typeVersion": 1,
  "position": [x, y],
  "id": "<uuid>",
  "name": "Display Name"
}
```

`name` is what `connections` references; `id` is the persistent UUID. Both must be unique per workflow. `position` is layout-only; any number is fine.

## Connections

`connections` is keyed by source node `name` and lists per-output-port arrays of targets:

```json
"connections": {
  "Source Node": {
    "main": [
      [{"node": "Target A", "type": "main", "index": 0}]
    ]
  }
}
```

The outer array indexes the source's output ports (0-based). Most nodes have one `main` output. **IF** has two outputs (`[0] = true branch`, `[1] = false branch`). **Loop Over Items** also has two outputs (`[0] = each batch`, `[1] = done — fires once after the loop completes`). To fan out to multiple targets from the same output, list them in the same inner array.

## Expression syntax

In any `parameters` string field, prefix with `=` to make it an expression. Inside, use `{{ ... }}` for interpolation:

| Pattern | Meaning |
|---|---|
| `{{ $json.field }}` | Field on the current item flowing into this node |
| `{{ $json["weird key"] }}` | Bracket form for non-identifier keys |
| `{{ $node["Other Node"].json.field }}` | Field on another node's output |
| `{{ $now.minus(7, "days").toISO() }}` | Built-in `$now` (Luxon DateTime); 7-day-ago timestamp |
| `=foo {{ $json.tag }} bar` | Mixed literal + interpolation, prefix with `=` |

## Common nodes for the typical scenario

### `n8n-nodes-base.manualTrigger` — workflow entry point

```json
{"parameters": {}, "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "id": "...", "name": "When clicking 'Test workflow'"}
```

### `n8n-nodes-base.set` — initialize / transform fields (Edit Fields)

```json
{
  "parameters": {
    "assignments": {
      "assignments": [
        {"id": "a1", "name": "repos", "value": "=[\"nodejs/node\", \"facebook/react\"]", "type": "array"}
      ]
    },
    "options": {}
  },
  "type": "n8n-nodes-base.set",
  "typeVersion": 3.4,
  "id": "...", "name": "Init repos"
}
```

### `n8n-nodes-base.splitOut` — explode an array field into separate items

```json
{
  "parameters": {"fieldToSplitOut": "repos", "options": {}},
  "type": "n8n-nodes-base.splitOut",
  "typeVersion": 1,
  "id": "...", "name": "Split repos"
}
```

### `n8n-nodes-base.splitInBatches` — loop over incoming items (Loop Over Items)

```json
{
  "parameters": {"options": {"reset": false}},
  "type": "n8n-nodes-base.splitInBatches",
  "typeVersion": 3,
  "id": "...", "name": "Loop Over Items"
}
```

Output 0 is each batch (typically batch size 1 = each item). Output 1 fires once after all items processed. The loop terminus connects back to the splitInBatches's input.

### `n8n-nodes-base.httpRequest` — call an HTTP API

```json
{
  "parameters": {
    "url": "=https://api.github.com/repos/{{ $json.repo }}/releases/latest",
    "options": {}
  },
  "type": "n8n-nodes-base.httpRequest",
  "typeVersion": 4.2,
  "id": "...", "name": "Fetch Release"
}
```

The HTTP node's response JSON becomes the item shape for the next node. So if GitHub returns `{tag_name, body, html_url}`, downstream expressions use `{{ $json.tag_name }}`.

### `n8n-nodes-base.if` — decision, 2 outputs

```json
{
  "parameters": {
    "conditions": {
      "options": {"caseSensitive": false, "typeValidation": "strict"},
      "conditions": [
        {
          "leftValue": "={{ $json.name }}{{ $json.body }}",
          "rightValue": "cve",
          "operator": {"type": "string", "operation": "contains"}
        }
      ],
      "combinator": "or"
    }
  },
  "type": "n8n-nodes-base.if",
  "typeVersion": 2,
  "id": "...", "name": "Is Security?"
}
```

Use `combinator: "or"` (or `"and"`) and add more entries to the `conditions[]` array for multi-keyword checks.

### `n8n-nodes-base.code` — JavaScript for counters/aggregation

```json
{
  "parameters": {
    "mode": "runOnceForAllItems",
    "language": "javaScript",
    "jsCode": "const total = items.length;\nconst security = items.filter(i => i.json.isSecurity).length;\nreturn [{ json: { total, security } }];"
  },
  "type": "n8n-nodes-base.code",
  "typeVersion": 2,
  "id": "...", "name": "Aggregate"
}
```

### Connector nodes — Slack / Outlook / Jira

These need credentials. **For the eval, you don't need to provision real credentials** — the verification path is `n8n import:workflow` which doesn't execute, just validates structure. Author the node parameters as if credentials existed; the eval doesn't run.

```json
// Slack DM
{
  "parameters": {
    "resource": "message",
    "select": "user",
    "user": {"__rl": true, "mode": "id", "value": "U000000"},
    "messageType": "text",
    "text": "=Release: {{ $json.tag_name }}",
    "otherOptions": {}
  },
  "type": "n8n-nodes-base.slack",
  "typeVersion": 2.3
}

// Microsoft Outlook send email
{
  "parameters": {
    "resource": "message",
    "operation": "send",
    "subject": "=Release: {{ $json.tag_name }}",
    "bodyContent": "={{ $json.body }}",
    "bodyContentType": "Text",
    "toRecipients": "you@example.com",
    "additionalFields": {}
  },
  "type": "n8n-nodes-base.microsoftOutlook",
  "typeVersion": 2
}

// Jira create issue
{
  "parameters": {
    "resource": "issue",
    "project": {"__rl": true, "mode": "list", "value": "MAESTRO"},
    "summary": "=Security release: {{ $json.tag_name }}",
    "issueType": {"__rl": true, "mode": "list", "value": "10001"},
    "additionalFields": {"description": "={{ $json.body }}"}
  },
  "type": "n8n-nodes-base.jira",
  "typeVersion": 1
}
```

The `__rl` pattern (resourceLocator) is n8n's "either pick from a list or type the literal value" widget; setting `mode: "list"` and a literal `value` works for static identifiers.

## Discovering node schemas

To see the full parameter shape of any node, export the in-tree catalog and grep it:

```bash
n8n export:nodes --output=/tmp/nodes.json
python3 -c "
import json
n = next(x for x in json.load(open('/tmp/nodes.json')) if x['name'] == 'n8n-nodes-base.slack')
import json as j; print(j.dumps(n.get('properties', []), indent=2)[:3000])
"
```

`properties[]` lists every parameter with its name, type, default, and `displayOptions` (conditional visibility).

## Verifying with `n8n import:workflow`

```bash
N8N_USER_FOLDER=./.n8n n8n import:workflow --input=workflow.json
```

The `N8N_USER_FOLDER` env var isolates the local n8n DB. A successful import looks like:

```
Importing 1 workflows...
Successfully imported 1 workflow.
```

Failure modes the import catches:
- `NOT NULL constraint failed: workflow_entity.id` — missing top-level `id`
- `JSON parse error` — malformed JSON
- references to non-existent node types fail at execute time, not import

Import does NOT validate parameter shape against the node's schema — n8n is permissive at the JSON level. Use `n8n execute --id=<workflow-id>` if you want runtime validation, but that requires real credentials for every connector.

## Worked example

See `example/ReleaseMonitor.json` — a complete workflow that fetches GitHub releases for a list of repos, branches on security keywords, fans out to Jira + Slack + Outlook on the security path, sends Slack + Outlook on the normal path, and loops. Adapt this.
