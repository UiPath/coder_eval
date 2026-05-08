"""Generate per-task review.json artifacts via Claude Code.

Mirrors :mod:`dashboard.analysis`: invokes the ``/coder-eval-review`` slash
command on a completed run directory. The skill writes one ``review.json``
per failed task at ``<task_dir>/review.json`` plus a ``review_index.json``
digest at the run root. This wrapper validates the index and the per-task
files structurally — it does *not* check tag membership against any
vocabulary, since tags are intentionally an open vocabulary (drift is
caught by ``scripts/lint_review_vocabulary.py``).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import CODER_EVAL_DIR
from .run import _env_without_claudecode

REQUIRED_REVIEW_KEYS = ("task_id", "summary", "tags", "created_at")
REQUIRED_INDEX_KEYS = ("task_id", "variant_id", "replicate", "tags", "summary_excerpt")
MAX_SUMMARY_CHARS = 480
MAX_SUMMARY_EXCERPT_CHARS = 200
MAX_TAGS_PER_REVIEW = 8
KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# ISO 8601 UTC: 2026-05-08T12:00:00Z, optional fractional seconds, optional offset.
ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def _validate_tag(tag: Any, *, where: str) -> None:
    if not isinstance(tag, str) or not KEBAB_CASE_RE.match(tag):
        raise ValueError(f"{where}: tag {tag!r} is not a kebab-case slug")


def _validate_review(payload: Any, *, path: Path) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: top-level must be an object")
    for key in REQUIRED_REVIEW_KEYS:
        if key not in payload:
            raise ValueError(f"{path}: missing required key '{key}'")
    if not isinstance(payload["task_id"], str) or not payload["task_id"].strip():
        raise ValueError(f"{path}: 'task_id' must be a non-empty string")
    if not isinstance(payload["summary"], str):
        raise ValueError(f"{path}: 'summary' must be a string")
    if len(payload["summary"]) > MAX_SUMMARY_CHARS:
        raise ValueError(f"{path}: summary exceeds {MAX_SUMMARY_CHARS} chars")
    if not isinstance(payload["created_at"], str) or not ISO_TIMESTAMP_RE.match(payload["created_at"]):
        raise ValueError(f"{path}: 'created_at' must be ISO 8601 UTC, got {payload['created_at']!r}")
    if not isinstance(payload["tags"], list):
        raise ValueError(f"{path}: 'tags' must be a list")
    if len(payload["tags"]) > MAX_TAGS_PER_REVIEW:
        raise ValueError(f"{path}: more than {MAX_TAGS_PER_REVIEW} tags")
    for tag in payload["tags"]:
        _validate_tag(tag, where=str(path))


def _validate_index_entry(entry: Any, *, path: Path) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"{path}: index entry is not a dict: {entry!r}")
    for key in REQUIRED_INDEX_KEYS:
        if key not in entry:
            raise ValueError(f"{path}: index entry missing required key '{key}'")
    if not isinstance(entry["summary_excerpt"], str):
        raise ValueError(f"{path}: index entry 'summary_excerpt' must be a string")
    if len(entry["summary_excerpt"]) > MAX_SUMMARY_EXCERPT_CHARS:
        raise ValueError(f"{path}: index summary_excerpt exceeds {MAX_SUMMARY_EXCERPT_CHARS} chars")
    if not isinstance(entry["tags"], list):
        raise ValueError(f"{path}: index entry 'tags' must be a list")
    for tag in entry["tags"]:
        _validate_tag(tag, where=f"{path}:{entry.get('task_id', '?')}")


def _index_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("variant_id", "")),
        str(entry.get("task_id", "")),
        str(entry.get("replicate", "")),
    )


def validate_run_reviews(run_path: Path) -> int:
    """Validate every review.json under run_path + the run-level index.

    Enforces:
    - Each review.json's ``task_id`` matches its parent directory name
      (otherwise the index/UI would silently misattribute it).
    - ``review_index.json`` entries are 1:1 with the on-disk review.json
      files (a missing or extra entry is a contract violation).
    - Required keys, types, length caps, kebab-case tags, ISO 8601 timestamps.

    Returns the count of per-task review.json files validated. Raises
    :class:`ValueError` on the first contract violation.
    """
    index_path = run_path / "review_index.json"
    if not index_path.exists():
        raise FileNotFoundError(f"review_index.json was not generated at {index_path}")

    try:
        with open(index_path) as f:
            index_payload = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{index_path} is not valid JSON: {exc}") from exc

    if not isinstance(index_payload, dict) or not isinstance(index_payload.get("reviews"), list):
        raise ValueError(f"{index_path}: must be an object with a 'reviews' list")

    index_keys: set[tuple[str, str, str]] = set()
    for entry in index_payload["reviews"]:
        _validate_index_entry(entry, path=index_path)
        index_keys.add(_index_key(entry))

    review_paths = sorted(run_path.glob("*/*/*/review.json"))
    review_keys: set[tuple[str, str, str]] = set()
    for rp in review_paths:
        try:
            with open(rp) as f:
                payload = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{rp} is not valid JSON: {exc}") from exc
        _validate_review(payload, path=rp)
        rel = rp.relative_to(run_path)
        variant_id, task_id, replicate = rel.parts[0], rel.parts[1], rel.parts[2]
        if payload["task_id"] != task_id:
            raise ValueError(f"{rp}: task_id {payload['task_id']!r} does not match path task_id {task_id!r}")
        review_keys.add((variant_id, task_id, replicate))

    if review_keys != index_keys:
        missing_in_index = sorted(review_keys - index_keys)
        missing_files = sorted(index_keys - review_keys)
        raise ValueError(
            f"{index_path}: review/index mismatch; "
            f"missing_in_index={missing_in_index[:5]}, missing_files={missing_files[:5]}"
        )

    return len(review_paths)


def _cleanup_review_artifacts(run_path: Path) -> None:
    """Remove every review.json and review_index.json under run_path.

    Used after a failed/timed-out skill invocation or a validation failure so
    the upload step doesn't ship a partial / malformed review tree. After
    cleanup, the rest of the pipeline behaves as if the run pre-dates the
    review feature (which is already a supported path).
    """
    for p in run_path.glob("*/*/*/review.json"):
        p.unlink(missing_ok=True)
    (run_path / "review_index.json").unlink(missing_ok=True)


def generate_reviews(run_path: Path) -> Path:
    """Invoke ``/coder-eval-review`` on a completed run directory.

    The skill writes one ``review.json`` per failed task and a
    ``review_index.json`` digest at the run root. After the subprocess
    returns, both the index and every per-task file are validated; on
    timeout, subprocess error, or validation failure, all partial review
    artifacts are removed before re-raising so the upload step never
    publishes half-written data.

    Returns the path to ``review_index.json``.
    """
    try:
        rel_path = run_path.relative_to(CODER_EVAL_DIR)
    except ValueError:
        rel_path = run_path

    try:
        subprocess.run(
            [
                "claude",
                "--print",
                "--permission-mode",
                "bypassPermissions",
                f"/coder-eval-review {rel_path}",
            ],
            cwd=str(CODER_EVAL_DIR),
            env=_env_without_claudecode(),
            check=True,
            timeout=900,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        _cleanup_review_artifacts(run_path)
        raise

    try:
        validate_run_reviews(run_path)
    except (FileNotFoundError, ValueError):
        _cleanup_review_artifacts(run_path)
        raise

    return run_path / "review_index.json"
