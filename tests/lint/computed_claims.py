"""CE039 — a prose surface's arithmetic must be CHECKED BY COMPUTING IT, not by matching text.

The plugin carries ~50 prose sensors and every one of them asserts a string is **present**.
None asserts that what it says is **true**, and two false claims shipped past all of them in a
single change: *"the statistic cannot be computed from one run dir"* (it can — only the MDE and
the per-invocation diagnostic cannot), and a successive-halving cost "saving" that is
arithmetically a **premium**. Both read plausibly, both passed every token check, and both were
caught by a human re-deriving the arithmetic.

``test_optimize_skill_snippet_names_the_public_gate_api`` is the one existing sensor that checks a
claim against the code — it imports the module and asserts every name the skill tells a user to
import really exists. This module generalizes that shape into a registry, and adds the rule that
makes it a sensor *class* rather than three more bespoke sensors: **an arithmetic-bearing table in
the optimize surfaces that no registered claim names is a failure.** Without the coverage rule a
new table ships unchecked and nothing says so.

Three design choices, each load-bearing:

* **``covers`` sits ON the claim**, not in a separate ``surface -> tables`` map beside ``CLAIMS``.
  A separate map is two edits that can disagree — the same shape ``_floor_key``'s derivation from
  ``NoiseFloor.model_fields`` exists to prevent. The coverage check derives its map from the
  registry.
* **One ``check(text, tmp)`` signature**, not a ``TextClaim`` / ``BehaviouralClaim`` pair. The
  ``tmp`` argument lets a behavioural claim build a run-directory fixture and RUN the code; the
  two text claims ignore it. A second dataclass and a union for one call site is ceremony.
* **A restricted ``ast`` walk, never ``eval()``.** These expressions come from a checked-in file in
  this repository, so the risk is theoretical — but the walk costs ten lines and removes the
  question, and it gives a failure message naming the offending node instead of a traceback.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof.** The coverage rule
demands a claim for **arithmetic-bearing tables** only — a table whose BODY carries a backticked
span holding ``*``, ``+``, ``/`` or ``ceil(`` (see ``_ARITHMETIC_SPAN``). Everything else in these
surfaces is outside it: a claim made in prose rather than in a table, a table of stages or file
names, and — the concrete miss — a sentence that has lost half its words. The clause *"Read that
front with the arms you are actually choosing between: the emptied-body / any arm that is cheap
because it does less…"* shipped in ``12eee85`` with an orphaned fragment mid-sentence, survived
three commits and ~50 token sensors, and was found by a human reading it: it deleted no token, so
no presence sensor could see it, and it carries no arithmetic, so this rule could not either. CE039
checks that a table's numbers are TRUE; nothing here checks that the prose parses as English.

The evaluator's whitelist **is** its dispatch: :data:`_BINARY_OPS` and :data:`_UNARY_OPS` map an
operator type to the function that computes it, so admitting an operator and implementing it are one
edit and cannot disagree. They used to be two — a tuple of allowed types plus a ``match`` — and a
wildcard arm returning a value would then have computed an unhandled operator as something else,
which is how ``ast.Mod`` in the whitelist would have been reported as *division* by the one sensor
class whose entire purpose is catching arithmetic that lies. **CE044** existed to pin the parity
between those halves and is RETIRED, because there is no longer a parity to keep: see
``.claude/harness-candidates.md``.

Like CE026-CE031 and CE033-CE038 this is **not** a ``BaseRule`` in ``tests/lint/runner.py`` (that
runner is an AST walk over ``src/**/*.py``); it reasons over Markdown and is wired as
``tests/test_custom_lint.py::TestCE039ComputedClaims``.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Re-exported: the pipe-table parser moved to its own module on its second consumer
# (`estimator_ledger.py`). Importing the names back keeps every reader of this module — and
# every claim below — spelling one name for one parser.
from tests.lint.markdown_tables import MarkdownTable, parse_markdown_tables, table_signature


PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "coder-eval"
METHOD = PLUGIN_ROOT / "reference" / "optimize-method.md"
SKILL = PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md"

# The two surfaces CE039 demands coverage of. Both are read by an agent that spends money on what
# they say, and both carry arithmetic a reader budgets from.
COVERED_SURFACES = (METHOD, SKILL)


@dataclass(frozen=True)
class ComputedClaim:
    """One numeric or behavioural claim a prose surface makes, checked by COMPUTING it."""

    id: str  # stable kebab-case id
    surface: Path  # the file whose claim this is
    why: str  # what shipped false, or what would
    covers: tuple[str, ...]  # header signatures of the tables this claim checks
    check: Callable[[str, Path], list[str]]  # (raw surface text, a tmp dir) -> failures; [] holds


# --- the typographic normalizer the surfaces need ----------------------------


# Written as escapes rather than literally: ruff's RUF001 flags these as ambiguous in source, and
# it is right to — they are indistinguishable from `x` and `-` at a glance, which is precisely why
# the prose has to be normalized before anything tries to parse it.
TIMES = "\u00d7"  # MULTIPLICATION SIGN, as optimize-method.md writes it
MINUS = "\u2212"  # MINUS SIGN
EN_DASH = "\u2013"


def _ascii(text: str) -> str:
    """Normalize the typographic operators both surfaces use into ASCII.

    ``optimize-method.md`` writes multiplication as U+00D7 and subtraction as U+2212. Parsing
    would fail on both, and "the formula does not parse" is indistinguishable from "someone
    deleted the formula" — which is a real failure this must not swallow.
    """
    return text.replace(TIMES, "*").replace(MINUS, "-").replace(EN_DASH, "-")


# A backticked span holding an arithmetic operator or a ceil() call. `M_train/2` and
# `(N+1) * M_train` (after normalization) both match; `metrics["f1.yes"]` and `--split train` do not.
_ARITHMETIC_SPAN = re.compile(r"`([^`]*(?:[*+/]|ceil\()[^`]*)`")


def _spans(cell: str) -> list[str]:
    """Every arithmetic-bearing backticked span in a cell, in order.

    A cell may carry more than one — the control-arm row prices the arm and the pair it is
    compared against — so the expected side has to declare both, in order.
    """
    return [m.group(1).strip() for m in _ARITHMETIC_SPAN.finditer(_ascii(cell))]


def arithmetic_tables(text: str) -> list[MarkdownTable]:
    """The tables CE039 requires coverage of: any whose BODY carries an arithmetic span.

    Deliberately tight. A table of stages, tracks or file names carries no arithmetic and demanding
    a computed claim for it would make the rule noisy — which is how a coverage gate stops being
    read. The count is pinned by a test so a widened predicate is a visible diff.
    """
    return [t for t in parse_markdown_tables(text) if any(_spans(cell) for row in t.rows for cell in row)]


# --- the restricted expression evaluator -------------------------------------

_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Call, ast.Load)

# The whitelist AND the dispatch, in one declaration each. An operator is allowed exactly because
# there is a function here to compute it, so widening is one edit and the two cannot drift.
#
# TWO dicts rather than one, because `ast.USub` is a `unaryop` and the rest are `operator`s: a single
# dict keyed on both would need an arity tag beside each entry, which is the second declaration
# returning under another name.
_BINARY_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {ast.USub: operator.neg}


def evaluate_expression(src: str, env: dict[str, float]) -> float:
    """Evaluate one cost-table expression against a symbol assignment.

    A restricted ``ast`` walk rather than ``eval()``: ten lines buys a whitelist and a failure
    message that names the offending node type or the unbound symbol, instead of a ``NameError``
    surfacing from three frames down where nobody can tell prose drift from a parser bug.
    """
    tree = ast.parse(_ascii(src).strip(), mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, ast.operator | ast.unaryop):
            # `ast.walk` yields the operator as its own node, so the whitelist has to admit the
            # allowed ones here rather than only via their parent BinOp/UnaryOp. Membership in the
            # dispatch tables IS the whitelist — there is nothing else to keep in step with them.
            if type(node) not in _BINARY_OPS and type(node) not in _UNARY_OPS:
                raise ValueError(f"{src!r} uses the disallowed operator {type(node).__name__}")
            continue
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id == "ceil"):
                raise ValueError(f"{src!r} calls something other than ceil()")
            continue
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"{src!r} contains a disallowed {type(node).__name__} node")

    def _compute(node: ast.AST) -> float:
        match node:
            case ast.Expression():
                return _compute(node.body)
            case ast.Constant(value=value) if isinstance(value, int | float):
                return float(value)
            case ast.Name(id=name):
                if name not in env:
                    raise ValueError(f"{src!r} references {name!r}, which is not one of {sorted(env)}")
                return env[name]
            case ast.UnaryOp(op=op, operand=operand):
                return _UNARY_OPS[type(op)](_compute(operand))
            case ast.Call(args=[arg]):
                return float(math.ceil(_compute(arg)))
            case ast.BinOp(left=left, op=op, right=right):
                # The lookup cannot miss: the walk above admitted this operator BECAUSE it is a key
                # here. That is the whole point of collapsing the two halves.
                return _BINARY_OPS[type(op)](_compute(left), _compute(right))
        raise ValueError(f"{src!r} contains an unevaluatable {type(node).__name__} node")

    return _compute(tree)


# --- claim 1: the cost table's INVARIANTS -----------------------------------

_COST_TABLE_SIGNATURE = "Spend | Runs"

# Every row the cost table is allowed to have. Matched by EXACT label — a dict lookup, never a
# prefix or substring test: "Stage A — triage" is a strict prefix of "Stage A — triage, halved …",
# so a prefix matcher silently checks the halved row's formula against the flat row's invariant and
# passes. That is a bug in the matcher masquerading as a green sensor, and exact lookup makes it
# unrepresentable. A row here with no parseable formula, or a row in the table that is not here,
# both fail — so the checked set and the rendered set cannot drift apart.
_COST_ROWS = (
    "Step 6 baseline",
    "Control arm — execution track, **once per suite**",
    "Stage A — triage",
    "Stage A — triage, halved (an abandon point, NOT a saving)",
    "Search loop — one round, rounds 2+",
    "Stage B — gate, activation track",
    "Stage B — gate, execution track",
    "Stage C — confirm",
)


def _cost_rows(text: str) -> dict[str, list[str]]:
    """Row label -> its Runs cell's arithmetic spans, for the one table this claim covers."""
    for table in parse_markdown_tables(text):
        if table_signature(table) == _COST_TABLE_SIGNATURE:
            return {row[0]: _spans(row[1]) for row in table.rows if len(row) >= 2}
    return {}


def _check_cost_table(text: str, _tmp: Path) -> list[str]:
    """The cost table, checked against INVARIANTS OF THE COST MODEL rather than a retyped copy.

    A mirror of the table's own formulas would only detect that someone edited it; it could never
    detect that the table is WRONG, which is the whole point of CE039. So each invariant follows
    from what the stages actually do, and each can fail on a genuinely mistaken table.
    """
    rows = _cost_rows(text)
    failures: list[str] = []
    if not rows:
        return [f"the cost table ({_COST_TABLE_SIGNATURE}) is gone or changed shape — nothing to check"]

    # A row whose formula was deleted or turned into prose is a FAILURE, not a skip.
    missing = sorted(label for label in _COST_ROWS if not rows.get(label))
    if missing:
        failures.append(f"cost-table rows with no parseable Runs expression: {missing}")
    unknown = sorted(set(rows) - set(_COST_ROWS))
    if unknown:
        failures.append(
            f"cost-table rows no invariant covers: {unknown}. Add one to _COST_ROWS with the "
            "property it must satisfy — an unchecked row is the drift this claim exists to catch."
        )
    if missing:
        return failures

    def _at(label: str, index: int, **env: float) -> float:
        return evaluate_expression(rows[label][index], env)

    try:
        failures += _cost_invariants(rows, _at)
    except ValueError as exc:
        # A cell that PARSES but references an unexpected symbol (the `M_tune`/`M_holdout` class of
        # drift) reaches the evaluator and raises. Reported as a failure with the symbol named,
        # never as a traceback: a lint rule that crashes on the very drift it exists to catch reads
        # as a broken sensor rather than as a wrong table.
        failures.append(f"a cost-table expression could not be evaluated — {exc}")
    return failures


def _cost_invariants(rows: dict[str, list[str]], _at: Callable[..., float]) -> list[str]:
    """The invariants themselves, split out so the caller owns the one place they can raise."""
    failures: list[str] = []

    # 1. Halving NEVER saves. v1's error #7, and the only honest way to sensor it is to compute it.
    for n in range(2, 33):
        flat = _at("Stage A — triage", 0, N=n, M_train=1.0)
        halved = _at("Stage A — triage, halved (an abandon point, NOT a saving)", 0, N=n, M_train=1.0)
        if halved < flat:
            failures.append(f"the halved Stage A row is CHEAPER than the flat one at N={n} ({halved} < {flat})")
            break

    # 2. Stage B (activation) is 3x Stage A per arm — the three separate invocations, priced.
    for count in (1, 3, 5):
        stage_a = _at("Stage A — triage", 0, N=count, M_train=1.0)
        stage_b = _at("Stage B — gate, activation track", 0, S=count, M_train=1.0)
        if stage_b != 3.0 * stage_a:
            failures.append(
                f"Stage B (activation) is {stage_b} against Stage A's {stage_a} at N=S={count}; "
                "it runs the same arm set through THREE separate invocations, so the ratio is 3"
            )
            break

    # 3. The control arm's paired figure is exactly twice its unpaired one — one arm against a pair.
    control = "Control arm — execution track, **once per suite**"
    if len(rows[control]) < 2:
        failures.append(f"the {control!r} row no longer prices both the arm and the pair")
    else:
        alone, paired = _at(control, 0, M_train=1.0), _at(control, 1, M_train=1.0)
        if paired != 2.0 * alone:
            failures.append(f"the control row prices the pair at {paired} against {alone} for the arm alone (want 2x)")

    # 4. Stage B (execution) and Stage C carry the same coefficient on DIFFERENT split symbols —
    #    both are 2 arms x 3 repeats. The symbol half is what catches the M_tune/M_holdout class of
    #    drift that a name-only sensor catches only by accident.
    #
    #    Symbols FIRST, and then the arithmetic with both bound. A wrong symbol would otherwise
    #    raise out of the evaluator before the check that names it precisely ever ran, and the
    #    caller's catch-all would report "could not be evaluated" for a defect this can describe.
    for label, symbol in (("Stage C — confirm", "M_test"), ("Stage B — gate, execution track", "M_train")):
        if symbol not in _ascii(rows[label][0]):
            failures.append(f"the {label!r} row no longer references {symbol}")
    if "M_train" in _ascii(rows["Stage C — confirm"][0]):
        failures.append("Stage C is priced in M_train — it confirms on the TEST split, and that is its whole point")

    both = {"M_train": 1.0, "M_test": 1.0}
    b_exec = _at("Stage B — gate, execution track", 0, **both)
    c = _at("Stage C — confirm", 0, **both)
    if b_exec != c:
        failures.append(f"Stage B (execution) is {b_exec} and Stage C is {c}; both are 2 arms x 3 repeats")

    # 5. A search round is strictly cheaper than the SMALLEST possible Stage A (N=1: incumbent
    #    plus one candidate). The plausible wrong edit is to co-run the incumbent "for a fair
    #    comparison", which doubles the phase and deletes its reason to exist.
    stage_a_floor = _at("Stage A — triage", 0, N=1, M_train=1.0)
    search = _at("Search loop — one round, rounds 2+", 0, M_train=1.0)
    if search >= stage_a_floor:
        failures.append(
            f"a search round costs {search} against Stage A's floor of {stage_a_floor} at one "
            "candidate; the search loop runs ONE variant and reads the lineage head's recorded score"
        )

    return failures


# --- claim 2: the halving premium table -------------------------------------

_HALVING_SIGNATURE = "arms `A` | flat | halved | premium"


def _coefficient(cell: str) -> float:
    """The numeric coefficient a halving-table cell states, with ``**none**`` reading as 0.0."""
    text = _ascii(cell).strip()
    if "none" in text.lower():
        return 0.0
    spans = _spans(text)
    return evaluate_expression(spans[0], {"M_train": 1.0}) if spans else float(text.strip("`"))


def _check_halving_premium(text: str, _tmp: Path) -> list[str]:
    """The halving table, RECOMPUTED — plus the standing claim that halving never saves.

    ``halved = A/2 + ceil(A/2)`` against ``flat = A``, so ``premium = ceil(A/2) - A/2``: zero at an
    even arm count and a half at an odd one. The first draft of this prose claimed a SAVING, which
    is the defect the recomputation exists to catch.
    """
    table = next((t for t in parse_markdown_tables(text) if table_signature(t) == _HALVING_SIGNATURE), None)
    if table is None:
        return [f"the halving table ({_HALVING_SIGNATURE}) is gone or changed shape — nothing to check"]

    failures: list[str] = []
    for row in table.rows:
        if len(row) < 4:
            failures.append(f"the halving row {row!r} does not carry all four columns")
            continue
        arms = float(_ascii(row[0]).strip("` "))
        want = {"flat": arms, "halved": arms / 2 + math.ceil(arms / 2), "premium": math.ceil(arms / 2) - arms / 2}
        for column, cell in zip(("flat", "halved", "premium"), row[1:4], strict=True):
            got = _coefficient(cell)
            if got != want[column]:
                failures.append(f"halving table A={arms:g}: {column} reads {got:g}, recomputes to {want[column]:g}")

    # The standing claim, over a range the table does not enumerate.
    for arms in range(2, 33):
        if (math.ceil(arms / 2) - arms / 2) < 0:
            failures.append(f"halving would SAVE at A={arms} — the surrounding prose says it never does")
            break
    return failures


# --- claim 3: the pre-spend sizing table -------------------------------------

_SIZING_SIGNATURE = "survivors gated `S` | Holm threshold | discordant rows needed at 8 paired rows | at 20"

# The row counts the last two columns are computed at, pulled out of the HEADER rather than typed
# here: the header is what a reader budgets against, so if someone re-columns the table to 12 and 30
# the claim must follow them there instead of silently checking the old pair.
_ROW_COUNT = re.compile(r"(\d+)")


def _check_sizing_table(text: str, _tmp: Path) -> list[str]:
    """The skill's pre-spend sizing table, RECOMPUTED from the gate's own helper.

    Every cell is arithmetic: the threshold column is ``alpha/S`` evaluated against
    ``DEFAULT_ALPHA``, and the two count columns are :func:`min_discordant_rows` at the row counts
    the header names. So a stale figure here — the kind that ships when the floor's estimator moves
    — is a failure rather than a plausible-looking number a user sizes a suite from.

    It also checks the STANDING claim the prose makes beside the table ("four to five rows,
    essentially regardless of suite size"), over a range the table does not enumerate. A mirror of
    the table alone could only detect an edit; this can detect that the table is wrong.
    """
    from coder_eval.optimize.activation import min_discordant_rows
    from coder_eval.reports_stats import DEFAULT_ALPHA

    table = next((t for t in parse_markdown_tables(text) if table_signature(t) == _SIZING_SIGNATURE), None)
    if table is None:
        return [f"the sizing table ({_SIZING_SIGNATURE}) is gone or changed shape — nothing to check"]

    row_counts = [int(m.group(1)) for cell in table.header[2:] if (m := _ROW_COUNT.search(cell))]
    if len(row_counts) != len(table.header) - 2:
        return [f"the sizing table's count columns no longer name their row counts: {table.header[2:]}"]

    failures: list[str] = []
    for row in table.rows:
        if len(row) != len(table.header):
            failures.append(f"the sizing row {row!r} does not carry every column")
            continue
        survivors = float(_ascii(row[0]).strip("` "))
        spans = _spans(row[1])
        if not spans:
            failures.append(f"the S={survivors:g} row states its Holm threshold as prose, not as `alpha/S`")
            continue
        # Symbolic, so the claim binds it to DEFAULT_ALPHA rather than to a retyped 0.05.
        threshold = evaluate_expression(spans[0], {"alpha": DEFAULT_ALPHA})
        if threshold != DEFAULT_ALPHA / survivors:
            failures.append(f"sizing table S={survivors:g}: the threshold cell evaluates to {threshold}, not alpha/S")
            continue
        for cell, n_rows in zip(row[2:], row_counts, strict=True):
            want = min_discordant_rows(n_rows, threshold)
            got = _ascii(cell).strip("` ")
            if got != (str(want) if want is not None else "none"):
                failures.append(f"sizing table S={survivors:g} at {n_rows} rows reads {got!r}, recomputes to {want}")

    # The standing claim, over sizes the table does not list. If this ever fails the PROSE is what
    # needs rewriting — the number stopped being flat in the row count.
    across = {min_discordant_rows(m, DEFAULT_ALPHA / s) for m in range(8, 25) for s in (1, 3, 5)}
    if not across <= {3, 4, 5}:
        failures.append(
            f"the requirement is no longer 'four to five rows regardless of suite size' — it ranges "
            f"over {sorted(x for x in across if x is not None)} across 8-24 rows and 1-5 survivors"
        )
    return failures


# --- claim 4: the behavioural one -------------------------------------------


def _check_interval_from_one_run_dir(text: str, tmp: Path) -> list[str]:
    """The prose says the INTERVAL alone could come from one run dir, but not the MDE. Run it.

    This is the claim whose FALSE version shipped — "the statistic cannot be computed from one run
    dir" — past every token sensor, because the tokens were all still there. The only way to check
    it is to build the fixture and call the code.
    """
    from tests.optimize_fixtures import SUITE, eval_result, write_row

    failures: list[str] = []
    if "could be computed from a" not in " ".join(text.split()):
        failures.append(
            "the method file no longer says the interval COULD be computed from a single run "
            "directory — the false version of this claim ('the statistic cannot be') shipped once"
        )

    from coder_eval.optimize.activation import activation_gate

    def _gate(run_dirs: list[Path]):
        return activation_gate(
            incumbent_run_dirs=run_dirs,
            candidate_run_dirs=run_dirs,
            incumbent_variant="incumbent",
            candidate_variant="candidate",
            suite_id=SUITE,
            criterion_index=0,
            n_resamples=200,
        )

    # One run dir, three replicates per row: enough for the interval, no null comparison for the MDE.
    one = tmp / "one" / "run-0"
    for replicate in range(3):
        for row in range(4):
            observed = "yes" if (row + replicate) % 3 else "no"
            write_row(one, "incumbent", f"r{row}", eval_result(f"r{row}", [("yes", observed)]), replicate)
            write_row(one, "candidate", f"r{row}", eval_result(f"r{row}", [("yes", "yes")]), replicate)
    single = _gate([one])
    if single.ci_low is None:
        failures.append("the interval was NOT computable from one run directory, but the method file says it is")
    if single.mde is not None:
        failures.append("an MDE came back from one run directory, but the method file says a null split needs two")

    # Two run dirs: the null comparison exists, so the MDE does too.
    two = [tmp / "two" / f"run-{i}" for i in range(2)]
    for i, run_dir in enumerate(two):
        for row in range(4):
            observed = "yes" if (row + i) % 3 else "no"
            write_row(run_dir, "incumbent", f"r{row}", eval_result(f"r{row}", [("yes", observed)]))
            write_row(run_dir, "candidate", f"r{row}", eval_result(f"r{row}", [("yes", "yes")]))
    if _gate(two).mde is None:
        failures.append("no MDE from TWO run directories — the method file prices the second invocation for exactly it")
    return failures


def _check_execution_sign_resolution(text: str, tmp: Path) -> list[str]:
    """The method file says the gate resolves the subtraction's sign. Run it and see.

    The sibling of ``interval-from-one-run-dir``, for the other track. Presence first — the
    sentence must still be there — then the behaviour, because a sentence nobody deleted can still
    describe code that has stopped doing it.

    This claim's job is to bind the PROSE to the behaviour. ``TestExecutionGateSign``
    (``tests/test_optimize_execution.py``) is the unit-level guarantee and asserts the same three
    things deliberately: a weaker check here would not prove the sentence. Neither is redundant
    with the other — delete the unit test and the behaviour is unpinned; delete this and
    ``optimize-method.md`` can be reworded into a lie about it, which is exactly what the file
    warns costs you a promotion of the arm that LOST.
    """
    from tests.optimize_fixtures import WINNER, exec_gate, exec_run_dir

    failures: list[str] = []
    normalized = " ".join(text.split())
    for token in (
        "the difference it reports is always",
        "candidate minus incumbent",
        "whichever order the experiment file declared its variants in",
    ):
        if token not in normalized:
            failures.append(
                f"the method file no longer says {token!r} — that sentence is the gate's central "
                "promise, and a reversed reading promotes the arm that lost"
            )

    # Distinct sub-directories: `exec_run_dir` asserts its target does not exist, precisely
    # because two builds under one parent silently MERGE into one fixture.
    first = exec_gate(exec_run_dir(tmp / "declared-incumbent-first", **WINNER, declare_incumbent_first=True))
    second = exec_gate(exec_run_dir(tmp / "declared-candidate-first", **WINNER, declare_incumbent_first=False))

    for order, verdict in (("incumbent first", first), ("candidate first", second)):
        if verdict.mean_diff is None or verdict.mean_diff <= 0.0:
            failures.append(
                f"with the variants declared {order} the gate reports mean_diff={verdict.mean_diff} "
                "on a fixture where the candidate wins EVERY row — the method file says the sign is "
                "resolved to candidate minus incumbent whichever order was declared"
            )
        # A MISSING interval fails too. This fixture is a clean two-arm win, so `None` here means
        # the gate refused or could not measure — not an ordering to skip checking.
        if verdict.ci_low is None or verdict.ci_high is None:
            failures.append(
                f"with the variants declared {order} the gate returned no interval "
                f"([{verdict.ci_low}, {verdict.ci_high}]) on a clean two-arm win — the sentence "
                "describes bounds that are re-ordered to match the resolved sign, so there must be "
                "bounds to order"
            )
        elif verdict.ci_low > verdict.ci_high:
            failures.append(
                f"with the variants declared {order} the interval came back reversed "
                f"([{verdict.ci_low}, {verdict.ci_high}]) — negating a difference reverses its "
                "bounds, and the method file says they are re-ordered to match"
            )
    if first.mean_diff != second.mean_diff:
        failures.append(
            f"the two declaration orders disagree: {first.mean_diff} against {second.mean_diff}. "
            "Same rows, same scores — so the reading depends on the order the experiment file "
            "happened to list its variants in, which is the one thing the gate exists to remove"
        )
    return failures


# --- claim 5: the headroom-ceiling table ------------------------------------

_HEADROOM_SIGNATURE = "rule | rows failing | headroom | ceiling | against the 0.0255 floor"
_FLOOR_IN_HEADER = re.compile(r"(\d+\.\d+)")


def _check_headroom_ceilings(text: str, _tmp: Path) -> list[str]:
    """The Step 7 ceiling table, RECOMPUTED from the code and the round it was measured on.

    A reader decides what to spend on from this table. It claims three of four rules were
    unpromotable by arithmetic, and if the formula drifts that advice silently INVERTS — the
    cheapest possible mistake to make and the most expensive to notice, because every number
    downstream keeps agreeing with itself.

    Three things are checked, and they fail on different edits:

    * each cell against :func:`~coder_eval.optimize.fronts.headroom_ceiling` over the real vector,
      so the table cannot drift from the estimator OR from the round;
    * the ceiling cell's own expression, evaluated, so a hand-edited number fails;
    * the DENOMINATOR of that expression against the suite's full row count — the single property
      the whole block rests on, since dividing by the rows that failed reports a per-row lift and
      makes every rule look promotable.
    """
    from coder_eval.optimize.fronts import headroom_ceiling
    from tests.optimize_fixtures import HEADROOM_ROW_SCORES, HEADROOM_RULE_ROWS

    table = next((t for t in parse_markdown_tables(text) if table_signature(t) == _HEADROOM_SIGNATURE), None)
    if table is None:
        return [f"the ceiling table ({_HEADROOM_SIGNATURE}) is gone or changed shape — nothing to check"]

    failures: list[str] = []
    for token, why in (
        ("suite gap, not a hypothesis", "the rule the table exists to state — a candidate cannot fix a gap"),
        ("3 * floor * n_rows", "the inverse sizing rule: how much headroom a rule needs to be worth writing for"),
    ):
        if token not in _ascii(" ".join(text.split())):
            failures.append(f"the skill no longer says {token!r} — {why}")

    # The table must cover EVERY rule the round attributed, and the prose's headline count must
    # follow from the cells. Without this the table can be trimmed to a single row — deleting the
    # one rule that is NOT a gap — and every remaining cell still recomputes correctly, leaving the
    # claim green over a table that says the opposite of what it was written to say.
    listed = [_ascii(row[0]).strip("` ") for row in table.rows if row]
    if set(listed) != set(HEADROOM_RULE_ROWS):
        failures.append(
            f"the ceiling table lists {sorted(listed)}; the measured round attributed "
            f"{sorted(HEADROOM_RULE_ROWS)}. A trimmed table recomputes cell by cell and still lies."
        )

    floor_match = _FLOOR_IN_HEADER.search(table.header[4] if len(table.header) > 4 else "")
    if floor_match is None:
        return [*failures, f"the ceiling table's last column no longer names the floor: {table.header}"]
    floor = float(floor_match.group(1))
    n_rows = len(HEADROOM_ROW_SCORES)

    for row in table.rows:
        if len(row) != len(table.header):
            failures.append(f"the ceiling row {row!r} does not carry every column")
            continue
        rule = _ascii(row[0]).strip("` ")
        if rule not in HEADROOM_RULE_ROWS:
            failures.append(f"the ceiling table names rule {rule!r}, which the measured round did not attribute")
            continue
        want = headroom_ceiling(HEADROOM_ROW_SCORES, rule=rule, rows=HEADROOM_RULE_ROWS[rule])

        if int(_ascii(row[1]).strip("` ")) != want.n_failing:
            failures.append(f"ceiling table {rule}: rows-failing reads {row[1]}, the round has {want.n_failing}")
        if float(_ascii(row[2]).strip("` ")) != round(want.headroom, 3):
            failures.append(f"ceiling table {rule}: headroom reads {row[2]}, recomputes to {want.headroom:.3f}")

        spans = _spans(row[3])
        if not spans:
            failures.append(f"ceiling table {rule}: the ceiling cell states no `headroom / n_rows` expression")
            continue
        # The denominator, checked SYMBOLICALLY before the value: dividing by the rows that failed
        # is the wrong-but-plausible edit, and it yields a bigger number that still "adds up".
        denominator = spans[0].split("/")[-1].strip()
        if denominator != str(n_rows):
            failures.append(
                f"ceiling table {rule}: the ceiling divides by {denominator!r}, not by the suite's "
                f"{n_rows} rows. A subset denominator is a per-row LIFT and makes every rule look promotable"
            )
        if round(evaluate_expression(spans[0], {}), 4) != round(want.ceiling, 4):
            failures.append(f"ceiling table {rule}: `{spans[0]}` does not evaluate to the computed {want.ceiling:.4f}")

        stated = _ascii(row[3]).split("=")[-1].strip("` ")
        if float(stated) != round(want.ceiling, 4):
            failures.append(f"ceiling table {rule}: ceiling reads {stated}, recomputes to {want.ceiling:.4f}")

        # `_ascii` rewrites the U+00D7 the cell is written with, so the multiplier is parsed by
        # LEADING NUMBER rather than by splitting on a character the normalizer moves.
        ratio_match = _FLOOR_IN_HEADER.match(_ascii(row[4]).strip())
        if ratio_match is None:
            failures.append(f"ceiling table {rule}: {row[4]!r} states no multiple of the floor")
            continue
        ratio = float(ratio_match.group(1))
        if round(want.ceiling / floor, 2) != ratio:
            failures.append(
                f"ceiling table {rule}: stated {ratio}x the floor, recomputes to {want.ceiling / floor:.2f}x"
            )
        # The verdict half. A gap and a live rule call for opposite next actions, so a row whose
        # ratio and whose words disagree is worse than one with a stale number.
        if (want.ceiling < floor) != ("gap" in row[4].casefold()):
            failures.append(f"ceiling table {rule}: {row[4]!r} contradicts its own {want.ceiling / floor:.2f}x")

    # The surrounding prose's headline number, DERIVED rather than matched: "three of those four"
    # is the sentence a reader acts on, and it is a fact about the cells above it.
    gaps = sum(
        1
        for rule, rows in HEADROOM_RULE_ROWS.items()
        if headroom_ceiling(HEADROOM_ROW_SCORES, rule=rule, rows=rows).ceiling < floor
    )
    words = {1: "one", 2: "two", 3: "three", 4: "four"}
    claim = f"{words.get(gaps, gaps)} of those {words.get(len(HEADROOM_RULE_ROWS), len(HEADROOM_RULE_ROWS))}"
    if claim not in " ".join(text.split()).casefold():
        failures.append(
            f"the prose beside the ceiling table no longer says {claim!r} candidates were "
            f"unpromotable — the cells above it recompute to {gaps} gaps"
        )
    return failures


CLAIMS: list[ComputedClaim] = [
    ComputedClaim(
        id="cost-table",
        surface=METHOD,
        why=(
            "the table a reader budgets a whole round from. Its symbols went stale once already "
            "(M_tune/M_holdout after the splits were renamed) with no instruction deleted, so no "
            "presence sensor could see it."
        ),
        covers=(_COST_TABLE_SIGNATURE,),
        check=_check_cost_table,
    ),
    ComputedClaim(
        id="halving-premium",
        surface=METHOD,
        why=(
            "this table shipped claiming successive halving SAVES runs. It costs a premium at an "
            "odd arm count and breaks even at an even one — arithmetic, and therefore checkable."
        ),
        covers=(_HALVING_SIGNATURE,),
        check=_check_halving_premium,
    ),
    ComputedClaim(
        id="sizing-table",
        surface=SKILL,
        why=(
            "a user sizes a suite from this table BEFORE spending, and its numbers move with the "
            "floor's estimator. A stale cell here reads as a smaller suite being enough and is "
            "paid for at Stage B, where the gate refuses."
        ),
        covers=(_SIZING_SIGNATURE,),
        check=_check_sizing_table,
    ),
    ComputedClaim(
        id="interval-from-one-run-dir",
        surface=METHOD,
        why=(
            "the false version — 'the statistic cannot be computed from one run dir' — shipped past "
            "every token sensor because it deleted no token. Only running the code catches it."
        ),
        covers=(),
        check=_check_interval_from_one_run_dir,
    ),
    ComputedClaim(
        id="headroom-ceiling",
        surface=SKILL,
        why=(
            "the table a reader decides what to spend on. It claims three of four rules were "
            "unpromotable by arithmetic; if the formula drifts, that advice silently inverts — and "
            "every number downstream keeps agreeing with itself."
        ),
        covers=(_HEADROOM_SIGNATURE,),
        check=_check_headroom_ceilings,
    ),
    ComputedClaim(
        id="execution-sign-resolution",
        surface=METHOD,
        why=(
            "the file's central promise about the execution gate — that it reports "
            "candidate-minus-incumbent whichever order the experiment declared its variants in. "
            "A reversed reading PROMOTES THE ARM THAT LOST, and every subsequent number in the "
            "ledger corroborates it. This binds the sentence to the behaviour; "
            "TestExecutionGateSign is the unit-level guarantee and neither replaces the other."
        ),
        # `covers=()` deliberately: this claim checks a SENTENCE, not a table. The coverage rule
        # (`uncovered_tables`) only demands claims for arithmetic-bearing tables.
        covers=(),
        check=_check_execution_sign_resolution,
    ),
]


def evaluate_claims(tmp: Path, claims: list[ComputedClaim] | None = None) -> list[str]:
    """Every registered claim that does not hold, as ``"<id>: <failure>"`` strings."""
    failures: list[str] = []
    for claim in claims if claims is not None else CLAIMS:
        text = claim.surface.read_text(encoding="utf-8")
        failures += [f"{claim.id}: {failure}" for failure in claim.check(text, tmp / claim.id)]
    return failures


def uncovered_tables(surface: Path, claims: list[ComputedClaim] | None = None) -> list[str]:
    """Arithmetic-bearing tables in ``surface`` that no claim registered against it names.

    The coverage rule, and what makes CE039 a sensor CLASS rather than three bespoke sensors: a new
    table carrying arithmetic fails until someone registers a claim that computes it.

    The map is DERIVED from ``ComputedClaim.covers`` rather than kept beside ``CLAIMS`` — a
    separate ``surface -> tables`` mapping would be two edits that can disagree.
    """
    registered = {
        sig for claim in (claims if claims is not None else CLAIMS) if claim.surface == surface for sig in claim.covers
    }
    text = surface.read_text(encoding="utf-8")
    return [
        f"{surface.name}:{table.line} `{table_signature(table)}`"
        for table in arithmetic_tables(text)
        if table_signature(table) not in registered
    ]
