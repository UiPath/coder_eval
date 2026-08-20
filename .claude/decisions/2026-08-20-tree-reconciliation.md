# Reconciling a run tree against its own `run.json`

Subject: `optimize/load.py::reconcile_tree_against_run_json`, `reconcile_arms`, `stale_tree_reason`,
`wrong_path_reason`, `TASK_JSON_GLOB`, `_rules_verdicts`, `rule_row_map`.

## The failure it exists for

`run.json` is written per INVOCATION; the row tree is APPEND-ONLY. So a re-used `--run-dir` leaves an
earlier call's rows on disk — or, with a smaller `--repeats`, its extra replicates — while
`row_selection` is rewritten to describe only the latest invocation. Everything then loads, parses
and pools without error, and the numbers are computed over a row set no invocation ran.

It is silent in every consumer: a contaminated tree produces a confident result. Measured on dirs the
activation gate correctly refuses, four other readers of the same trees returned numbers — the noise
floor over an extra pooled row, and the Stage A vectors with the stale row included.

## Why the glob is declared once

`TASK_JSON_GLOB` is this module's single declaration, and `task_json_pattern` derives all four
wrong-path messages from it. Changing the glob otherwise leaves three messages describing a tree the
code no longer searches — and those messages are the whole diagnostic for the family's documented
silent-zero failure mode.

The replicate directory's NAME is spelled once too, in `path_utils.replicate_subdir_name` (CE042).
The day that padding widens, nothing raises: the globs simply match nothing, both gates load ZERO
rows, and the zero-row message then blames a wrong variant id, a wrong suite id or a wrong run
directory — the three things that would be correct.

## Three-valued, not boolean

`unrecorded` (results the run.json never wrote) and `unknown` (a dir whose provenance cannot be read)
are different findings with different remedies: the first is contamination and refuses, the second is
an old run dir and notes. Collapsing them to "not clean" would make every pre-`row_selection` run
unmeasurable.

## The rules line is an attribution, not a score

`_rules_verdicts` reads the grader's `RULES ` line — the LAST line of stdout, compact JSON of rule id
to `"pass" | "fail" | "na"`. It is what lets a reader see WHICH check moved, beside a
`weighted_score` that only says the blend moved. A malformed line costs no score: the float on line 1
is the measurement and the attribution is optional by construction.
