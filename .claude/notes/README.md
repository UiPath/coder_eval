# Architecture notes

Design rationale moved out of `CLAUDE.md` and out of `src/` docstrings, so both stay
working references rather than changelogs. Nothing here is deleted history.

**These notes are NOT auto-loaded into context.** Read the file for a subsystem when you
touch it — each entry explains *why* the design is shaped the way it is, and most of them
are written around a specific shipped defect.

Authoritative sources, when a note and the code disagree: the code wins, then the lint
rule docstrings in `tests/lint/rules/`, then the guides under `docs/`.

## Contents

- [agents.md](agents.md) — agent adapters, the turn lifecycle, token reconciliation, harness parity
- [contracts.md](contracts.md) — criteria, datasets, aggregation, judging
- [isolation.md](isolation.md) — the docker driver, the sandbox, detached grading
- [lint-rules.md](lint-rules.md) — why each CE lint rule exists
- [orchestration.md](orchestration.md) — config merge, resume, early stop, execute vs. run
- [permissions.md](permissions.md) — the chmod window and the reference anti-cheat
- [persistence.md](persistence.md) — atomic writes and judge persistence
- [reporting.md](reporting.md) — reports, pricing, harbor, telemetry, the plugin and Action
- [timing.md](timing.md) — the turn clock and the single subtraction seam

## What belongs here, and what stays in the source

Every paragraph in a docstring or comment sorts into exactly one bucket:

| Bucket | Test | Action |
|---|---|---|
| **CONTRACT** | A caller must know it to call correctly: what it returns, what it mutates, what it raises, an invariant they must maintain. | **Keep** in the source, compressed to its claim. |
| **HAZARD** | A future editor breaks something if they do not know it. Reads as "do not X without Y" — a coupling between two distant places. | **Keep** in the source, 1–3 lines, stating the coupling only. |
| **RATIONALE** | Why the design is this shape; what was considered and cut; what defect motivated it; what was verified experimentally. | **Move** here, to `<subsystem>.md`. |
| **HISTORY** | "used to", "no longer", "previously", "an earlier revision", "shipped once as". | **Delete.** Git holds it. |
| **CROSS-MODULE CLAIM** | Asserts a current property of a different module or harness. | **Delete**, replaced by a link to that module's SSOT. |

## The pointer line

Moved rationale leaves exactly one line behind, at the end of the docstring or comment
block it came from:

```
Rationale: .claude/notes/timing.md § subtract_tool_time
```

Path relative to the repo root, then `§`, then the target `##` heading text verbatim.
There is no other accepted form: `tests/lint/prose_budget.py` parses this one and fails
`make docs-budget` when the file or the heading does not exist.

## The prose budget is not a lint rule

`make docs-budget` runs `tests/lint/prose_budget.py` over `src/coder_eval` and `tests`.
It enforces two bars with no baseline to maintain: no docstring over 150 prose words (an
`@abstractmethod` and the Typer commands are exempt), and no own-line comment run over 8
lines. It also fails when a `Rationale:` pointer does not resolve or is not the last prose
line of its block.

Neither bar grants a per-file allowance, and that is the point. The run cap replaced a
`MAX(20, 0.15 × lines)` budget on a file's TOTAL comment lines, which was inverted: it
blocked `isolation/docker_runner.py` at 229/229 for carrying 62 short annotations, while a
16-line essay in `tests/test_regrade.py` sat at 29% of its budget. Four files had settled
at exactly 100% of the cap — the budget had stopped being a ceiling and become a target.

It is deliberately **not** a `CE` rule: `tests/lint/rules/` polices per-pattern invariants
one AST at a time, while this measures prose across whole trees. Making it a rule would
mean a rule class, a rule test and a catalogue entry — enlarging the harness the budget
exists to shrink. Do not "fix" this by promoting it.

Nothing here states how many `CE` rules exist. `tests/lint/rules/` owns that count, and a
number written down anywhere else is a second declaration that will be wrong.

## Where a CE rule's rationale lives

A rule's invariant, scope and blind spots live in its rule file under `tests/lint/rules/`
(or its `@pytest.mark.lint` class in `tests/test_custom_lint.py`). Read that before you
edit, suppress or widen a rule. The defect that motivated it lives in
[lint-rules.md](lint-rules.md), under the rule's id. When the two disagree, the rule file
is correct.
