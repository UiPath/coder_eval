"""The composites in `coder_eval.optimize.api` — the surface `SKILL.md` imports.

Every assertion on a NUMBER compares against a direct call to the library function the composite
composes, never a hardcoded float: the composite's job is the guards, the fallbacks and the order,
and a literal here would pin the estimator instead — which is what `tests/lint/estimator_ledger.py`
exists to govern.

A `NoiseFloor` is deliberately never compared by `model_dump()`: `computed_at` is
`datetime.now(UTC)`, so the dump moves between two calls over the same tree.
"""

from pathlib import Path

import pytest

from coder_eval.models import ACTIVATION_FLOOR_METRIC, EXECUTION_FLOOR_METRIC, NoiseFloor
from coder_eval.optimize.activation import noise_floor_mde
from coder_eval.optimize.api import activation_floor_report, execution_floor_report
from coder_eval.optimize.execution import measure_execution_noise_floor
from coder_eval.optimize.store import UNRESOLVED_MODEL
from tests.optimize_fixtures import SUITE, eval_result, weighted_arm, write_row


def _sidecar(tmp_path: Path) -> Path:
    """A sidecar path under a `my-skill` directory — `load_measurements` keys on the parent name."""
    return tmp_path / "my-skill" / "measurements.json"


def _baseline(tmp_path: Path, *, invocations: int = 2) -> list[Path]:
    """One arm over `invocations` run directories, whose halves DISAGREE on two rows.

    Not `write_arm`, and that is the whole point: it writes the identical result into every
    invocation, so both halves of the null split are byte-identical and the floor comes back exactly
    0.000. A test asserting on that number is satisfied by any composition that reaches the
    estimator at all — it cannot see a mis-threaded seed, resample count or statistic. Here the
    later invocations flip two rows, so the measured floor is a number the arithmetic produced.
    """
    labels: dict[str, list[tuple[str, str]]] = {
        "r1": [("yes", "yes")],
        "r2": [("yes", "yes")],
        "r3": [("yes", "no")],
        "r4": [("no", "no")],
        "r5": [("no", "no")],
    }
    flipped = {**labels, "r1": [("yes", "no")], "r4": [("no", "yes")]}
    run_dirs: list[Path] = []
    for i in range(invocations):
        run_dir = tmp_path / f"run-{i}"
        for row_id, pairs in (labels if i == 0 else flipped).items():
            write_row(run_dir, "default", row_id, eval_result(row_id, pairs))
        run_dirs.append(run_dir)
    return run_dirs


class TestActivationFloorReport:
    def test_the_block_states_the_floor_the_estimator_measured(self, tmp_path: Path) -> None:
        run_dirs = _baseline(tmp_path)
        block = activation_floor_report(
            run_dirs=run_dirs, suite_id=SUITE, criterion_index=0, sidecar=_sidecar(tmp_path)
        )

        expected = noise_floor_mde(run_dirs=run_dirs, variant_id="default", suite_id=SUITE, criterion_index=0)
        assert expected is not None
        # Non-degenerate, or the assertion below cannot fail: a floor of 0.000 is what an identical
        # pair of halves measures, and every wiring mistake also produces it.
        assert expected > 0.0, "the fixture's halves must disagree, or this test pins nothing"
        assert f"{expected:.4f}" in block
        assert ACTIVATION_FLOOR_METRIC in block

    def test_one_invocation_says_the_sample_cannot_support_a_floor(self, tmp_path: Path) -> None:
        block = activation_floor_report(
            run_dirs=_baseline(tmp_path, invocations=1),
            suite_id=SUITE,
            criterion_index=0,
            sidecar=_sidecar(tmp_path),
        )

        assert "No noise floor" in block
        # The cause, not just the absence: "at least 2 invocations" is a different remedy from
        # "too few rows scored", and a block that only said "no floor" sends a reader to buy rows.
        assert "at least 2 invocations" in block
        assert "NOT the same as a floor of zero" in block

    def test_a_wrong_criterion_index_names_the_precondition_that_failed(self, tmp_path: Path) -> None:
        block = activation_floor_report(
            run_dirs=_baseline(tmp_path), suite_id=SUITE, criterion_index=7, sidecar=_sidecar(tmp_path)
        )

        assert "No noise floor" in block
        assert "criterion 7" in block
        assert "scored a classification result" in block

    def test_a_missing_sidecar_is_an_empty_cache_rather_than_a_raise(self, tmp_path: Path) -> None:
        sidecar = _sidecar(tmp_path)
        assert not sidecar.exists(), "round 1 always hits this path"

        block = activation_floor_report(
            run_dirs=_baseline(tmp_path), suite_id=SUITE, criterion_index=0, sidecar=sidecar
        )

        assert "Noise floor" in block

    def test_a_negative_criterion_index_still_raises_at_the_boundary(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="criterion_index must be >= 0"):
            activation_floor_report(
                run_dirs=_baseline(tmp_path), suite_id=SUITE, criterion_index=-1, sidecar=_sidecar(tmp_path)
            )


class TestExecutionFloorReport:
    def test_the_block_states_the_floor_over_a_replicated_control_arm(self, tmp_path: Path) -> None:
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.2, 0.6], "r2": [0.4, 0.9]})

        block = execution_floor_report(
            run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, sidecar=_sidecar(tmp_path)
        )

        # The NUMBER, against a direct call — otherwise `floor.mde` could be swapped for any other
        # float field on the record and this stays green.
        expected = measure_execution_noise_floor(
            run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, model=UNRESOLVED_MODEL
        )
        assert expected is not None and expected.mde > 0.0
        assert f"{expected.mde:.4f}" in block
        assert EXECUTION_FLOOR_METRIC in block
        # And the sample shape the skill's prose says to read before quoting the floor.
        assert f"{expected.n_rows} row(s) scored in both halves" in block
        assert f"{expected.n_replicates} replicate(s) per row" in block

    def test_an_unresolvable_model_is_recorded_as_the_sentinel(self, tmp_path: Path, monkeypatch) -> None:
        """The one place the two tracks deliberately differ, so it is asserted rather than assumed.

        `NoiseFloor.model` is `str` with `min_length=1`, so this track cannot pass `None` through
        the way the activation composite does — and substituting a real-looking id would key a
        cache entry under a model nobody resolved.
        """
        run_dirs = weighted_arm(tmp_path, "incumbent", {"r1": [0.2, 0.6], "r2": [0.4, 0.9]})
        captured: dict[str, object] = {}

        def spy(**kwargs: object) -> NoiseFloor | None:
            captured.update(kwargs)
            return measure_execution_noise_floor(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("coder_eval.optimize.api.measure_execution_noise_floor", spy)
        block = execution_floor_report(
            run_dirs=run_dirs, variant_id="incumbent", suite_id=SUITE, sidecar=_sidecar(tmp_path)
        )

        assert captured["model"] == UNRESOLVED_MODEL, "the fixture rows record no model_used"
        assert "Noise floor" in block

    def test_a_single_replicate_tree_says_no_floor(self, tmp_path: Path) -> None:
        block = execution_floor_report(
            run_dirs=weighted_arm(tmp_path, "incumbent", {"r1": [0.2], "r2": [0.4]}),
            variant_id="incumbent",
            suite_id=SUITE,
            sidecar=_sidecar(tmp_path),
        )

        assert "No noise floor" in block
        assert "NOT the same as a floor of zero" in block


# A directory that does not exist, so a read would fail DIFFERENTLY from the guard's message —
# which is what proves the guard runs before any filesystem access rather than merely resembling it.
_MISSING = "/nonexistent/optimize-api-guard"


def _activation_call(run_dirs) -> str:
    return activation_floor_report(
        run_dirs=run_dirs, suite_id=SUITE, criterion_index=0, sidecar=Path(_MISSING) / "measurements.json"
    )


def _execution_call(run_dirs) -> str:
    return execution_floor_report(
        run_dirs=run_dirs,
        variant_id="incumbent",
        suite_id=SUITE,
        sidecar=Path(_MISSING) / "measurements.json",
    )


# Both entry points, so the guard is asserted on the SURFACE rather than on the private helper the
# two share: a composite that forgot to call it is exactly the regression this catches.
_ENTRY_POINTS = [pytest.param(_activation_call, id="activation"), pytest.param(_execution_call, id="execution")]


@pytest.mark.parametrize("call", _ENTRY_POINTS)
class TestRunDirsAreRejectedAtTheBoundary:
    def test_a_bare_string_raises_type_error_naming_the_argument(self, call) -> None:
        with pytest.raises(TypeError, match="run_dirs must be a sequence of pathlib"):
            call(_MISSING)

    def test_a_list_of_strings_raises_type_error(self, call) -> None:
        with pytest.raises(TypeError, match="string"):
            call([_MISSING])

    def test_a_bare_path_names_the_argument_rather_than_failing_downstream(self, call) -> None:
        # The likeliest of the three: every composite takes a LIST where the gate below it takes one
        # directory. Without the guard this raises "'PosixPath' object is not iterable" from
        # whichever comprehension reaches it first, naming neither the argument nor the fix.
        with pytest.raises(TypeError, match=r"pass \[dir\], not dir"):
            call(Path(_MISSING))

    def test_an_empty_sequence_is_a_caller_error_not_a_measurement(self, call) -> None:
        # Distinct from "dirs given, no rows found", which renders as a no-floor block: nothing was
        # named here, so there is no suite to report a reading about.
        with pytest.raises(ValueError, match="run_dirs is empty"):
            call([])
