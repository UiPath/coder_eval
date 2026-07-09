# Multi-model code review (shared procedure)

The parallel multi-model review used by `coder-eval-code-review` and the final
review of `coder-eval-implement-plan`. Each consumer supplies its own **scope**
(which files) and **`content`** (what to look for, which rubric to cite); this
file owns the *mechanics* so the invocation — and its non-obvious gotchas — live
in one place.

Run two reviews **in parallel**:

## Review A — Multi-model via MCP

If `mcp__multi__codereview` is available (check the deferred-tools list; if listed
but not loaded, load it with `ToolSearch` → `select:mcp__multi__codereview`), call
it with:

- `models`: the strongest available (e.g. `["gemini-3", "codex"]`)
- `relevant_files`: absolute paths of every changed / in-scope file
- `content`: the review request, citing the consumer's rubric (the shared **Review Criteria**, the **Severity Standard**, etc.)
- `base_path`: project root
- `step_number: 1`, `next_action: "stop"`

**Multi-step protocol — easy to get wrong:** step 1 usually returns a checklist
with `status: in_progress`, **not** findings. If so, make a follow-up call with
`step_number: 2`, the **same `thread_id`**, your cumulative `issues_found`, and
`next_action: "stop"` to actually dispatch the models. Do not mistake the step-1
checklist for the result.

**Fallback:** if `mcp__multi__codereview` is not available, launch two additional
Opus sub-agents (`Agent`, `model: "opus"`) instead, each reviewing against the
same rubric.

## Review B — Opus sub-agent

`Agent`, `model: "opus"`. Read every in-scope file in full; evaluate against the
consumer's rubric; return structured findings (`severity · file:line ·
description`), positive observations, and an overall assessment. A consumer may
enrich this with task-specific lenses (e.g. a git-blame/historical pass, a
CLAUDE.md-compliance pass) — those additions stay in the consuming command.

## Synthesize

Merge into one report; dedupe; note issues multiple reviewers flagged
independently (higher confidence); discard false positives and anything that
contradicts the rubric.
