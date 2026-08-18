"""CE036 — every live-observable criterion must honor the ``live_verdict`` contract.

``EarlyStopWatcher``'s deferred fail-stop, verdict latching, and ``_prev_verdicts``
flip-attribution (``orchestration/early_stop.py``) are correct ONLY because every
armed criterion's ``live_verdict`` is:

* **deterministic** — a pure function of the ``turn_records`` prefix handed in, with
  no wall-clock, randomness, or hidden instance state; and
* **monotonic** — once it returns ``"pass"``/``"fail"`` for some trajectory prefix it
  returns that SAME verdict for every longer prefix. ``"undecided"`` is the only
  verdict allowed to change.

That contract is documented on ``LiveVerdict`` / ``BaseCriterion.live_verdict``
(``criteria/base.py``) but, until this rule, nothing enforced it: a third criterion
(in-tree or third-party plugin) implementing ``live_verdict`` non-monotonically would
type-check, pass CE025, and silently corrupt the stop logic — latching a verdict the
run then contradicts. See GitHub issue #61 item 2.

Design choices, each load-bearing:

* **Replay, not static analysis.** Monotonicity over arbitrary Python is undecidable,
  so there is no sound *static* check to write. What IS mechanical is replaying a
  criterion against every prefix of a recorded trajectory and asserting the property
  directly. That is what ``contract_violations`` does.
* **Seeded permutations widen the walk.** ``permuted_violations`` re-runs the
  determinism + monotonicity walk over seeded reorderings of each case's commands —
  an order-sensitive bug (verdict read off the *latest* command instead of the
  accumulated set) can look perfectly monotone on the one ordering the author wrote
  and flip on a reordering. The terminal-verdict and polarity checks stay
  authored-ordering-only, where they are sound.
* **Fixtures are mandatory, and the registry says so.** A property test over random
  trajectories would return ``"undecided"`` almost always and pass *vacuously*,
  proving nothing. So each live criterion type must supply cases in ``CASES``, and
  ``missing_case_types`` — driven by the ``SuccessCriterion`` union, exactly like
  CE025 — fails when a newly added ``LiveSuccessCriterion`` has none. Adding a live
  criterion now forces the author to demonstrate the contract in the same change.
* **Each case declares what it reaches.** ``ContractCase.reaches`` pins the verdict on
  the FULL trajectory, so a fixture that quietly stops exercising its decision path
  (a renamed tool, a changed regex) fails loudly instead of degrading into another
  vacuous all-``undecided`` replay.
* **Polarity honesty is checked too.** ``live_decidable_polarities`` (on the model) is
  documented as a subset of what the checker's ``live_verdict`` can emit for that
  instance. A case that terminally decides a polarity the instance does NOT claim is a
  real bug — the watcher would treat that trigger as inert while the checker decides
  it — so ``contract_violations`` reports it.

**Honest limits.** (1) This proves the contract holds *on the trajectories the author
supplied*, not in general. A careless implementation with an agreeable fixture still
passes. The rule raises the cost of the bug and puts the contract in front of the next
implementer; it does not close the hole. Nothing short of a proof would. (2) It covers
the in-tree ``SuccessCriterion`` union only — an out-of-tree plugin criterion never
appears in ``live_criterion_types``, and this module lives under ``tests/`` (not shipped
in the wheel), so a plugin shipping a live criterion should copy the replay pattern —
a ``ContractCase``-style fixture plus the prefix walk — into its own test suite, with
this module as the reference implementation (docs/EXTENDING.md says so where plugin
authors will read it). (3) The determinism probe is two
back-to-back calls on identical input: it catches RNG and per-call mutable state, but
two calls microseconds apart will rarely disagree on a *wall-clock* read, so a
slowly-varying ``datetime.now()`` dependency largely escapes it (the monotonicity
replay is the likelier tripwire for one, and only if the fixture happens to straddle
the flip).

Like CE025/CE030, this is intentionally NOT a ``BaseRule`` registered in
``tests/lint/runner.py`` (that runner is AST-only, one ``.py`` file at a time); it
reasons over the criteria registry and executes checkers, and is wired as
``tests/test_custom_lint.py::TestCE036LiveVerdictContract``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args, get_origin

from coder_eval.models import (
    CommandExecutedCriterion,
    CommandTelemetry,
    LiveSuccessCriterion,
    SkillTriggeredCriterion,
    SuccessCriterion,
)
from tests._fixtures.live_criteria import make_command as cmd
from tests._fixtures.live_criteria import make_turn  # shared builders (frozen timestamp)


if TYPE_CHECKING:
    from coder_eval.criteria.base import BaseCriterion, LiveVerdict


@dataclass(frozen=True)
class ContractCase:
    """One replayable trajectory for one criterion instance.

    ``commands`` is replayed prefix by prefix (0 .. len), so a case is only as
    strong as the decision path it actually walks: prefer trajectories where the
    verdict flips partway through over ones that decide on the first command.
    """

    label: str
    criterion: LiveSuccessCriterion
    commands: tuple[CommandTelemetry, ...]
    reaches: LiveVerdict
    """Verdict on the FULL trajectory. ``"undecided"`` is a legitimate (and useful)
    expectation — it pins a shape the criterion deliberately never decides live."""


def _skill_crit(*, skill_name: str, expected_skill: str) -> SkillTriggeredCriterion:
    return SkillTriggeredCriterion(
        type="skill_triggered",
        description=f"skill_triggered[{skill_name}]",
        skill_name=skill_name,
        expected_skill=expected_skill,
    )


def _cmd_crit(
    *,
    pattern: str | None = "curl",
    min_count: int = 1,
    max_count: int | None = None,
    require_success: bool = False,
) -> CommandExecutedCriterion:
    return CommandExecutedCriterion(
        type="command_executed",
        description=f"command_executed[{pattern}]",
        tool_name="Bash",
        command_pattern=pattern,
        min_count=min_count,
        max_count=max_count,
        require_success=require_success,
    )


def _bash(
    command: str,
    *,
    sequence_number: int,
    result_status: Literal["success", "error", "unknown"] = "success",
) -> CommandTelemetry:
    return cmd("Bash", {"command": command}, sequence_number=sequence_number, result_status=result_status)


def _skill(name: str, *, sequence_number: int) -> CommandTelemetry:
    return cmd("Skill", {"skill": name}, sequence_number=sequence_number)


# --------------------------------------------------------------------------- #
# The fixture table. Every LiveSuccessCriterion type in the SuccessCriterion
# union MUST appear here (enforced by ``missing_case_types``), and every polarity
# its instances claim decidable must be reached by some case (``polarity_gaps``).
# --------------------------------------------------------------------------- #

CASES: dict[str, tuple[ContractCase, ...]] = {
    "skill_triggered": (
        ContractCase(
            label="positive row: expected skill engages via the Skill tool",
            criterion=_skill_crit(skill_name="uipath-agents", expected_skill="uipath-agents"),
            commands=(
                _bash("ls -la", sequence_number=0),
                _skill("uipath-agents", sequence_number=1),
                _bash("echo done", sequence_number=2),
            ),
            reaches="pass",
        ),
        ContractCase(
            label="positive row: a distractor engages FIRST, expected skill still passes",
            # The any-engagement recall path: an earlier wrong touch must not
            # freeze this instance, and the late "pass" must survive the trailing
            # commands unchanged.
            criterion=_skill_crit(skill_name="uipath-agents", expected_skill="uipath-agents"),
            commands=(
                _skill("uipath-rpa", sequence_number=0),
                _skill("uipath-agents", sequence_number=1),
                _skill("uipath-maestro-flow", sequence_number=2),
            ),
            reaches="pass",
        ),
        ContractCase(
            label="positive row: non-Claude engagement by reading the skill off disk",
            criterion=_skill_crit(skill_name="uipath-agents", expected_skill="uipath-agents"),
            commands=(
                _bash("ls .agents/skills", sequence_number=0),
                _bash("cat .agents/skills/uipath-agents/SKILL.md", sequence_number=1),
            ),
            reaches="pass",
        ),
        ContractCase(
            label="positive row: expected skill never engages -> never decides",
            criterion=_skill_crit(skill_name="uipath-agents", expected_skill="uipath-agents"),
            commands=(
                _bash("ls -la", sequence_number=0),
                _bash("cat README.md", sequence_number=1),
            ),
            reaches="undecided",
        ),
        ContractCase(
            label="positive row: foreign skill path read FIRST via shell, expected read later still passes",
            # Pinned from the live counterfactual probe
            # tasks/early_stop_contract_any_engagement.yaml: a first-engagement
            # regression (fail on any foreign engagement while the target is
            # unengaged) is non-monotonic exactly on this walk — it fail-stopped
            # the live run at tool call 1 and flipped SUCCESS to FAILURE.
            criterion=_skill_crit(skill_name="beta", expected_skill="beta"),
            commands=(
                _bash("ls skills/alpha/", sequence_number=0),
                _bash("ls skills/beta/", sequence_number=1),
                _bash("echo done", sequence_number=2),
            ),
            reaches="pass",
        ),
        ContractCase(
            label="distractor row: a wrong skill engaging is a decidable miss",
            criterion=_skill_crit(skill_name="uipath-rpa", expected_skill="uipath-agents"),
            commands=(
                _bash("ls -la", sequence_number=0),
                _skill("uipath-rpa", sequence_number=1),
                _skill("uipath-agents", sequence_number=2),
            ),
            reaches="fail",
        ),
    ),
    "command_executed": (
        ContractCase(
            label="no upper bound + positive floor: passes when the count reaches min_count",
            criterion=_cmd_crit(min_count=2, max_count=None),
            commands=(
                _bash("echo hello", sequence_number=0),
                _bash("curl https://example.com", sequence_number=1),
                _bash("curl https://example.org", sequence_number=2),
                _bash("echo bye", sequence_number=3),
            ),
            reaches="pass",
        ),
        ContractCase(
            label="must-NOT-run form (min 0 / max 0): the first forbidden match fails",
            criterion=_cmd_crit(pattern="rm -rf", min_count=0, max_count=0),
            commands=(
                _bash("ls -la", sequence_number=0),
                _bash("rm -rf /tmp/scratch", sequence_number=1),
                _bash("echo done", sequence_number=2),
            ),
            reaches="fail",
        ),
        ContractCase(
            label="upper bound exceeded: fails only once the count passes max_count",
            criterion=_cmd_crit(min_count=1, max_count=1),
            commands=(
                _bash("curl https://example.com", sequence_number=0),
                _bash("curl https://example.org", sequence_number=1),
            ),
            reaches="fail",
        ),
        ContractCase(
            label="bounded range: a pass is not final until end-of-run, so never decides live",
            criterion=_cmd_crit(min_count=1, max_count=3),
            commands=(
                _bash("curl https://example.com", sequence_number=0),
                _bash("echo done", sequence_number=1),
            ),
            reaches="undecided",
        ),
        ContractCase(
            label="bounded window then overrun: undecided through [min, max], fail past max",
            # Pinned from the live counterfactual probe
            # tasks/early_stop_contract_bounded_pass.yaml: a two-sided mutant
            # that latches a premature pass at min_count is non-monotonic
            # exactly on this walk (pass at count 1, fail at count 3) — live it
            # froze a still-compliant count and flipped FAILURE to SUCCESS.
            criterion=_cmd_crit(pattern="echo ping", min_count=1, max_count=2),
            commands=(
                _bash("echo ping", sequence_number=0),
                _bash("echo ping", sequence_number=1),
                _bash("echo ping", sequence_number=2),
            ),
            reaches="fail",
        ),
        ContractCase(
            label="no bounds at all (min 0 / max None): neither polarity is decidable",
            criterion=_cmd_crit(min_count=0, max_count=None),
            commands=(
                _bash("curl https://example.com", sequence_number=0),
                _bash("curl https://example.org", sequence_number=1),
            ),
            reaches="undecided",
        ),
        ContractCase(
            label="malformed regex degrades to undecided rather than raising",
            criterion=_cmd_crit(pattern="[unclosed", min_count=1, max_count=None),
            commands=(_bash("curl https://example.com", sequence_number=0),),
            reaches="undecided",
        ),
        ContractCase(
            label="require_success: a crashed match never counts toward the live pass",
            # The CE034-motivating hazard, pinned in the contract table: without
            # require_success an errored invocation would live-PASS this criterion
            # (and could fire on_pass: stop). WITH it, the shared matcher filters
            # the error out of BOTH live_verdict and _check_impl, so the verdict
            # stays undecided across the whole trajectory.
            criterion=_cmd_crit(min_count=1, max_count=None, require_success=True),
            commands=(
                _bash("curl https://example.com", sequence_number=0, result_status="error"),
                _bash("echo done", sequence_number=1),
            ),
            reaches="undecided",
        ),
        ContractCase(
            label="require_success: the pass latches only on the successful match",
            # An errored match first, a successful one later: the verdict must go
            # undecided -> undecided -> pass and hold — replaying every prefix pins
            # that the error can neither count nor un-count anything.
            criterion=_cmd_crit(min_count=1, max_count=None, require_success=True),
            commands=(
                _bash("curl https://example.com", sequence_number=0, result_status="error"),
                _bash("curl https://example.org", sequence_number=1),
                _bash("echo done", sequence_number=2),
            ),
            reaches="pass",
        ),
    ),
}


# --------------------------------------------------------------------------- #
# The replay engine
# --------------------------------------------------------------------------- #


def verdict_at(
    checker: BaseCriterion[Any],
    criterion: LiveSuccessCriterion,
    commands: tuple[CommandTelemetry, ...],
    prefix_len: int,
) -> LiveVerdict:
    """``live_verdict`` over the first ``prefix_len`` commands.

    Wraps the prefix in a SINGLE ``TurnRecord``, which is exactly how
    ``EarlyStopWatcher._collect_verdicts`` calls it (``records = [record]``) — the
    watcher rebuilds one record from its own collector on every round rather than
    accumulating a list.
    """
    record = make_turn(*commands[:prefix_len])
    return checker.live_verdict(criterion, [record])


def _walk_prefixes(
    checker: BaseCriterion[Any],
    criterion: LiveSuccessCriterion,
    commands: tuple[CommandTelemetry, ...],
    label: str,
) -> tuple[list[str], LiveVerdict]:
    """Prefix-by-prefix determinism + monotonicity walk over ONE command ordering.

    The shared core of both replay modes: ``contract_violations`` walks the
    fixture's authored ordering (and layers the terminal-verdict/polarity checks
    on top), ``permuted_violations`` walks seeded reorderings (where those extra
    checks would be unsound — see its docstring). Returns the breach list and the
    full-trajectory verdict.

    1. **Determinism** — ``live_verdict`` called twice on an identical prefix must
       agree. Catches RNG and per-call mutable state; NOT a reliable wall-clock
       tripwire — the two calls land microseconds apart (module docstring, honest
       limit 3).
    2. **Monotonicity** — once a prefix decides, every longer prefix returns that
       same verdict.
    3. **No raising** — an exception from ``live_verdict`` is reported as a labeled
       violation (case + prefix length) rather than crashing the walk; the remaining
       prefixes still replay so one bad prefix does not mask breaches elsewhere. The
       watcher runs mid-turn where a raise would take down the stop logic, and the
       shape ``command_executed`` pins for a malformed regex — degrade to
       ``"undecided"``, never raise — is the contract for every implementation.
    """
    violations: list[str] = []
    decided: LiveVerdict | None = None
    decided_at = 0
    final: LiveVerdict = "undecided"

    for prefix_len in range(len(commands) + 1):
        try:
            first = verdict_at(checker, criterion, commands, prefix_len)
            second = verdict_at(checker, criterion, commands, prefix_len)
        except Exception as exc:  # any raise, of any type, IS the violation being reported
            violations.append(
                f"{label}: live_verdict RAISED {exc!r} at prefix length {prefix_len} — it must "
                f"degrade to 'undecided' on inputs it cannot judge, never raise."
            )
            continue
        if first != second:
            violations.append(
                f"{label}: live_verdict is NON-DETERMINISTIC at prefix length {prefix_len} "
                f"({first!r} then {second!r} for the same input) — it must be a pure function of turn_records."
            )
        if decided is not None and first != decided:
            violations.append(
                f"{label}: live_verdict is NON-MONOTONIC — decided {decided!r} at prefix length "
                f"{decided_at}, then returned {first!r} at prefix length {prefix_len}. Once decided, a "
                f"verdict must hold for every longer prefix."
            )
        elif decided is None and first != "undecided":
            decided = first
            decided_at = prefix_len
        final = first

    return violations, final


def contract_violations(checker: BaseCriterion[Any], case: ContractCase) -> list[str]:
    """Replay every prefix of ``case``; return contract breaches (empty list = clean).

    Checks five things: determinism, monotonicity, and no-raising (via
    ``_walk_prefixes``), then two checks specific to the authored ordering:

    4. **Declared terminal verdict** — the full trajectory reaches ``case.reaches``,
       so a fixture cannot rot into a vacuous all-``undecided`` replay.
    5. **Polarity honesty** — a terminal decision must be a polarity the instance's
       own ``live_decidable_polarities()`` claims; deciding one it does not claim
       leaves the watcher treating a live trigger as inert.
    """
    violations, final = _walk_prefixes(checker, case.criterion, case.commands, repr(case.label))

    if final != case.reaches:
        violations.append(
            f"{case.label!r}: full trajectory reaches {final!r}, but the case declares {case.reaches!r}. "
            f"Update ContractCase.reaches, or fix the fixture so it exercises the intended decision path."
        )

    if final != "undecided":
        claimed = case.criterion.live_decidable_polarities()
        if final not in claimed:
            violations.append(
                f"{case.label!r}: live_verdict decided {final!r}, but this instance's "
                f"live_decidable_polarities() claims only {set(claimed) or '{}'}. EarlyStopWatcher would "
                f"treat that trigger as inert while the checker actually decides it."
            )

    return violations


# Fixed seed: every CI run replays the exact same shuffles (a flaky lint rule
# would erode trust in the gate faster than any coverage it adds).
_PERMUTATION_SEED = 20260816


def permuted_violations(
    checker: BaseCriterion[Any],
    case: ContractCase,
    *,
    shuffles: int = 5,
    seed: int = _PERMUTATION_SEED,
) -> list[str]:
    """Determinism + monotonicity under seeded reorderings of the case's commands.

    ``contract_violations`` walks ONE ordering — the one the fixture author wrote.
    But the contract quantifies over ANY trajectory, and the orderings an author
    does not think of are exactly where an order-sensitive bug (e.g. a verdict
    computed from the *latest* command instead of the accumulated set) hides:
    such a checker can look perfectly monotone on the authored ordering and flip
    on a reordering. Seeded shuffles probe those orderings essentially for free.

    Deliberately NOT checked here: ``case.reaches`` and polarity honesty. A
    reordering may legitimately change the terminal verdict for a criterion whose
    semantics are order-sensitive, so pinning either would make this layer
    unsound for exactly the criteria it exists to probe. Both stay enforced on
    the authored ordering by ``contract_violations``.
    """
    rng = random.Random(seed)
    violations: list[str] = []
    for round_no in range(shuffles):
        shuffled = list(case.commands)
        rng.shuffle(shuffled)
        walk, _final = _walk_prefixes(
            checker,
            case.criterion,
            tuple(shuffled),
            f"{case.label!r} [shuffle {round_no + 1}/{shuffles}, seed {seed}]",
        )
        violations.extend(walk)
    return violations


# --------------------------------------------------------------------------- #
# Registry-derived coverage
# --------------------------------------------------------------------------- #


def live_criterion_types() -> dict[str, type[LiveSuccessCriterion]]:
    """Every ``LiveSuccessCriterion`` member of the ``SuccessCriterion`` union, by discriminator.

    Walks the discriminated union rather than the checker registry (mirroring CE025's
    ``_type_to_model``): ``LiveSuccessCriterion`` subclassing on the MODEL is the single
    source of truth for "is this criterion type live-observable". In-tree types only —
    plugin criteria are not in the union (see the module docstring's honest limits).
    """
    assert get_origin(SuccessCriterion) is Annotated
    inner, *_ = get_args(SuccessCriterion)
    return {
        model.model_fields["type"].default: model
        for model in get_args(inner)
        if issubclass(model, LiveSuccessCriterion)
    }


def missing_case_types(cases: dict[str, tuple[ContractCase, ...]] | None = None) -> list[str]:
    """Live criterion types with no contract cases — a vacuous, unenforced contract."""
    table = CASES if cases is None else cases
    return sorted(ctype for ctype in live_criterion_types() if not table.get(ctype))


def polarity_gaps(cases: dict[str, tuple[ContractCase, ...]] | None = None) -> list[str]:
    """Polarities a type's fixtures claim decidable but never actually demonstrate.

    Without this, a type could satisfy ``missing_case_types`` with a single
    always-``undecided`` case and enforce nothing about its decision paths.
    """
    table = CASES if cases is None else cases
    gaps: list[str] = []
    for ctype, type_cases in sorted(table.items()):
        claimed = {p for case in type_cases for p in case.criterion.live_decidable_polarities()}
        reached = {case.reaches for case in type_cases}
        for polarity in sorted(claimed - reached):
            gaps.append(
                f"{ctype}: fixtures claim polarity {polarity!r} is live-decidable, but no ContractCase "
                f"reaches it — that decision path is untested."
            )
    return gaps
