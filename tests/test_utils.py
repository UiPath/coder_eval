"""Tests for coder_eval.utils version-info helpers."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from coder_eval.utils import _git_short_sha


def test_git_short_sha_returns_unknown_when_path_missing(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    assert _git_short_sha(missing) == "unknown"


def test_git_short_sha_returns_unknown_when_git_missing(tmp_path: Path):
    """FileNotFoundError on git binary is swallowed and returns 'unknown'."""
    with patch("coder_eval.utils.subprocess.run", side_effect=FileNotFoundError):
        assert _git_short_sha(tmp_path) == "unknown"


def test_git_short_sha_passes_utf8_encoding_kwargs(tmp_path: Path):
    """_git_short_sha must call subprocess.run with encoding='utf-8' / errors='replace'.

    Mocking subprocess.run bypasses the decoder, so we assert on the kwargs
    instead of on the decoded string — that's the actual contract that
    keeps Windows / corrupted-git stdout from crashing the run.
    """
    fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="abc1234\n", stderr="")
    with patch("coder_eval.utils.subprocess.run", return_value=fake_result) as mock_run:
        sha = _git_short_sha(tmp_path)
        assert sha == "abc1234"
        kwargs = mock_run.call_args.kwargs
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["text"] is True
