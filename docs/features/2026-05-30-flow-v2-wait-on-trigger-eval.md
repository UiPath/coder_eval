# flow-v2 waitOnTrigger Eval

This adds `uipath-flow-v2-wait-on-trigger`, a v2-only eval for the mid-flow
event-trigger runtime path.

The task proves:

- FIL can declare a manual start plus a webhook event trigger;
- `waitOnTrigger(webhookTrigger)` compiles and resumes from a local
  `--trigger-event` payload;
- `flow-run` persists `triggerEvent:` history and replays it into the next
  decision;
- branch routing can use fields parsed from the event payload.

There is no paired v1 JSON task yet. v2-to-v1 still rejects `waitOnTrigger`
flows until the BPMN intermediate catch-event mapping and graph-walk rewrite
land, so this eval measures the authoring and local verification loop only.
