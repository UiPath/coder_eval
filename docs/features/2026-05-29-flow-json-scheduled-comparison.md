# flow-json-scheduled Comparison

This adds the scheduled-start pair for Flow trigger work:

| Flavor | Task | Output |
| --- | --- | --- |
| v1 JSON | `tasks/uipath_flow/scheduled/scheduled.yaml` (`flow-json-scheduled`) | `ScheduledWeatherAlert/flow_files/ScheduledWeatherAlert.flow` |
| v2 FIL | `tasks/uipath_flow/v2_authoring/scheduled_v2.yaml` (`uipath-flow-v2-scheduled`) | `ScheduledWeatherAlert.fil`, `bindings.json`, `ScheduledWeatherAlert.flow` |

Both tasks implement the same hourly Bellevue weather alert:

- exactly one `core.trigger.scheduled` start trigger;
- `timerType: "timeCycle"`, `timerPreset: "R/PT1H"`, and a preserved `entryPointId`;
- an open-meteo HTTP fetch;
- temperature threshold logic at 60F;
- `{ message: "nice day" }` or `{ message: "bring a jacket" }` as the result.

The v1 task measures pure JSON authoring, including explicit scheduled-trigger
definition work. The v2 task measures the FIL authoring path after scheduled
trigger support landed in the flow-v2 converter and vendored task template.

Expected comparison signal:

- v2 should need fewer authored JSON details because `convert.sh` supplies the
  v1 node shape and definitions.
- v2 should be faster and cheaper when the agent follows the `verify.sh` loop.
- Both outputs must pass `uip maestro flow validate` and `uip maestro flow format`.

Run them as ordinary coder-eval tasks, then compare turn count, duration, token
cost, and criterion scores for the two task IDs.
