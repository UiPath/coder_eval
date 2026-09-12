"""``coder_eval.harbor.reward`` — the C1.1 verifier shim's reward writer.

Table test over the three no-file exclusion cases the design doc (C1.1 /
C3) calls out: an ungraded row, a missing ``task.json``, and a malformed
``task.json`` must all write NO reward file and raise, distinctly from a
measured row (including a genuine zero) writing the file. The whole point of
this module is that those two outcomes are never confusable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.harbor.reward import RegradeError, RewardWriteSkippedError, compute_reward, write_reward
from coder_eval.models import AgentKind, EvaluationResult, FinalStatus
from coder_eval.path_utils import TASK_JSON_FILENAME


def _write_task_json(run_dir: Path, **overrides: object) -> None:
    """Write a run_dir/task.json from an EvaluationResult, with field overrides."""
    fields: dict[str, object] = {
        "task_id": "t",
        "task_description": "d",
        "variant_id": "default",
        "agent_type": AgentKind.CLAUDE_CODE,
        "started_at": datetime(2020, 1, 1),
        "final_status": FinalStatus.SUCCESS,
        "iteration_count": 1,
    }
    fields.update(overrides)
    result = EvaluationResult(**fields)  # type: ignore[arg-type]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / TASK_JSON_FILENAME).write_text(result.model_dump_json(), encoding="utf-8")


class TestComputeReward:
    """Every path through compute_reward: measured (incl. zero) vs. the three unmeasured cases."""

    def test_measured_row_returns_weighted_score(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        _write_task_json(run_dir, final_status=FinalStatus.SUCCESS, weighted_score=0.75)
        assert compute_reward(run_dir) == {"reward": 0.75}

    def test_measured_zero_is_not_confused_with_unmeasured(self, tmp_path: Path) -> None:
        """A criterion suite that genuinely scored 0.0 must still write the file.

        This is the case the whole design guards: `weighted_score or 0.0` would
        pass this test AND the ungraded test below identically, which is exactly
        the CE049 defect. `weighted_score is not None` is the only correct test.
        """
        run_dir = tmp_path / "run"
        _write_task_json(run_dir, final_status=FinalStatus.FAILURE, weighted_score=0.0)
        assert compute_reward(run_dir) == {"reward": 0.0}

    def test_ungraded_row_raises_reward_write_skipped(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        _write_task_json(run_dir, final_status=FinalStatus.NOT_GRADED, weighted_score=None)
        with pytest.raises(RewardWriteSkippedError):
            compute_reward(run_dir)

    def test_missing_task_json_raises_regrade_error(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        with pytest.raises(RegradeError):
            compute_reward(run_dir)

    def test_malformed_task_json_raises_regrade_error(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / TASK_JSON_FILENAME).write_text("{not json", encoding="utf-8")
        with pytest.raises(RegradeError):
            compute_reward(run_dir)


class TestWriteReward:
    """The file-writing half: confirms the no-file contract, not just the exception."""

    def test_writes_reward_json_for_a_measured_row(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        _write_task_json(run_dir, final_status=FinalStatus.SUCCESS, weighted_score=0.5)
        out = tmp_path / "verifier" / "reward.json"

        rewards = write_reward(run_dir, out)

        assert rewards == {"reward": 0.5}
        assert json.loads(out.read_text(encoding="utf-8")) == {"reward": 0.5}

    def test_writes_no_file_for_an_ungraded_row(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        _write_task_json(run_dir, final_status=FinalStatus.NOT_GRADED, weighted_score=None)
        out = tmp_path / "verifier" / "reward.json"

        with pytest.raises(RewardWriteSkippedError):
            write_reward(run_dir, out)

        assert not out.exists()
        assert not out.parent.exists(), "must not even create the destination dir on a skipped write"

    def test_writes_no_file_when_task_json_is_missing(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        out = tmp_path / "verifier" / "reward.json"

        with pytest.raises(RegradeError):
            write_reward(run_dir, out)

        assert not out.exists()

    def test_reward_json_is_a_flat_dict_per_harbor_contract(self, tmp_path: Path) -> None:
        """Harbor's VerifierResult.rewards is dict[str, float | int] | None, never a bare scalar."""
        run_dir = tmp_path / "run"
        _write_task_json(run_dir, final_status=FinalStatus.SUCCESS, weighted_score=1.0)
        out = tmp_path / "verifier" / "reward.json"

        write_reward(run_dir, out)

        parsed = json.loads(out.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        assert parsed == {"reward": 1.0}
