---
allowed-tools: Read(*), Glob(*), Grep(*), Bash(ls:*), Bash(wc:*), Bash(jq:*), Bash(python3:*), Agent, Write(runs/*)
description: Analyze evaluation runs and suggest improvements to tasks, config, and prompts
---

## Context

If `$ARGUMENTS` is empty or blank, default to `runs/latest` and inform the user: "No path provided — analyzing the latest run at `runs/latest`."

You are analyzing a coder_eval evaluation run to produce actionable recommendations. The target path is: `$ARGUMENTS`

This skill works for both **failed runs** (diagnose what went wrong) and **passing runs** (suggest optimizations). Every analysis axis has two lenses:
- **Diagnose**: When things failed — identify root causes and fixes.
- **Optimize**: When things passed — suggest tighter criteria, cheaper models, reduced turn budgets, harder prompts.

## Step 1: Determine Scope

The run layout is `runs/<run_id>/<variant_id>/<task_id>/<NN>/{task.json,task.log,artifacts/}`, where `<NN>` is a zero-padded replicate index (`00`, `01`, ...). Detect scope by inspecting the target path:

- **Single-replicate task scope**: path contains `task.json` directly (e.g., `.../hello_date/00/`) → analyze just this replicate.
- **Aggregate task scope**: path contains one or more replicate subdirs matching `??/task.json` (e.g., `.../hello_date/`) → analyze every replicate together in aggregate.
- **Variant scope**: path contains `variant.json` plus task subdirectories → cross-task analysis.
- **Run scope**: path contains `run.json` → full run analysis. If `experiment.json` also exists, this is an experiment comparison run.

Replicate count reflects the variant's `repeats:` setting (or `--repeats N`). When `repeats > 1`, the "aggregate task scope" branch is the normal case — there will be multiple `NN/` replicate dirs per task. When `repeats == 1` (default), there's exactly one `00/` dir per task and aggregate/single-replicate scopes produce equivalent output.

Read the appropriate files:
- **Single-replicate task scope**: Read `task.json`.
- **Aggregate task scope**: Read every `??/task.json` under the target dir. Build an aggregated view (see Step 2 below).
- **Variant scope**: Read `variant.json`, then read each `*/*/task.json` (the `*/*/` glob traverses `<task_id>/<NN>/`). For each `task_id` subtree, apply the aggregate-task-scope logic below.
- **Run scope**: Read `run.json` + `experiment.json` (if exists) + `experiment.md` (if exists). Then count the total number of task.json files across all variants (use `ls` or `Glob`).

**Small experiment shortcut** (total tasks across all variants <= 20): Read ALL `task.json` files directly. Skip the map-reduce approach — you have enough context to analyze everything in full. Pass full task data to sub-agents.

**Large experiment** (total tasks > 20): Use the map-reduce approach described in Step 2b. Read `experiment.json` as the primary cross-variant data source — it already contains aggregates, win rates, p-values, and score spreads. Do NOT recompute these — build analysis on top of them.

## Step 2: Analysis

### Task Scope — Launch 3 Sub-Agents in Parallel

For **single-replicate task scope**, read that replicate's `task.json` and use it directly.

For **aggregate task scope**, collect every replicate's `task.json` under the target dir and merge into an aggregated view: per-replicate arrays for `final_status`, `weighted_score`, `iteration_count`, `duration_seconds`, `total_cost_usd`, and any other per-run field, plus a union of `success_criteria_results` keyed by criterion `description`. When a field varies across replicates, include all values; when it's constant, include it once. If only one replicate exists, the arrays collapse to a single element — behavior matches the single-replicate case. Drive recommendations from the aggregate signal ("3/5 replicates failed criterion X"), not cherry-picked replicates.

After reading or aggregating `task.json`, determine whether the task **passed** (every replicate's weighted_score >= 0.9 AND every replicate's final_status == SUCCESS) or **failed** (any replicate violates either condition). Then launch 3 agents in parallel, passing each agent the relevant slice of the (possibly aggregated) task.json data.

**Agent 1 — Task Design (Axes 1, 2, 4)**

Give this agent the following data from task.json: `final_status`, `weighted_score`, `iteration_count`, `max_turns_exhausted`, `success_criteria_results`, `task_config.resolved` (specifically `initial_prompt`, `success_criteria`, `tags`, `max_iterations`), and `task_config.source_yaml`.

Instruct it to analyze:

Axis 1 — Outcome:
- If failed: Classify root cause as one of: prompt_gap, environment_issue, agent_error, config_issue, impossible_task. Which criteria failed and with what margins?
- If passed: Did all criteria score 1.0? If so, the task may not be discriminating enough. Could max_iterations or max_turns be lowered?

Axis 2 — Prompt Quality:
- If failed: Is the prompt missing context the agent needed? Does the prompt mention all required flags, paths, or identifiers? Does it match what criteria actually check?
- If passed: Is the prompt over-specified (hand-holding)? Could hints be removed to make the task harder? Is there unnecessary verbosity?

Axis 4 — Success Criteria Design:
- If failed: Are all criteria achievable given the environment and prompt? Compute sensitivity = `weight * (pass_threshold - score)` for each failed criterion — rank by highest ROI.
- If passed: Are criteria too lenient? (e.g., `file_exists` without content check, `file_contains` with only one string). Could criteria be more rigorous? Are there untested aspects?
- Both: Flag fragile passes (score exactly at threshold). Check for redundant criteria. Check for coverage gaps.

The agent must produce: a list of findings (each with axis, impact level, evidence from the data, recommendation, and YAML fix snippet), a suggested prompt rewrite (if applicable), and suggested criteria changes.

**Agent 2 — Agent Behavior (Axis 3)**

Give this agent: `turns` array (specifically each turn's `commands`, `files_changed`, `assistant_turn_count`, `duration_seconds`), `command_stats`, and `agent_config.max_turns`.

Instruct it to analyze:

Axis 3 — Agent Efficiency:
- Turn budget utilization: `total_assistant_turns` vs `max_turns`. How close to the limit?
- Command patterns: List the tool sequence. Count exploratory commands (`--help` calls). Count repeated/similar commands.
- Error recovery: After hitting an error, did the agent adapt or repeat the same pattern? Count consecutive similar errors (same tool + similar command). Flag "stuck in a loop" patterns.
- Time sinks: Which commands were slowest? What fraction of total time was tool execution vs thinking?
- File output timing: When did the agent first create/modify output files? Was it early (good) or only at the end (risky)?
- If passed: Could max_turns be lowered? (suggest `assistant_turn_count + small buffer`). Were there unnecessary exploratory commands?

The agent must produce: efficiency metrics (turns used/budgeted, commands total/failed, time in tools vs thinking), behavioral observations, and a recommended max_turns value.

**Agent 3 — Config, Environment & Cost (Axes 5, 6, 7)**

Give this agent: `task_config.lineage`, `agent_config`, `sdk_options`, `environment_info`, `total_token_usage`, `duration_seconds`, `total_assistant_turns`, and any commands from `turns` that had `result_status: "error"`.

Instruct it to analyze:

Axis 5 — Configuration:
- Lineage conflicts: For each entry in `lineage`, check if the `source` is not "task" — surface overrides where experiment-base/variant/CLI changed a value the task YAML defined. Show before/after.
- max_turns/max_iterations: Were they hit? Were they excessive?
- Model selection: Is the model appropriate for the task complexity? (Opus on a trivial task = wasted cost.)
- allowed_tools: Were all allowed tools actually used? Were needed tools missing from the list?

Axis 6 — Environment:
- Scan error commands for infrastructure issues: API failures, missing services, sandbox problems, CLI tool errors.
- If passed: Were there non-fatal warnings the agent worked around? Is the sandbox more permissive than needed?

Axis 7 — Cost & Performance:
- Token breakdown: input, output, cache_creation, cache_read. Compute cache hit rate = cache_read / (cache_creation + cache_read).
- Cost: total_cost_usd. Is it reasonable for what the task does?
- Duration vs timeout: How much headroom?
- If passed: Could a cheaper model achieve the same result? Compute cost-per-score-point.

The agent must produce: lineage conflict table (setting, value, source, task YAML value, impact), config tuning recommendations, environment issues, and cost analysis.

### Variant Scope

Count the number of **task directories** (immediate children of the variant dir, one per `task_id`). Do NOT count replicate dirs (`00/`, `01/`, ...) — they sit one level deeper. If **<= 20 tasks**, read all `??/task.json` files directly and apply aggregate-task-scope merging per task_id, then pass the full (aggregated) data to sub-agents (no map-reduce needed). If **> 20 tasks**, use map-reduce:

**Map phase**: For each task directory, aggregate every replicate's `task.json` (as described in the task-scope aggregation rules above) and extract a compact summary. Try `jq` first (concise and fast); if `jq` is not installed, fall back to `python3`:
```
{task_id, final_status, weighted_score, duration_seconds, total_cost_usd,
 total_tokens, assistant_turn_count, max_turns, iteration_count,
 max_turns_exhausted, model_used, criteria_count,
 failed_criteria: [{type, description, score}],
 all_criteria_perfect: bool}
```

Then for each task that **failed** (weighted_score < 0.9 or final_status != SUCCESS), run the full 3-agent task analysis above.

**Cross-task analysis** (both small and large): Launch a single agent with all task data (full or summarized) + `variant.json`. Instruct it to find:
- Efficiency ranking: order tasks by turns, duration, cost
- Common findings: patterns across multiple tasks (e.g., "3/5 tasks only use file_exists without content validation")
- If any tasks failed: failure clustering by status + dominant failing criterion type

### Run Scope — Experiment Analysis

For each variant, do the variant-scope analysis above. Then:

If `experiment.json` exists, launch an additional agent with:
- The full `experiment.json` content
- The `experiment.md` content (for reference, not to duplicate)
- All per-task compact summaries from both variants
- Per-task `total_cost_usd` from `run.json` task_results

Instruct this agent to produce:

1. **Experiment Summary Table**: Pull aggregates from experiment.json (scores, durations, p-values). ADD cost totals per variant (sum from run.json task_results).

2. **Efficiency Comparison**: Per-task table showing score + cost + duration for each variant. Compute cost-per-score-point per variant.

3. **Variant Recommendation**: One paragraph — "Pick <variant> because..." with tradeoff summary. Consider: score differences (and significance), cost ratio, speed ratio. If scores are tied, lead with efficiency.

4. **Task Difficulty Ranking**: From experiment.json `task_summaries`, use `score_spread` to rank tasks. Zero spread = not discriminating (either too easy or too hard for both). High spread = good discriminator.

5. **Failure Clusters** (if any): Group failed tasks across variants by root cause.

If no `experiment.json` exists (single-variant run), treat as variant scope.

## Step 3: Synthesize

After all sub-agents complete, combine their findings:

1. **Deduplicate**: If multiple agents flagged the same issue, keep the most detailed version and note it was flagged by multiple analyses.
2. **Rank by impact**: For failed tasks: estimated score improvement. For passing tasks: cost/time savings or criteria rigor improvement.
3. **Sensitivity ranking**: For failed criteria, sort by `weight * (pass_threshold - score)`.
4. **Group**: Split into "Quick Wins" (config change, threshold tweak, prompt clarification) vs "Structural Changes" (rewrite prompt, redesign criteria, fix environment).
5. **Evidence rule**: Drop any finding that doesn't cite specific data from the run.
6. **Already-fixed check**: For each recommendation that suggests changing a task YAML file, read the **current** file on disk (path is in `task_config.source_file`). Compare the current content against the run-time `task_config.source_yaml`. If the issue has already been fixed in the current file, mark the finding as "**Already fixed** in current codebase" and don't include it in Quick Wins or diffs. Don't recommend fixing something that's already fixed.
7. **Detect systemic patterns**: Before writing per-task findings, scan all failures for shared patterns. If one criterion type or root cause accounts for >50% of failures across tasks, report it as a **Systemic Pattern** — a single finding with a list of affected tasks — instead of repeating it per-task. Examples: "command_executed with pass_threshold=1.0 on data-dependent commands affects 8 tasks", "missing tenant data blocks all process-* tasks". Systemic patterns go at the top of the Findings section.
8. **Detect false negatives**: Cross-reference `file_exists`/`file_contains` results with `command_executed` results for the same task. If the agent **produced the expected output files** (file_exists/file_contains criteria pass) but `command_executed` criteria fail (agent used different commands than expected), flag this as a **likely criteria bug, not an agent failure**. The agent achieved the task's intent through an alternative path. Report these as "False Negative — agent succeeded but criteria didn't match" with a recommendation to broaden the command_executed pattern.
9. **Generate diff**: Compare original task YAML (from `task_config.source_yaml`) with recommended changes.
10. **Generate suggested YAML**: Produce a complete rewritten task YAML incorporating all recommendations. For variant scope with >5 tasks, produce suggested YAML only for the top 3 highest-impact fixable tasks (near-misses and single-criterion failures), not all tasks.

## Step 4: Write Report

Write the report as `analysis.md` **in the target directory** (the same path that was passed as the argument):

- Aggregate task scope (pointed at the task_id dir): `<target_path>/analysis.md` (e.g., `runs/2026-03-16_07-24-50/default/uipath-is-connections-simple/analysis.md`) — one analysis covering all replicates.
- Single-replicate task scope (pointed at a specific replicate dir like `.../hello_date/00/`): `<target_path>/analysis.md` inside the replicate dir.
- Variant scope: `<target_path>/analysis.md` (e.g., `runs/2026-03-16_10-10-29/sonnet/analysis.md`)
- Run scope: `<target_path>/analysis.md` (e.g., `runs/2026-03-16_10-10-29/analysis.md`)

## Output Format

### Task Scope Report

```markdown
# Run Analysis: <task_id> (<variant_id>)

## TL;DR
<2-3 sentences. For failures: what went wrong + top fix.
For passes: key optimization opportunity.>

## Score Breakdown
| Criterion | Weight | Score | Threshold | Status | Sensitivity | Note |
|-----------|--------|-------|-----------|--------|-------------|------|
| ...       | ...    | ...   | ...       | ...    | ...         | ...  |

*Sensitivity = weight x (threshold - score). For passing criteria: "fragile" if score == threshold.*

**Weighted Score**: X.XX / 1.00
**Final Status**: <status>
**Duration**: Xs ($X.XX)
**Turns Used**: X / Y

## Config Lineage Conflicts
| Setting | Value | Source | Task YAML Value | Impact |
|---------|-------|--------|-----------------|--------|
<Omit section entirely if no conflicts detected.>

## Findings

### 1. [impact: high] <Finding title>
**Axis**: <axis name>
**Evidence**: <specific data from the run>
**Recommendation**: <what to change>
**Fix**:
```yaml
# suggested change
```

### 2. [impact: medium] ...
<Continue for all findings, ordered by impact.>

## Quick Wins
1. ...

## Structural Changes
1. ...

## Recommended Changes (Diff)
```diff
- original line from task YAML
+ suggested replacement
```

## Suggested Task YAML
```yaml
<Complete rewritten task YAML>
```
```

### Variant Scope Report (additional sections after findings)

```markdown
## Cross-Task Patterns

### Efficiency Ranking
| Task | Score | Turns Used/Max | Duration | Cost | Notes |
| ...  | ...   | ...            | ...      | ...  | ...   |

### Common Findings
<Patterns across multiple tasks>

### False Negatives
<Tasks where the agent produced correct output but failed command_executed criteria due to
using alternative (but functionally equivalent) commands. These are criteria bugs, not agent failures.>

| Task | Score | What agent did right | What criteria rejected | Fix |
| ...  | ...   | ...                  | ...                   | ... |

*Omit if no false negatives detected.*

### Systemic Patterns
<Criterion types or root causes that account for >50% of failures across tasks.
Report once here instead of repeating per-task.>

*Omit if no systemic patterns detected.*
```

### Run Scope with Experiment (additional sections)

```markdown
## Experiment Summary

*Aggregates from experiment.json.*

| Metric | <variant_a> | <variant_b> | Delta | p-value |
|--------|-------------|-------------|-------|---------|
| Score (mean +/- std) | ... | ... | ... | ... |
| Duration (mean) | ... | ... | ... | ... |
| Cost (total) | ... | ... | ... | — |
| Tokens (total) | ... | ... | ... | — |

Win rates: <variant_a>: X/N, <variant_b>: Y/N, Ties: Z/N

## Efficiency Comparison

| Task | <variant_a> Score | <variant_a> Cost | <variant_b> Score | <variant_b> Cost | Winner |
|------|-------------------|------------------|-------------------|------------------|--------|
| ...  | ...               | ...              | ...               | ...              | ...    |

**Cost per score point**: <variant_a>: $X.XX, <variant_b>: $X.XX

## Variant Recommendation

<One paragraph: "Pick <variant> because..." with tradeoff summary.>

## Cross-Task Patterns

### Task Difficulty
| Task | Score Spread | <variant_a> | <variant_b> | Discriminating? |
| ...  | ...          | ...         | ...         | ...             |

### Failure Clusters
<Omit if no failures.>
| Cluster | Tasks | Root Cause | Fix |
| ...     | ...   | ...        | ... |
```

## Principles

- **Evidence-based**: Every finding must cite specific data (command output, score, config value, timing).
- **Actionable**: Every recommendation must include a concrete fix (YAML snippet, config change, prompt rewrite).
- **Dual lens**: Always consider both diagnose (failures) and optimize (passes). A passing run still has room for improvement.
- **Don't duplicate**: For experiment runs, use `experiment.json` aggregates. Don't recompute p-values or win rates.
- **Impact over severity**: Rank by "how much would this fix improve the outcome" not by abstract severity.
- **No silent omissions**: If you omit a section defined in the output format (Score Breakdown, Config Lineage Conflicts, Failure Clusters, etc.), include a one-liner explaining why. For example: "*Score Breakdown omitted — all criteria scored 1.0 across all tasks.*" or "*Config Lineage Conflicts omitted — no overrides detected that impacted results.*" Never silently skip a section.
- **Statistical honesty**: If total tasks per variant is less than 8, add a prominent warning in the Experiment Summary: "*Note: With only N tasks per variant, this sample is too small for statistically meaningful conclusions. p-values are shown for reference but should not be used for decision-making. A minimum of 8-10 tasks per variant is recommended.*"
- **Don't recommend what's already fixed**: Always check the current task YAML on disk before recommending a change. If the file has been updated since the run, say so and move on.
- **Systemic over repetitive**: When one root cause or criterion pattern dominates failures across multiple tasks, report it once as a systemic pattern with a list of affected tasks. Don't produce N copies of the same finding for N tasks.
- **False negatives are criteria bugs**: When the agent produces correct output files but fails command_executed checks because it used different (but functionally equivalent) commands, that's a criteria problem, not an agent problem. Flag these clearly so the user knows to fix criteria, not the agent or prompt.
