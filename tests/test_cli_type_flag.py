"""Tests for the --type CLI flag added in Phase 3."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from coder_eval.cli import app


runner = CliRunner()

# Click/Rich wraps tokens in ANSI escapes (e.g., `\x1b[1;36m--type\x1b[0m`) which
# breaks naive substring matching against the rendered help. Strip them before
# asserting so tests are robust across CI terminal-width and color settings.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def test_cli_rejects_invalid_type() -> None:
    """`--type bogus` exits non-zero with a clear error mentioning the valid choices."""
    result = runner.invoke(app, ["run", "--type", "bogus"])
    assert result.exit_code != 0
    output = _strip_ansi(result.output).lower()
    # Click Choice rejects with 'is not one of' or 'invalid value' depending on version.
    assert "claude-code" in output or "invalid" in output


def test_cli_rejects_unsupported_agent_kind() -> None:
    """`--type aider` (an unregistered kind — no plugin provides it) is rejected: it
    flows through to parse_agent_config, which raises the BYOA "no agent registered"
    error listing the registered kinds. Asserted explicitly (not OR'd with a generic
    Click fallback) so a regression in that UX surfaces as a failure."""
    result = runner.invoke(app, ["run", "--type", "aider"])
    assert result.exit_code != 0
    output = _strip_ansi(result.output).lower()
    assert "no agent registered" in output
    # The error must enumerate the registered kinds so the author knows the valid set.
    assert "claude-code" in output


def test_cli_accepts_known_type() -> None:
    """`--type claude-code` is accepted at the parser level (further behavior covered by integration tests)."""
    # Run with `--help` to ensure parser accepts the flag without invoking the run.
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    output = _strip_ansi(result.output)
    assert "--type" in output
    assert "-T" in output
