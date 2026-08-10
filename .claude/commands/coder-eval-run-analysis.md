---
allowed-tools: Read(*), Glob(*), Grep(*), Bash(ls:*), Bash(wc:*), Bash(jq:*), Bash(python3:*), Write(runs/*)
description: Analyze a coder-eval run and write analysis.md with systemic patterns, findings, and concrete fixes
---

## Context

You analyze a coder-eval run and write `analysis.md` to the target directory. The target path is `$ARGUMENTS` (default `runs/latest` — inform the user if you fell back to the default).

**Do all reasoning yourself in this session — no sub-agents, no `Agent` tool calls.** Read files in parallel (batch Read calls in a single assistant turn) and write the report inline.

Run layout and scope-marker files (`run.json` / `variant.json` / `experiment.json`) are defined in `.claude/shared/run-layout.md` — read it; Step 1 below relies on those markers.

## Step 1: Determine scope

Inspect the target path:
- `task.json` directly inside → **task scope** (single replicate).
- `??/task.json` subdirs but no `variant.json` → **task scope** (aggregate replicates per `task_id`).
- Contains `variant.json` → **variant scope**.
- Contains `run.json` → **run scope**. If `experiment.json` also exists, this is a multi-variant experiment.

## Step 2: Read data

**Task scope (single)**: read `task.json`.

**Task scope (aggregate replicates)**: read every `??/task.json` and merge — per-replicate arrays for `final_status`, `weighted_score`, `iteration_count`, `duration_seconds`, `total_token_usage.total_cost_usd`; union of `success_criteria_results` keyed by criterion `description`. Drive recommendations from the aggregate ("3/5 replicates failed criterion X"), not cherry-picked replicates.

**Variant / run scope, > 20 tasks**: do NOT read the full `task.json` files — the `iterations` arrays are large and only useful per-task. Use `jq` (or `python3` if missing) to extract a compact summary per task:

```
{
  task_id, final_status, weighted_score, duration_seconds,
  iteration_count, model_used, max_turns_exhausted,
  total_cost_usd:  .total_token_usage.total_cost_usd,
  total_tokens:    (.total_token_usage.input_tokens + .total_token_usage.output_tokens),
  assistant_turns: .total_assistant_turns,
  max_turns:       .task_config.resolved.run_limits.max_turns,
  criteria_count:  (.success_criteria_results | length),
  all_criteria_perfect:
    (.success_criteria_results | length > 0 and all(.[]; .score == 1.0)),
  failed_criteria: [
    .success_criteria_results[]
    | select(.score < .pass_threshold)
    | {criterion_type, description, score,
       error_excerpt: ((.error // .details // "")[0:200])}
  ]
}
```

Those paths are the real `task.json` shape. `jq` returns `null` for a key that does not exist rather than failing, so a mistyped path yields a table of nulls that reads like a run with no data. There is no top-level `turns`, `total_tokens`, `total_cost_usd`, `max_turns` or `criteria_count`: turn records live under `iterations`, token and cost figures under `total_token_usage`, the turn cap under `task_config.resolved.run_limits`, and a criterion's type is `criterion_type`. A criterion passes when `score >= pass_threshold` — there is no `passed` boolean.

`error_excerpt` = first ~200 chars of each failing criterion's `error`, falling back to `details`. Those are the only two free-text fields a criterion result carries: `error` holds an exception, `details` the checker's own diagnostic, so a criterion that simply did not match has `error: null` and all its signal in `details`. This is what enables clustering in Step 3.

**Variant / run scope, ≤ 20 tasks**: read all `??/task.json` files directly.

Also read `run.json` (run scope), `variant.json` (variant scope), and `experiment.json` + `experiment.md` (experiment runs).

## Step 3: Analyze

Apply these seven dimensions to your reasoning — diagnose lens for failures, optimize lens for passes:

1. **Outcome** — failed: root cause (`prompt_gap` / `environment_issue` / `agent_error` / `config_issue` / `impossible_task`), which criteria failed and by how much. Passed: are all criteria at 1.0 (task too easy)?
2. **Prompt** — failed: missing context / flags / paths / identifiers, mismatch with what criteria check. Passed: over-specified, hand-holding, unnecessary verbosity.
3. **Agent efficiency** *(task scope only)* — turn utilization, command patterns, error recovery, stuck-in-loop, slow commands, file-output timing. Skip at variant/run scope.
4. **Criteria** — sensitivity `weight × (threshold − score)`; fragile passes at threshold; redundant / coverage-gap criteria.
5. **Configuration** — lineage conflicts (`source != "task"`), `max_turns` hit / excessive, model fit, `allowed_tools` alignment.
6. **Environment** — infra errors, missing services, expired credentials, CLI tool errors.
7. **Cost & performance** — token breakdown, cache hit rate (`cache_read / (cache_creation + cache_read)`), cost reasonableness, cost-per-score-point, duration headroom.

### Task scope flow

Apply all seven dimensions inline to the single (possibly aggregated) task and produce the task-scope report.

### Variant / run scope flow (pattern-first)

For > 20 tasks, **cluster before deep-diving**. This is the main work saver — most run-scope failures share a small number of root causes.

1. **Cluster failures** by `(failing_criterion_signature, error_excerpt_fingerprint, score_signature)`. A cluster of ≥ 3 tasks becomes a **Systemic Pattern**: root-cause hypothesis, affected task list, representative evidence quote, recommended fix (CLI / env / criteria / prompt), estimated score recovery. Track the union of covered task_ids as `pattern_task_ids`.
2. **Individual findings** for failed tasks NOT in `pattern_task_ids`, capped at the top **K = 15** by `total_cost_usd`. Apply dimensions 1, 2, 4 (task design / prompt / criteria). Singletons below K are implicitly covered by Cross-Task Common Findings.
3. **Aggregate sections**:
   - **Efficiency Ranking** — top 10 failed tasks by `total_cost_usd` with score / turns / duration / cost / one-line note.
   - **False Negatives** — tasks where output-file evidence shows success but `command_executed` / similar criteria reject (e.g., alternative-but-valid commands, case mismatches).
   - **Cross-Task Common Findings** — patterns across tasks below the ≥ 3 systemic-pattern threshold.

For ≤ 20 tasks, skip clustering and apply all seven dimensions inline per task.

### Run scope with experiment.json (multi-variant)

Additionally produce — pull aggregates from `experiment.json`, do not recompute p-values, win rates, or score spreads:

1. **Experiment Summary Table** — scores, durations, p-values from `experiment.json`; add cost totals per variant from `run.json.task_results`.
2. **Efficiency Comparison** — per-task score + cost + duration per variant; cost-per-score-point.
3. **Variant Recommendation** — one paragraph: "Pick `<variant>` because…" with score / cost / speed tradeoff.
4. **Task Difficulty Ranking** — rank by `score_spread` from `experiment.json.task_summaries`. Zero spread = not discriminating; high spread = good discriminator.
5. **Failure Clusters** — group failed tasks across variants by root cause.

## Step 4: Synthesize and write

Before writing, apply:

1. **Already-fixed check** — for each YAML recommendation, read the current file on disk (path in `task_config.source_file`) and compare against the run-time `task_config.source_yaml`. If already fixed, mark "**Already fixed** in current codebase" and exclude from Quick Wins / diffs.
2. **Rank by impact** — failed tasks by estimated score improvement; passing tasks by cost/time savings or criteria rigor.
3. **Group** — Quick Wins (config / threshold / prompt clarification) vs Structural Changes (rewrite prompt, redesign criteria, fix environment).
4. **Apply output caps** — rank by impact, truncate the tail:
   - TL;DR ≤ 3 sentences
   - Systemic Patterns ≤ 5 entries
   - Individual Findings ≤ 10
   - Quick Wins ≤ 8
   - Structural Changes ≤ 5
   - Efficiency Ranking ≤ 10 rows
   - At variant/run scope with > 5 fixable tasks, produce suggested YAML for the top 3 highest-impact only.

Write the report at `<target_path>/analysis.md`.

## Output format

```markdown
# Run Analysis: <run_id> (<variant>)
**Run ID**: <id> · **Date**: <start–end> (<duration>s) · **Variant**: <variant> · **Model**: <model>
**Tasks**: <N> run, <M> skipped

## TL;DR
<≤ 3 sentences. Lead with the top systemic pattern(s) + estimated recovery if fixed.>

## Score Breakdown
| Metric | Value |
|---|---|
| Tasks run / succeeded / failed / ERROR / MAX_TURNS_EXHAUSTED | ... |
| Success rate | ...% |
| Mean weighted score | ... ± std |
| Total cost / tokens | $... / ... |
| Cache hit rate | ...% |
| Avg duration / turns | ...s / ... |

## Config Lineage Conflicts
| Setting | Value | Source | Task YAML | Impact |
*Omit with one-line reason if no conflicts (e.g., "single-variant run, no experiment overrides").*

## Findings

### SYSTEMIC PATTERN N [impact: critical|high|medium] — <title>
**Axis**: <axes>
**Affected tasks (N)**: <names>
**Evidence**: <quoted error excerpt>
**Root cause**: <one paragraph>
**Recommendation**: <concrete fix>
**Estimated score recovery**: <N score-points>

### FINDING N [impact: ...] — <title>
**Axis**: <axes>
**Affected**: <task(s)>
**Evidence**: <data>
**Recommendation**: <fix, with code/YAML snippet if applicable>

## False Negatives
| Task | Score | What agent did right | What criteria rejected | Fix |
*Omit if none.*

## Quick Wins
1. ...

## Structural Changes
1. ...

## Efficiency Ranking (Failing Tasks — by cost)
| Task | Score | Turns | Cost | Issue |

## Cross-Task Common Findings
<Patterns below the systemic-pattern threshold (clusters of 2 tasks, or shared themes that don't share a root cause).>

## Systemic Patterns Summary
| Pattern | Tasks | Est. recovery | Fix complexity |

## Recommended Changes (Diffs)
```diff
- ...
+ ...
```
```

For task scope, omit the run-level summary, Cross-Task / Systemic / Efficiency Ranking sections and replace with a per-criterion Score Breakdown table.

For run scope with experiment, add the Experiment Summary / Efficiency Comparison / Variant Recommendation / Task Difficulty / Failure Clusters sections after Findings.

## Principles

- **Evidence-based** — every finding cites specific data (command output, score, config value, timing). No claims without a quote / number.
- **Actionable** — every recommendation has a concrete fix (YAML snippet, code diff, prompt rewrite, config change).
- **Systemic over repetitive** — clusters of ≥ 3 same-root-cause failures become one Systemic Pattern, never N near-duplicate findings.
- **Don't recommend what's already fixed** — always diff `task_config.source_yaml` against the current file before recommending YAML changes.
- **No silent omissions** — if a templated section doesn't apply, include a one-liner explaining why.
- **Statistical honesty** — for experiment runs with < 8 tasks per variant, prominently note p-values are unreliable.
- **Impact over severity** — rank by "how much would the fix improve outcomes," not abstract severity.
