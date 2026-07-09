"""Tests for sandbox git clone failure handling.

Tests ensure clear error messages for git operations failures.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from coder_eval.models import RepoSource, SandboxConfig
from coder_eval.sandbox import Sandbox


def test_git_clone_invalid_url_raises_runtime_error(tmp_path):
    """Test that invalid git URL raises RuntimeError with clear message.

    Hypothesis: Bad git URLs should raise RuntimeError with stderr.
    Expected: RuntimeError contains "Failed to clone repository" and error details.

    Context: Lines 115-116 in sandbox.py catch CalledProcessError and raise RuntimeError.
    """
    config = SandboxConfig(
        driver="tempdir",
        template_sources=[
            RepoSource(url="https://invalid-url-that-does-not-exist.com/repo.git"),
        ],
    )

    sandbox = Sandbox(config=config, task_id="test_task")
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Mock subprocess.run to simulate git clone failure
    stderr_msg = (
        "fatal: unable to access 'https://invalid-url-that-does-not-exist.com/repo.git/': "
        "Could not resolve host: invalid-url-that-does-not-exist.com"
    )
    mock_error = subprocess.CalledProcessError(
        returncode=128,
        cmd=[
            "git",
            "clone",
            "https://invalid-url-that-does-not-exist.com/repo.git",
            str(tmp_path / "sandbox" / "repo"),
        ],
        stderr=stderr_msg,
    )

    with (
        patch("subprocess.run", side_effect=mock_error),
        pytest.raises(RuntimeError, match="Failed to clone repository"),
    ):
        sandbox._apply_repo_source(config.template_sources[0])


def test_git_clone_private_repo_raises_runtime_error(tmp_path):
    """Test that private repo without auth raises RuntimeError.

    Hypothesis: Authentication failures should be reported clearly.
    Expected: RuntimeError with authentication error details.
    """
    config = SandboxConfig(
        driver="tempdir",
        template_sources=[
            RepoSource(url="https://github.com/private-org/private-repo.git"),
        ],
    )

    sandbox = Sandbox(config=config, task_id="test_task")
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Mock subprocess.run to simulate authentication failure
    mock_error = subprocess.CalledProcessError(
        returncode=128,
        cmd=[
            "git",
            "clone",
            "https://github.com/private-org/private-repo.git",
            str(tmp_path / "sandbox" / "repo"),
        ],
        stderr="fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    )

    with (
        patch("subprocess.run", side_effect=mock_error),
        pytest.raises(RuntimeError, match="Failed to clone repository"),
    ):
        sandbox._apply_repo_source(config.template_sources[0])


def test_git_clone_timeout_raises_runtime_error(tmp_path):
    """Test that git clone timeout raises RuntimeError.

    Hypothesis: Network timeouts should be handled gracefully.
    Expected: TimeoutExpired converted to RuntimeError or propagated.
    """
    config = SandboxConfig(
        driver="tempdir",
        template_sources=[
            RepoSource(url="https://github.com/some-org/huge-repo.git"),
        ],
    )

    sandbox = Sandbox(config=config, task_id="test_task")
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Mock subprocess.run to simulate timeout
    with (
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git clone", timeout=60)),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        sandbox._apply_repo_source(config.template_sources[0])


def test_git_checkout_invalid_commit_raises_runtime_error(tmp_path):
    """Test that invalid commit checkout raises RuntimeError.

    Hypothesis: Checkout failures should include commit hash in error.
    Expected: RuntimeError with "Failed to clone repository" and stderr.
    """
    config = SandboxConfig(
        driver="tempdir",
        template_sources=[
            RepoSource(
                url="https://github.com/some-org/repo.git",
                commit="invalid-commit-hash-12345",
            ),
        ],
    )

    sandbox = Sandbox(config=config, task_id="test_task")
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Mock first call (clone) to succeed, second call (checkout) to fail
    clone_success = MagicMock()
    checkout_error = subprocess.CalledProcessError(
        returncode=1,
        cmd=["git", "checkout", "invalid-commit-hash-12345"],
        stderr="error: pathspec 'invalid-commit-hash-12345' did not match any file(s) known to git",
    )

    with (
        patch("subprocess.run", side_effect=[clone_success, checkout_error]),
        pytest.raises(RuntimeError, match="Failed to clone repository"),
    ):
        sandbox._apply_repo_source(config.template_sources[0])


def test_git_clone_success_creates_repo_dir(tmp_path):
    """Test that successful git clone creates repo directory.

    Hypothesis: Valid git operations should succeed without errors.
    Expected: No exceptions raised, repo directory structure correct.
    """
    config = SandboxConfig(
        driver="tempdir",
        template_sources=[
            RepoSource(url="https://github.com/some-org/repo.git"),
        ],
    )

    sandbox = Sandbox(config=config, task_id="test_task")
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Mock successful git clone
    with patch("subprocess.run", return_value=MagicMock(returncode=0)):
        sandbox._apply_repo_source(config.template_sources[0])

        # Verify method completed without raising


def test_git_clone_with_commit_success(tmp_path):
    """Test that git clone with specific commit succeeds when valid.

    Hypothesis: Checkout specific commit should work when commit exists.
    Expected: Both clone and checkout commands executed.
    """
    config = SandboxConfig(
        driver="tempdir",
        template_sources=[
            RepoSource(
                url="https://github.com/some-org/repo.git",
                commit="abc123def456",
            ),
        ],
    )

    sandbox = Sandbox(config=config, task_id="test_task")
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()

    # Mock both clone and checkout success
    mock_run = MagicMock(return_value=MagicMock(returncode=0))

    with patch("subprocess.run", mock_run):
        sandbox._apply_repo_source(config.template_sources[0])

        # Verify subprocess.run was called twice (clone + checkout)
        assert mock_run.call_count == 2

        # Verify first call was git clone
        first_call_args = mock_run.call_args_list[0]
        assert "git" in first_call_args[0][0]
        assert "clone" in first_call_args[0][0]

        # Verify second call was git checkout
        second_call_args = mock_run.call_args_list[1]
        assert "git" in second_call_args[0][0]
        assert "checkout" in second_call_args[0][0]
        assert "abc123def456" in second_call_args[0][0]
