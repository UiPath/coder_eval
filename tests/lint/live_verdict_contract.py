"""CE036 — every live-observable criterion must honor the ``live_verdict`` contract.

``EarlyStopWatcher`` (``orchestration/early_stop.py``) is correct only if every armed
criterion's ``live_verdict`` is:

* **deterministic** — a pure function of the ``turn_records`` prefix handed in; and
* **monotonic** — once it returns ``"pass"``/``"fail"`` for a prefix, it returns that
  verdict for every longer prefix. Only ``"undecided"`` may change.

``contract_violations`` replays each ``CASES`` fixture prefix by prefix;
``permuted_violations`` repeats that over seeded reorderings; ``missing_case_types`` and
``polarity_gaps`` fail a live type with no cases, or a claimed polarity no case reaches.

**Honest limits.** (1) Proves the contract only on the supplied trajectories.
(2) In-tree ``SuccessCriterion`` union only; plugins copy the replay pattern
(docs/EXTENDING.md). (3) The determinism probe is two back-to-back calls, so it rarely
catches a slowly-varying wall-clock read.

Wired as ``tests/test_custom_lint.py::TestCE036LiveVerdictContract``.

Rationale: .claude/notes/lint-rules.md § CE036
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
            # Pinned from a live counterfactual run (PR #126): a first-engagement
            # regression (fail on any foreign engagement while the target is
            # unengaged) is non-monotonic exactly on this walk — live, it fail-stopped
            # at tool call 1, truncating the run before the expected engagement, and
            # flipped SUCCESS to FAILURE. This case IS the enforced form of that
            # evidence: the probe task it came from is deliberately not checked in,
            # since the mutant half is not reproducible from the repo.
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
            # Pinned from a live counterfactual run (PR #126): a two-sided mutant
            # that latches a premature pass at min_count is non-monotonic exactly on
            # this walk (pass at count 1, fail at count 3) — live, it froze a
            # still-compliant count and flipped FAILURE to SUCCESS. As above, this
            # case is the enforced form of that evidence; the probe task is not
            # checked in.
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
) -> tuple[list[str], LiveVerdict | None]:
    """Prefix-by-prefix determinism + monotonicity walk over ONE command ordering.

    Shared core of ``contract_violations`` (authored ordering) and
    ``permuted_violations`` (seeded reorderings). Per prefix it reports:

    1. **Determinism** — two calls on an identical prefix disagree. NOT a reliable
       wall-clock tripwire (module docstring, honest limit 3).
    2. **Monotonicity** — a decided verdict changes on a longer prefix.
    3. **No raising** — any exception, as a labeled violation; the walk continues.

    Returns the breach list and the full-trajectory verdict, or ``None`` for that
    verdict when the terminal prefix raised.

    Rationale: .claude/notes/lint-rules.md § CE036
    """
    violations: list[str] = []
    decided: LiveVerdict | None = None
    decided_at = 0
    final: LiveVerdict | None = "undecided"

    for prefix_len in range(len(commands) + 1):
        try:
            first = verdict_at(checker, criterion, commands, prefix_len)
            second = verdict_at(checker, criterion, commands, prefix_len)
        except Exception as exc:  # any raise, of any type, IS the violation being reported
            violations.append(
                f"{label}: live_verdict RAISED {exc!r} at prefix length {prefix_len} — it must "
                + "degrade to 'undecided' on inputs it cannot judge, never raise."
            )
            # No verdict for THIS prefix. Clear the running terminal value so a raise on
            # the last prefix cannot leave the previous prefix's verdict standing in for
            # it (which would stack a phantom `reaches` breach on top of the real one).
            final = None
            continue
        if first != second:
            violations.append(
                f"{label}: live_verdict is NON-DETERMINISTIC at prefix length {prefix_len} "
                + f"({first!r} then {second!r} for the same input) — it must be a pure function of turn_records."
            )
        if decided is not None and first != decided:
            violations.append(
                f"{label}: live_verdict is NON-MONOTONIC — decided {decided!r} at prefix length "
                + f"{decided_at}, then returned {first!r} at prefix length {prefix_len}. Once decided, "
                + "a verdict must hold for every longer prefix."
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

    Checks 4 and 5 are skipped when the terminal prefix RAISED (``final is None``):
    there is no verdict to judge, and the raise reported by ``_walk_prefixes`` is
    already the finding — adding a derived ``reaches`` breach would only bury it.
    """
    violations, final = _walk_prefixes(checker, case.criterion, case.commands, repr(case.label))

    if final is None:
        return violations

    if final != case.reaches:
        violations.append(
            f"{case.label!r}: full trajectory reaches {final!r}, but the case declares {case.reaches!r}. "
            + "Update ContractCase.reaches, or fix the fixture so it exercises the intended decision path."
        )

    if final != "undecided":
        claimed = case.criterion.live_decidable_polarities()
        if final not in claimed:
            violations.append(
                f"{case.label!r}: live_verdict decided {final!r}, but this instance's "
                + f"live_decidable_polarities() claims only {set(claimed) or '{}'}. EarlyStopWatcher "
                + "would treat that trigger as inert while the checker actually decides it."
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

    Runs ``_walk_prefixes`` over ``shuffles`` seeded shuffles, each RENUMBERED
    (``sequence_number`` reassigned 0..N-1 in the new order) so it is a trajectory
    the watcher could produce. Do not drop the renumber: without it, a checker that
    sorts by ``sequence_number`` silently replays the authored ordering.

    Does NOT check ``case.reaches`` or polarity honesty: a reordering may
    legitimately change the terminal verdict, so both are unsound here and stay
    enforced on the authored ordering by ``contract_violations``.

    Rationale: .claude/notes/lint-rules.md § CE036
    """
    rng = random.Random(seed)
    violations: list[str] = []
    for round_no in range(shuffles):
        shuffled = list(case.commands)
        rng.shuffle(shuffled)
        renumbered = tuple(
            command.model_copy(update={"sequence_number": position}) for position, command in enumerate(shuffled)
        )
        walk, _final = _walk_prefixes(
            checker,
            case.criterion,
            renumbered,
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
                + "reaches it — that decision path is untested."
            )
    return gaps
