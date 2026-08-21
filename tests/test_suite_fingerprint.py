"""The suite fingerprint — a content digest over the parts of a suite that decide what a score MEANS.

The grader fingerprint covers the outcome track's script and answer key. This covers the suite
around it, and it is the activation track's ONLY instrument provenance: that track has no script
grader, so its criteria plus its prompt plus its row set are the whole instrument.

Three hazard classes are tested here, and the shape of the test set is itself a decision recorded in
the plan: NOT a fifteen-member fixture factory over the `SuccessCriterion` union. All 15 members
require constructor arguments, so that test means authoring and maintaining fifteen fixtures, and it
would be close to circular anyway — the implementation IS "every field minus the denylist". The real
hazards are narrower:

- **Denylist integrity** — every excluded key still names a real field, with a reason (CE063's
  stale-licence lesson).
- **Dump-settings mutation** — the actual rot path is someone adding `exclude_none=True`,
  `exclude_defaults=True` or `exclude_unset=True` to the dump, which silently drops fields. One
  hand-built criterion exercises all three at once.
- **Discovered real suites** — the shipped templates, loaded through the real `load_task`, so the
  per-type coverage costs zero fixture authoring (CE052's discover-don't-enumerate precedent).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coder_eval.models import RoundScores, RunCommandCriterion, TaskDefinition, copy_with
from coder_eval.optimize.store import grader_changed, suite_changed
from coder_eval.orchestration.task_loader import load_task
from coder_eval.suite_fingerprint import (
    _NOT_SCORING_RELEVANT,
    scoring_dump,
    stale_denylist_keys,
    suite_fingerprint,
)


TEMPLATES = Path(__file__).parent.parent / "plugins" / "coder-eval" / "reference" / "templates"
HERE = Path(__file__).parent


def _rows(*ids: str, prompt: str = "do the thing") -> list[TaskDefinition]:
    """Expanded row-tasks, the shape `expand_dataset` returns and `suite_fingerprint` takes.

    Hand-built rather than expanded, so a test can vary one row's prompt or label without also
    varying the template that produced it — which is the distinction the digest's two arguments are
    for. `_expanded` below covers the real loader's output.
    """
    return [
        TaskDefinition.model_validate(
            {
                "task_id": f"s/{row_id}",
                "row_id": row_id,
                "description": "row",
                "initial_prompt": prompt,
                "success_criteria": [
                    {
                        "type": "skill_triggered",
                        "description": "engaged",
                        "skill_name": "my-skill",
                        "expected_skill": "my-skill",
                    }
                ],
            }
        )
        for row_id in ids
    ]


ROWS = _rows("r1", "r2", "r3")


def _suite(**overrides) -> TaskDefinition:
    """A minimal two-criterion suite. Overrides land on the TaskDefinition, not on a criterion."""
    payload: dict[str, object] = {
        "task_id": "s",
        "description": "d",
        "initial_prompt": "Use the skill. ${row.scenario}",
        "success_criteria": [
            {
                "type": "skill_triggered",
                "description": "engaged",
                "skill_name": "my-skill",
                "expected_skill": "my-skill",
                "weight": 0.05,
            },
            {
                "type": "run_command",
                "description": "graded",
                "command": "python3 $TASK_DIR/outcome-grader/verify.py ${row.id}",
                "score_from_stdout": True,
            },
        ],
        **overrides,
    }
    return TaskDefinition.model_validate(payload)


def _digest(task: TaskDefinition, rows: list[TaskDefinition] | None = None) -> str:
    return suite_fingerprint(task, rows if rows is not None else ROWS)


class TestTheDigestIsStableAcrossMachines:
    """Nothing machine-local, and nothing secret-bearing, may reach it.

    A whole `model_dump` of the task would report "the instrument moved" on a colleague's checkout —
    the exact false positive `verify.py::fingerprint` avoids by hashing relative paths — and would
    also put `agent.env` values into a committed sidecar's pre-image.
    """

    def test_the_agent_and_sandbox_blocks_do_not_reach_it(self) -> None:
        plain = _suite()
        machine_local = _suite(
            agent={
                "type": "claude-code",
                "plugins": [{"type": "local", "path": "$SKILL_SOURCE_PATH"}],
                "claude_settings": {"apiKeyHelper": "/usr/local/bin/print-my-token"},
            },
            sandbox={"template_sources": [{"type": "template_dir", "path": "/Users/someone/checkout/fixture"}]},
        )
        assert _digest(plain) == _digest(machine_local)

    def test_two_checkouts_at_different_absolute_paths_agree(self) -> None:
        here = _suite(sandbox={"template_sources": [{"type": "template_dir", "path": "/home/ci/work/fixture"}]})
        there = _suite(sandbox={"template_sources": [{"type": "template_dir", "path": "/Users/dev/work/fixture"}]})
        assert _digest(here) == _digest(there)

    def test_the_digest_is_a_hex_sha256_and_not_the_pre_image(self) -> None:
        # Recorded in a committed sidecar, so it must be one-way: never the pre-image, and never a
        # field-level diff of values.
        # Only the shape is assertable here: a 64-char hex string contains no `-` and no `.`, so a
        # "the skill name is not in it" assertion could not fail and would prove nothing.
        digest = _digest(_suite())
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


class TestWhatMovesTheDigest:
    def test_a_changed_weight_moves_it(self) -> None:
        """THE headline case — the one `grader_changed` cannot see.

        A weight change re-blends `weighted_score`, which is the execution gate's primary statistic,
        without touching the grader script or its answer key by a byte.
        """
        heavy = _suite()
        heavy.success_criteria[0].weight = 1.0
        assert _digest(_suite()) != _digest(heavy)

    def test_swapping_the_grader_script_moves_it(self) -> None:
        """The failure a base-fields-only digest could not see.

        `command` is a `run_command` field, not a `BaseSuccessCriterion` one, so hashing only the
        five base fields would reintroduce — one level up — the exact defect the grader fingerprint
        exists to catch.
        """
        swapped = _suite()
        swapped.success_criteria[1].command = "python3 $TASK_DIR/outcome-grader/verify2.py ${row.id}"  # type: ignore[union-attr]
        assert _digest(_suite()) != _digest(swapped)

    def test_a_changed_file_check_includes_moves_it(self) -> None:
        base = _suite(
            success_criteria=[
                {"type": "file_check", "description": "artifact", "path": "out.md", "includes": ["Total Revenue"]}
            ]
        )
        changed = _suite(
            success_criteria=[
                {"type": "file_check", "description": "artifact", "path": "out.md", "includes": ["Net Revenue"]}
            ]
        )
        assert _digest(base) != _digest(changed)

    def test_a_changed_skill_name_moves_it(self) -> None:
        renamed = _suite()
        renamed.success_criteria[0].skill_name = "other-skill"  # type: ignore[union-attr]
        assert _digest(_suite()) != _digest(renamed)

    def test_a_changed_prompt_moves_it(self) -> None:
        assert _digest(_suite()) != _digest(_suite(initial_prompt="Do the thing. ${row.scenario}"))

    def test_a_changed_suite_threshold_value_moves_it(self) -> None:
        gated = _suite()
        gated.success_criteria[1].suite_thresholds = {"mean": 0.7}
        raised = _suite()
        raised.success_criteria[1].suite_thresholds = {"mean": 0.8}
        assert _digest(gated) != _digest(raised)

    @pytest.mark.parametrize(
        ("field", "value"),
        [("max_turns", 30), ("turn_timeout", 600), ("task_timeout", 900), ("max_usd", 3.0)],
    )
    def test_a_changed_run_cap_moves_it(self, field: str, value: float) -> None:
        # A raised `max_turns` turns a truncation into a score, which makes two rounds' numbers
        # incomparable as surely as a grader edit does.
        base = {"max_turns": 20, "turn_timeout": 900, "task_timeout": 1800, "max_usd": 2.0}
        assert _digest(_suite(run_limits=base)) != _digest(_suite(run_limits={**base, field: value}))

    def test_a_different_row_set_moves_it(self) -> None:
        assert _digest(_suite(), ROWS) != _digest(_suite(), [*ROWS, *_rows("r4")])

    def test_a_rewritten_row_prompt_moves_it(self) -> None:
        """The blind spot a row-ID-only digest had, and it is the activation track's whole prompt.

        `activation.yaml` is `initial_prompt: ${row.prompt}`, so every prompt lives in the rows file.
        Rewriting one while keeping its id left the digest byte-identical — on the track this digest
        is the ONLY instrument provenance for.
        """
        assert _digest(_suite(), ROWS) != _digest(_suite(), _rows("r1", "r2", "r3", prompt="reworded"))

    def test_a_flipped_row_label_moves_it(self) -> None:
        # The same hole one field over, and the worse half: `expected_skill: "${row.expected_skill}"`
        # means a row's LABEL is its answer key, and the grader half of this pair hashes
        # `expectations/*.json` for exactly that reason.
        relabelled = _rows("r1", "r2", "r3")
        relabelled[0].success_criteria[0].expected_skill = "some-other-skill"  # type: ignore[union-attr]
        assert _digest(_suite(), ROWS) != _digest(_suite(), relabelled)

    def test_a_row_criterion_parameter_moves_it(self) -> None:
        # Row criteria are the SUBSTITUTED ones, so a per-row grader path lands here and nowhere else.
        rekeyed = _rows("r1", "r2", "r3")
        rekeyed[1].success_criteria[0].skill_name = "other-skill"  # type: ignore[union-attr]
        assert _digest(_suite(), ROWS) != _digest(_suite(), rekeyed)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("reference", {"directory": "solution"}),
            ("expected_commands", 4),
            ("pre_run", [{"command": "echo setup"}]),
            ("post_run", [{"command": "echo teardown"}]),
            ("simulation", {"persona": "a terse user", "goal": "get it done", "max_turns": 3}),
        ],
    )
    def test_a_scoring_relevant_task_field_moves_it(self, field: str, value: object) -> None:
        """The five fields the first version was blind to, and `reference` is the sharpest.

        `reference` is the answer key `reference_comparison` / `llm_judge` / `agent_judge` score
        against — and this module's own justification for hashing the criteria WHOLE is that the answer
        key is part of the instrument. Swapping it left the digest byte-identical, so
        `suite_changed` answered False for a moved instrument. `expected_commands` is what
        `commands_efficiency` scores against, `pre_run`/`post_run` decide what the agent starts from,
        and a dialog suite's simulated user IS its instrument.
        """
        assert _digest(_suite()) != _digest(_suite(**{field: value}))

    def test_a_changed_initial_prompt_file_moves_it(self) -> None:
        """The prompt section was VACUOUS for a file-backed suite — it hashed `null`.

        The docstring claimed to cover the initial prompt, which was true only for the inline form.
        Note the stated limit: the file's CONTENTS are not read (this module takes no filesystem), so
        a changed file at an unchanged path does not move the digest.
        """

        def file_backed(name: str) -> TaskDefinition:
            return TaskDefinition.model_validate(
                {
                    "task_id": "s",
                    "description": "d",
                    "initial_prompt_file": name,
                    "success_criteria": [{"type": "file_exists", "description": "a", "path": "o"}],
                }
            )

        assert _digest(file_backed("prompt-a.md")) != _digest(file_backed("prompt-b.md"))

    def test_swapping_two_criteria_moves_it(self) -> None:
        # `criterion_index` is positional everywhere in the optimize family, so declaration order
        # decides which criterion a gate reads. A changed order is a changed instrument.
        swapped = _suite()
        swapped.success_criteria.reverse()
        assert _digest(_suite()) != _digest(swapped)


class TestWhatDoesNotMoveTheDigest:
    def test_a_changed_description_does_not(self) -> None:
        # The one denylist entry: authored prose that changes no score.
        reworded = _suite()
        reworded.success_criteria[0].description = "the skill fired for this row"
        assert _digest(_suite()) == _digest(reworded)

    def test_reordering_suite_threshold_keys_does_not(self) -> None:
        one = _suite()
        one.success_criteria[1].suite_thresholds = {"mean": 0.7, "completion_rate": 1.0}
        other = _suite()
        other.success_criteria[1].suite_thresholds = {"completion_rate": 1.0, "mean": 0.7}
        assert _digest(one) == _digest(other)

    def test_a_different_row_order_does_not(self) -> None:
        assert _digest(_suite(), [ROWS[2], ROWS[0], ROWS[1]]) == _digest(_suite(), ROWS)

    def test_an_unset_run_cap_and_an_absent_run_limits_block_agree(self) -> None:
        assert _digest(_suite()) == _digest(_suite(run_limits={"max_turns": None}))


class TestTheFramingIsCollisionResistant:
    """Two genuinely different suites must not hash the same, however their values are shaped.

    These pin the PROPERTY, and each docstring names the mechanism that actually secures it — because
    verifying by mutation showed the obvious attribution was wrong. Removing the length prefix leaves
    all of these green, and so does removing the section tags: every variable part goes through
    `_canonical`, which escapes a NUL and quotes a string, so a value can neither contain the
    delimiter nor equal a neighbouring section's mapping. The prefix and the tags are redundancy that
    keeps those arguments local, and the module docstring says so rather than claiming otherwise.

    Writing this class as "assert the mutation fails" would therefore have meant writing three tests
    that cannot fail — the shape the plan cites CE044/CE045 for.
    """

    def test_a_value_containing_the_delimiter_does_not_collide(self) -> None:
        # Secured by `_canonical` escaping NUL to `\u0000`, with the length prefix behind it.
        left = suite_fingerprint(_suite(initial_prompt="a"), _rows("b"))
        right = suite_fingerprint(_suite(initial_prompt="a\x00b"), [])
        assert left != right
        assert _digest(_suite(initial_prompt="a")) != _digest(_suite(initial_prompt="a\x00"))

    def test_a_prompt_equal_to_a_criterion_dump_does_not_collide(self) -> None:
        """The migration collision, which was REAL when the prompt was absorbed raw.

        Secured now by `_canonical` quoting the string — a mapping's canonical form begins `{` and a
        string's begins `"` — with the section tags keeping that argument from depending on every
        future part's JSON type.
        """
        extra = {"type": "file_exists", "description": "artifact", "path": "out.md"}
        two_criteria = _suite(success_criteria=[_suite().success_criteria[0].model_dump(mode="json"), extra])
        moved = _suite(
            success_criteria=[_suite().success_criteria[0].model_dump(mode="json")],
            initial_prompt=json.dumps(
                scoring_dump(two_criteria.success_criteria[1]),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        assert suite_fingerprint(two_criteria, []) != suite_fingerprint(moved, [])

    def test_a_row_is_not_mistaken_for_the_prompt(self) -> None:
        assert suite_fingerprint(_suite(initial_prompt="x"), []) != suite_fingerprint(
            _suite(initial_prompt="x"), _rows("x")
        )

    def test_an_absent_value_and_the_string_null_do_not_collide(self) -> None:
        """The reason `_canonical` replaced a hand-rolled "unset" sentinel.

        `None` renders as `null` while the *string* `"null"` renders as `"null"` WITH quotes, so the
        two cannot be confused and no sentinel is needed. Asserted on a ROW's prompt because a
        suite-level `initial_prompt` of `None` is unrepresentable — `TaskDefinition` requires one of
        `initial_prompt` / `initial_prompt_file`.
        """
        absent = _rows("r1")
        absent[0].initial_prompt = None
        assert _digest(_suite(), absent) != _digest(_suite(), _rows("r1", prompt="null"))


class TestTheTwoArgumentsAreDifferentThings:
    """The first is the UNEXPANDED template; the second is the EXPANDED rows it produced.

    Handing the same object to both is the mistake the signature exists to prevent, and the two
    digests are genuinely different values.
    """

    @staticmethod
    def _expanded(count: int) -> tuple[TaskDefinition, list[TaskDefinition]]:
        from coder_eval.orchestration.task_loader import expand_dataset

        suite = _suite(
            dataset={"rows": [{"id": f"r{i}", "scenario": f"scenario {i}"} for i in range(3)], "sample_seed": 7}
        )
        return suite, expand_dataset(suite, HERE, max_rows=count)

    def test_a_row_task_in_the_first_position_digests_differently(self) -> None:
        suite, expanded = self._expanded(3)
        assert suite_fingerprint(suite, expanded) != suite_fingerprint(expanded[0], expanded)

    def test_two_row_selections_of_one_suite_digest_differently(self) -> None:
        # The row SET is part of the instrument, which is the whole reason the rows are an argument.
        suite, two = self._expanded(2)
        _suite_again, three = self._expanded(3)
        assert len(two) == 2 and len(three) == 3
        assert suite_fingerprint(suite, two) != suite_fingerprint(suite, three)

    def test_the_template_placeholders_are_hashed_as_authored(self) -> None:
        # `${row.scenario}` reaches the digest unsubstituted, so a changed TEMPLATE is a changed
        # instrument even when every row is identical.
        suite, expanded = self._expanded(3)
        reworded = _suite(dataset=suite.dataset.model_dump(mode="json") if suite.dataset else None, initial_prompt="X")
        assert suite_fingerprint(suite, expanded) != suite_fingerprint(reworded, expanded)

    def test_a_suite_with_no_dataset_passes_no_rows(self) -> None:
        # The template is then the whole instrument, and an empty row list is the honest input.
        assert suite_fingerprint(_suite(), []) != suite_fingerprint(_suite(), ROWS)


class TestDenylistIntegrity:
    """Every exclusion names a real field and carries a reason — CE063's stale-licence lesson."""

    def test_every_excluded_key_is_a_real_base_criterion_field(self) -> None:
        assert stale_denylist_keys() == set(), (
            f"the denylist names {stale_denylist_keys()}, which is not a BaseSuccessCriterion "
            "field — a licence that has outlived its trade"
        )

    def test_every_exclusion_carries_a_non_empty_reason(self) -> None:
        assert _NOT_SCORING_RELEVANT, "GAP: the denylist is empty, so this assertion checks nothing"
        assert all(reason.strip() for reason in _NOT_SCORING_RELEVANT.values())

    def test_the_partition_is_total_by_construction(self) -> None:
        """Every base field is either hashed or denylisted, and that needs no maintenance.

        The plan's original bullet — "the field-partition test fails when a field is added to
        `BaseSuccessCriterion` and classified nowhere" — is UNSATISFIABLE after the allowlist was
        inverted to a denylist, and that is the inversion working: a new field is in the concrete dump
        and therefore in the digest by default. What is worth asserting is the property that makes it
        so, rather than leaving the bullet looking unimplemented.
        """
        from coder_eval.models import BaseSuccessCriterion

        dumped = set(_suite().success_criteria[0].model_dump(mode="json"))
        base = set(BaseSuccessCriterion.model_fields)
        assert base, "GAP: the base criterion declares no fields"
        assert base - set(_NOT_SCORING_RELEVANT) <= dumped

    def test_the_denylist_holds_exactly_the_one_entry_it_is_documented_to(self) -> None:
        # A second entry is a real decision someone should have to make deliberately: the digest
        # hashes the concrete dump, so a new criterion parameter is included BY DEFAULT and is only
        # ever excluded on purpose.
        assert set(_NOT_SCORING_RELEVANT) == {"description"}


class TestDumpSettingsMutation:
    """The real rot path: `exclude_none` / `exclude_defaults` / `exclude_unset` on the criterion dump.

    An earlier version of this class asserted three digest INEQUALITIES and caught none of the three —
    measured: each mutation left all 41 tests green. The reason is structural, and it is worth stating
    so the shape does not come back: dropping a key from ONE side of an inequality still leaves the
    two sides unequal. The damage from a dropped key is COLLISION, not a lost difference.

    So the contract is asserted on `scoring_dump` directly — every key of the plain dump except the
    denylisted ones must be there — plus one behavioural witness for the worst case:
    `exclude_defaults=True` drops the `type` discriminator, after which two criteria of DIFFERENT
    types with the same remaining fields hash identically.
    """

    @pytest.mark.parametrize("name", ["activation.yaml", "outcome.yaml"])
    def test_scoring_dump_keeps_every_key_of_the_plain_dump(self, name: str) -> None:
        task, _ = load_task(TEMPLATES / name)
        assert task.success_criteria, f"GAP: {name} declares no criteria"
        for criterion in task.success_criteria:
            plain = set(criterion.model_dump(mode="json"))
            assert plain, "GAP: a criterion dumped no keys at all"
            assert set(scoring_dump(criterion)) == plain - set(_NOT_SCORING_RELEVANT), (
                "scoring_dump is dropping keys the plain dump has. An `exclude_none` / "
                "`exclude_defaults` / `exclude_unset` on that dump silently removes fields, and "
                "`exclude_defaults` removes the `type` discriminator itself"
            )

    def test_a_field_left_at_its_default_is_in_the_dump(self) -> None:
        # `exclude_defaults=True` would drop it. `timeout` defaults to a real value.
        criterion = RunCommandCriterion(description="graded", command="python3 verify.py")
        assert scoring_dump(criterion)["timeout"] == criterion.timeout

    def test_a_field_explicitly_set_to_none_is_in_the_dump(self) -> None:
        # `exclude_none=True` would drop it.
        criterion = RunCommandCriterion(description="graded", command="python3 verify.py", expected_stdout=None)
        assert "expected_stdout" in scoring_dump(criterion)

    def test_a_field_never_set_is_in_the_dump(self) -> None:
        # `exclude_unset=True` would drop it.
        criterion = RunCommandCriterion(description="graded", command="python3 verify.py")
        assert "score_from_stdout" in scoring_dump(criterion)

    def test_two_criterion_types_with_the_same_fields_do_not_collide(self) -> None:
        """The behavioural witness, and the collision `exclude_defaults=True` actually causes.

        `file_exists` and `file_check` both reduce to `{description, path}` plus defaults, so with the
        `type` discriminator dropped they become the same criterion — two different instruments
        reading as one, which no inequality-on-one-field test can see.
        """
        exists = _suite(success_criteria=[{"type": "file_exists", "description": "a", "path": "out.md"}])
        check = _suite(success_criteria=[{"type": "file_check", "description": "a", "path": "out.md"}])
        assert _digest(exists) != _digest(check)


# The parameters that say what these suites MEASURE, per shipped criterion type. Named so the coverage
# cannot silently shrink to `weight` and `pass_threshold` — the two fields a base-fields-only digest
# would already have caught, and the reason the first draft of this module was wrong. Module level
# because the test parametrizes on its keys, which a class attribute cannot supply at collection time.
_REQUIRED_PARAMETERS: dict[str, set[str]] = {
    "activation.yaml": {"skill_name", "expected_skill", "weight", "suite_thresholds"},
    "outcome.yaml": {"command", "score_from_stdout", "path", "includes", "skill_name", "weight"},
}


class TestDiscoveredRealSuites:
    """The shipped templates, through the real loader — per-type coverage at zero fixture cost.

    CE052's discover-don't-enumerate precedent, and it is what makes the digest's sensitivity a
    property of `file_check` / `run_command` / `skill_triggered` as authored rather than of three
    hand-built fixtures.

    **Boundary**, stated so a green run is not mistaken for a proof: a field is perturbed only when a
    different value of the same SHAPE can be built from the one it has. `None`, an empty list and an
    empty dict cannot supply one — `stop_early: StopEarlyPolicy | None` and `patterns: list[RegexPattern]`
    are the real cases, and a string dropped into either serializes with a pydantic warning, which
    this suite treats as an error. The explicitly-`None` shape is covered by
    `TestDumpSettingsMutation` instead, and the REQUIRED set below is what keeps these exclusions
    from quietly emptying the test.
    """

    @pytest.mark.parametrize("name", sorted(_REQUIRED_PARAMETERS))
    def test_every_non_denylisted_field_of_every_criterion_moves_the_digest(self, name: str) -> None:
        task, _ = load_task(TEMPLATES / name)
        assert task.success_criteria, f"GAP: {name} declares no criteria, so this checks nothing"
        baseline = _digest(task)
        covered: set[str] = set()
        skipped: set[str] = set()
        for index, criterion in enumerate(task.success_criteria):
            for field, value in criterion.model_dump(mode="json").items():
                if field in _NOT_SCORING_RELEVANT or field == "type":
                    continue
                perturbed = _perturb(value)
                if perturbed is _UNPERTURBABLE:
                    skipped.add(field)
                    continue
                # `copy_with` rather than `model_copy(update=)`: it validates the KEY, which is what
                # keeps a renamed field from silently perturbing nothing, while leaving the VALUE
                # unvalidated so a perturbation of any annotation lands. That is the trade the
                # helper exists for (CE048).
                mutated_criterion = copy_with(criterion, **{field: perturbed})
                criteria = list(task.success_criteria)
                criteria[index] = mutated_criterion
                assert _digest(copy_with(task, success_criteria=criteria)) != baseline, (
                    f"{name} criterion {index}'s {field!r} does not reach the digest, so a change "
                    "to it would read as the same instrument"
                )
                covered.add(field)
        missing = _REQUIRED_PARAMETERS[name] - covered
        assert not missing, (
            f"GAP: {name}'s measuring parameters {sorted(missing)} were not perturbed (skipped: "
            f"{sorted(skipped)}), so this test no longer covers what the suite actually grades on"
        )

    def test_the_two_shipped_templates_do_not_collide(self) -> None:
        activation, _ = load_task(TEMPLATES / "activation.yaml")
        outcome, _ = load_task(TEMPLATES / "outcome.yaml")
        assert _digest(activation) != _digest(outcome)


# "no different value of this shape can be built from the one given" — see the boundary above.
_UNPERTURBABLE = object()


def _perturb(value: object) -> object:
    """A different value of the SAME shape, or :data:`_UNPERTURBABLE`.

    Same shape is the whole requirement: the mutated criterion is built with ``copy_with``, which
    validates the key and not the value, so a string dropped into ``list[RegexPattern]`` reaches
    ``model_dump`` and emits a pydantic serializer warning — an error under this suite's settings, and
    a failure that has nothing to do with the digest. A container therefore perturbs from its own
    contents (duplicate an element; re-perturb the first value of a mapping) and gives up when it has
    none to work from.
    """
    if value is None:
        return _UNPERTURBABLE
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 1
    if isinstance(value, str):
        return value + "-perturbed"
    if isinstance(value, list):
        # Duplicating an existing element keeps the element type valid and still lengthens the list,
        # which the canonical JSON shows.
        return [*value, value[0]] if value else _UNPERTURBABLE
    if isinstance(value, dict):
        if not value:
            return _UNPERTURBABLE
        key = next(iter(value))
        inner = _perturb(value[key])
        return _UNPERTURBABLE if inner is _UNPERTURBABLE else {**value, key: inner}
    return _UNPERTURBABLE


class TestSuiteChanged:
    """The three-valued predicate, and the ONE body it shares with `grader_changed`."""

    @staticmethod
    def _round(number: int, *, suite: str | None = None, grader: str | None = None) -> RoundScores:
        return RoundScores(round=number, suite_fingerprint=suite, grader_fingerprint=grader)

    def test_two_recorded_and_equal_fingerprints_read_false(self) -> None:
        assert suite_changed(self._round(1, suite="abc"), self._round(2, suite="abc")) is False

    def test_two_recorded_and_different_fingerprints_read_true(self) -> None:
        assert suite_changed(self._round(1, suite="abc"), self._round(2, suite="def")) is True

    @pytest.mark.parametrize(
        ("previous", "current"),
        [(None, "abc"), ("abc", None), (None, None)],
        ids=["previous-missing", "current-missing", "both-missing"],
    )
    def test_a_missing_fingerprint_is_unknown_never_false(self, previous: str | None, current: str | None) -> None:
        # `None`, never `False`: an older sidecar must not masquerade as an instrument that provably
        # did not move.
        assert suite_changed(self._round(1, suite=previous), self._round(2, suite=current)) is None

    def test_no_previous_round_is_unknown(self) -> None:
        assert suite_changed(None, self._round(1, suite="abc")) is None

    def test_the_two_predicates_read_different_fields(self) -> None:
        # One body, two fields — a round whose SUITE moved while its grader did not, and the reverse.
        suite_moved = (self._round(1, suite="a", grader="g"), self._round(2, suite="b", grader="g"))
        assert (suite_changed(*suite_moved), grader_changed(*suite_moved)) == (True, False)
        grader_moved = (self._round(1, suite="a", grader="g"), self._round(2, suite="a", grader="h"))
        assert (suite_changed(*grader_moved), grader_changed(*grader_moved)) == (False, True)

    def test_the_field_round_trips_through_the_sidecar_json(self) -> None:
        stored = json.loads(self._round(1, suite="abc").model_dump_json())
        assert stored["suite_fingerprint"] == "abc"
        assert RoundScores.model_validate(stored).suite_fingerprint == "abc"

    def test_a_round_written_before_the_field_reads_as_unrecorded(self) -> None:
        assert RoundScores.model_validate({"round": 1}).suite_fingerprint is None
