"""Tests for the per-task review generation + validation wrapper."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from dashboard.review import generate_reviews, validate_run_reviews


def _write_review(run_path: Path, task_id: str, **overrides) -> Path:
    """Write a per-task review.json under the standard layout."""
    task_dir = run_path / "default" / task_id / "00"
    task_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task_id,
        "summary": "ok",
        "tags": ["criteria-bug"],
        "created_at": "2026-05-08T12:00:00Z",
    }
    payload.update(overrides)
    p = task_dir / "review.json"
    p.write_text(json.dumps(payload))
    return p


def _write_index(run_path: Path, entries: list[dict]) -> Path:
    p = run_path / "review_index.json"
    p.write_text(json.dumps({"generated_at": "2026-05-08T12:00:00Z", "reviews": entries}))
    return p


def _index_entry(task_id: str, **overrides) -> dict:
    base = {
        "task_id": task_id,
        "variant_id": "default",
        "replicate": "00",
        "tags": ["criteria-bug"],
        "summary_excerpt": "ok",
    }
    base.update(overrides)
    return base


def test_validate_accepts_well_formed_run(tmp_path):
    run = tmp_path / "run-1"
    _write_review(run, "t1")
    _write_review(run, "t2", tags=["agent-loop", "infra"])
    _write_index(
        run,
        [_index_entry("t1"), _index_entry("t2", tags=["agent-loop", "infra"])],
    )
    assert validate_run_reviews(run) == 2


def test_validate_accepts_empty_run(tmp_path):
    """A run with no failures still gets an empty index."""
    run = tmp_path / "run-1"
    run.mkdir()
    _write_index(run, [])
    assert validate_run_reviews(run) == 0


def test_validate_rejects_missing_index(tmp_path):
    run = tmp_path / "run-1"
    run.mkdir()
    with pytest.raises(FileNotFoundError, match=r"review_index\.json"):
        validate_run_reviews(run)


def test_validate_rejects_malformed_index_json(tmp_path):
    run = tmp_path / "run-1"
    run.mkdir()
    (run / "review_index.json").write_text("{not json}")
    with pytest.raises(ValueError, match="not valid JSON"):
        validate_run_reviews(run)


def test_validate_rejects_non_kebab_tag(tmp_path):
    run = tmp_path / "run-1"
    _write_review(run, "t1", tags=["NotKebab"])
    _write_index(run, [_index_entry("t1", tags=["NotKebab"])])
    with pytest.raises(ValueError, match="not a kebab-case slug"):
        validate_run_reviews(run)


def test_validate_rejects_long_summary(tmp_path):
    run = tmp_path / "run-1"
    _write_review(run, "t1", summary="x" * 600)
    _write_index(run, [_index_entry("t1")])
    with pytest.raises(ValueError, match="exceeds 480 chars"):
        validate_run_reviews(run)


def test_validate_rejects_too_many_tags(tmp_path):
    run = tmp_path / "run-1"
    _write_review(run, "t1", tags=[f"tag-{i}" for i in range(9)])
    _write_index(run, [_index_entry("t1", tags=[f"tag-{i}" for i in range(9)])])
    with pytest.raises(ValueError, match="more than 8 tags"):
        validate_run_reviews(run)


def test_validate_rejects_missing_required_key(tmp_path):
    run = tmp_path / "run-1"
    task_dir = run / "default" / "t1" / "00"
    task_dir.mkdir(parents=True)
    (task_dir / "review.json").write_text(
        json.dumps({"task_id": "t1", "tags": ["x"], "created_at": "2026-05-08T12:00:00Z"})
    )
    _write_index(run, [_index_entry("t1")])
    with pytest.raises(ValueError, match="missing required key 'summary'"):
        validate_run_reviews(run)


def test_validate_rejects_task_id_path_mismatch(tmp_path):
    """review.json's task_id must match the directory name."""
    run = tmp_path / "run-1"
    # Write a file under default/t1/00/ but the body claims task_id "t2".
    task_dir = run / "default" / "t1" / "00"
    task_dir.mkdir(parents=True)
    (task_dir / "review.json").write_text(
        json.dumps(
            {
                "task_id": "t2",
                "summary": "ok",
                "tags": ["criteria-bug"],
                "created_at": "2026-05-08T12:00:00Z",
            }
        )
    )
    _write_index(run, [_index_entry("t1")])
    with pytest.raises(ValueError, match="does not match path task_id"):
        validate_run_reviews(run)


def test_validate_rejects_extra_review_file(tmp_path):
    """A review.json on disk with no matching index entry is a contract violation."""
    run = tmp_path / "run-1"
    _write_review(run, "t1")
    _write_review(run, "t2")
    _write_index(run, [_index_entry("t1")])  # missing t2
    with pytest.raises(ValueError, match="review/index mismatch"):
        validate_run_reviews(run)


def test_validate_rejects_extra_index_entry(tmp_path):
    """An index entry with no matching review.json is a contract violation."""
    run = tmp_path / "run-1"
    _write_review(run, "t1")
    _write_index(run, [_index_entry("t1"), _index_entry("t2")])  # extra t2
    with pytest.raises(ValueError, match="review/index mismatch"):
        validate_run_reviews(run)


def test_validate_rejects_invalid_created_at(tmp_path):
    run = tmp_path / "run-1"
    _write_review(run, "t1", created_at="yesterday")
    _write_index(run, [_index_entry("t1")])
    with pytest.raises(ValueError, match="ISO 8601"):
        validate_run_reviews(run)


def test_validate_rejects_missing_summary_excerpt(tmp_path):
    """summary_excerpt is part of the contract (Step 4) — index without it is invalid."""
    run = tmp_path / "run-1"
    _write_review(run, "t1")
    entry = _index_entry("t1")
    del entry["summary_excerpt"]
    _write_index(run, [entry])
    with pytest.raises(ValueError, match="missing required key 'summary_excerpt'"):
        validate_run_reviews(run)


def test_validate_rejects_long_summary_excerpt(tmp_path):
    run = tmp_path / "run-1"
    _write_review(run, "t1")
    _write_index(run, [_index_entry("t1", summary_excerpt="x" * 500)])
    with pytest.raises(ValueError, match="summary_excerpt exceeds"):
        validate_run_reviews(run)


def test_generate_reviews_cleans_up_on_subprocess_timeout(tmp_path):
    """Partial review files from a timed-out skill must be removed before re-raise."""
    from dashboard import review as review_mod

    run = tmp_path / "run-1"
    _write_review(run, "t1")
    _write_index(run, [_index_entry("t1")])
    leftover = run / "default" / "t1" / "00" / "review.json"
    assert leftover.exists()

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=900)

    with (
        patch.object(review_mod.subprocess, "run", side_effect=_raise_timeout),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        review_mod.generate_reviews(run)

    assert not leftover.exists()
    assert not (run / "review_index.json").exists()


def test_generate_reviews_cleans_up_on_validation_failure(tmp_path):
    """A skill that exits cleanly but produces malformed review files must be cleaned up."""
    from dashboard import review as review_mod

    run = tmp_path / "run-1"
    # Write an invalid review.json (created_at = 'yesterday').
    _write_review(run, "t1", created_at="yesterday")
    _write_index(run, [_index_entry("t1")])

    with patch.object(review_mod.subprocess, "run"), pytest.raises(ValueError, match="ISO 8601"):
        review_mod.generate_reviews(run)

    assert not (run / "default" / "t1" / "00" / "review.json").exists()
    assert not (run / "review_index.json").exists()


def test_generate_reviews_invokes_skill(tmp_path):
    run = tmp_path / "run-1"
    _write_review(run, "t1")
    _write_index(run, [_index_entry("t1")])

    with patch("dashboard.review.subprocess.run") as mock_run:
        result = generate_reviews(run)

    assert result == run / "review_index.json"
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "claude"
    assert any("/coder-eval-review" in arg for arg in cmd)


def test_generate_reviews_strips_claudecode_env(tmp_path):
    run = tmp_path / "run-1"
    _write_review(run, "t1")
    _write_index(run, [_index_entry("t1")])
    with (
        patch.dict("os.environ", {"CLAUDECODE": "1", "HOME": "/home/test"}),
        patch("dashboard.review.subprocess.run") as mock_run,
    ):
        generate_reviews(run)
    env = mock_run.call_args[1]["env"]
    assert "CLAUDECODE" not in env
    assert "HOME" in env
