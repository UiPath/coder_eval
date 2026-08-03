"""Tests for SandboxConfig.record_cli — generated CLI recording shims.

The load-bearing test here is the ROUND TRIP: generate a shim, actually run it,
then grade the log it wrote with the `cli_called` criterion. Writer and reader
ship in the same package precisely so that contract can be tested, rather than
asserted in prose across two repositories.
"""

import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from coder_eval.cli_recorder import parse_log, render_recorder
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    RECORD_CLI_DIR,
    RECORD_CLI_LOG,
    CliCalledCriterion,
    RecordedCli,
    SandboxConfig,
    StarterFile,
    StarterFilesSource,
)
from coder_eval.sandbox import Sandbox


def _sandbox(task_id: str, **kwargs) -> Sandbox:
    config = SandboxConfig(driver="tempdir", python=None, **kwargs)
    return Sandbox(config, task_id=task_id)


def _run_shim(sandbox_dir, tool: str, args: list[str]) -> subprocess.CompletedProcess:
    """Invoke a generated shim the way the agent's shell would."""
    shim = sandbox_dir / RECORD_CLI_DIR / tool
    return subprocess.run(
        [sys.executable, str(shim), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class TestGeneration:
    def test_generates_shim_cmd_twin_and_seeded_log(self):
        sandbox = _sandbox("record_gen", record_cli=[RecordedCli(tool="uip")])
        try:
            sandbox_dir = sandbox.setup()
            recorder_dir = sandbox_dir / RECORD_CLI_DIR
            assert (recorder_dir / "uip").is_file()
            # Windows PATHEXT lookup needs the .cmd; POSIX uses the extensionless twin.
            assert (recorder_dir / "uip.cmd").is_file()
            # Seeded empty: distinguishes "mock never ran" from "correct run made no calls".
            log = sandbox_dir / RECORD_CLI_LOG
            assert log.is_file()
            assert log.read_text(encoding="utf-8") == ""
        finally:
            sandbox.cleanup(preserve=False)

    def test_recorder_dir_is_path_prepended_before_user_mocks(self):
        sandbox = _sandbox("record_path", record_cli=[RecordedCli(tool="uip")], mock_path_dirs=["mocks"])
        try:
            sandbox_dir = sandbox.setup()
            (sandbox_dir / "mocks").mkdir(exist_ok=True)
            resolved = sandbox.resolved_mock_path_dirs
            assert resolved[0] == sandbox_dir / RECORD_CLI_DIR
        finally:
            sandbox.cleanup(preserve=False)

    def test_no_record_cli_leaves_no_directory(self):
        sandbox = _sandbox("record_absent")
        try:
            sandbox_dir = sandbox.setup()
            assert not (sandbox_dir / RECORD_CLI_DIR).exists()
            assert sandbox.resolved_mock_path_dirs == []
        finally:
            sandbox.cleanup(preserve=False)

    def test_collision_with_user_mock_raises(self):
        """Silently shadowing a task's own mock would make PATH order load-bearing."""
        sandbox = _sandbox(
            "record_clash",
            record_cli=[RecordedCli(tool="uip")],
            mock_path_dirs=["mocks"],
            template_sources=[
                StarterFilesSource(
                    type="starter_files",
                    files=[StarterFile(path="mocks/uip", content="#!/bin/sh\nexit 0\n")],
                )
            ],
        )
        try:
            with pytest.raises(RuntimeError, match="already provides one"):
                sandbox.setup()
        finally:
            if sandbox.sandbox_dir is not None:
                sandbox.cleanup(preserve=False)


class TestRecording:
    def test_records_argv_and_fails_without_running_anything(self):
        spec = RecordedCli(tool="uip", exit_code=1, stderr="uip: not connected\n")
        sandbox = _sandbox("record_offline", record_cli=[spec])
        try:
            sandbox_dir = sandbox.setup()
            proc = _run_shim(
                sandbox_dir,
                "uip",
                ["ixp", "projects", "configure-model", "proj-1", "--model", "gemini_2_5_pro"],
            )
            assert proc.returncode == 1
            assert proc.stderr == "uip: not connected\n"

            records = parse_log((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
            assert len(records) == 1
            assert records[0]["tool"] == "uip"
            assert records[0]["exit"] == 1
            assert records[0]["argv"] == [
                "ixp",
                "projects",
                "configure-model",
                "proj-1",
                "--model",
                "gemini_2_5_pro",
            ]
        finally:
            sandbox.cleanup(preserve=False)

    def test_stdout_text_is_emitted(self):
        spec = RecordedCli(tool="fake", exit_code=0, stdout='{"Result":"Success"}')
        sandbox = _sandbox("record_stdout", record_cli=[spec])
        try:
            sandbox_dir = sandbox.setup()
            proc = _run_shim(sandbox_dir, "fake", ["anything"])
            assert proc.returncode == 0
            assert proc.stdout == '{"Result":"Success"}'
        finally:
            sandbox.cleanup(preserve=False)

    def test_quoted_argument_with_spaces_stays_one_element(self):
        """The defect a flattened command line cannot represent."""
        sandbox = _sandbox("record_quoted", record_cli=[RecordedCli(tool="uip")])
        try:
            sandbox_dir = sandbox.setup()
            _run_shim(sandbox_dir, "uip", ["fields", "rename", "--group", "Invoice Header"])
            records = parse_log((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
            assert records[0]["argv"][-1] == "Invoice Header"
        finally:
            sandbox.cleanup(preserve=False)

    def test_multiline_argument_survives_as_one_element(self):
        """A heredoc-expanded JSON payload must not split into several records."""
        payload = '[\n  {"name": "Invoice Number"}\n]'
        sandbox = _sandbox("record_multiline", record_cli=[RecordedCli(tool="uip")])
        try:
            sandbox_dir = sandbox.setup()
            _run_shim(sandbox_dir, "uip", ["fields", "update-prompts", "--updates", payload])
            records = parse_log((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
            assert len(records) == 1
            assert records[0]["argv"][-1] == payload
        finally:
            sandbox.cleanup(preserve=False)

    def test_repeated_invocations_append_in_order(self):
        sandbox = _sandbox("record_append", record_cli=[RecordedCli(tool="uip")])
        try:
            sandbox_dir = sandbox.setup()
            for n in range(3):
                _run_shim(sandbox_dir, "uip", ["documents", "upload", f"doc{n}.pdf"])
            records = parse_log((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
            assert [r["argv"][-1] for r in records] == ["doc0.pdf", "doc1.pdf", "doc2.pdf"]
        finally:
            sandbox.cleanup(preserve=False)

    def test_several_tools_share_one_log_tagged_by_tool(self):
        sandbox = _sandbox(
            "record_multi",
            record_cli=[RecordedCli(tool="uip"), RecordedCli(tool="curl")],
        )
        try:
            sandbox_dir = sandbox.setup()
            _run_shim(sandbox_dir, "uip", ["projects", "list"])
            _run_shim(sandbox_dir, "curl", ["-s", "https://example.invalid"])
            records = parse_log((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
            assert [r["tool"] for r in records] == ["uip", "curl"]
        finally:
            sandbox.cleanup(preserve=False)


class TestRoundTripWithCliCalled:
    """Generate → run → grade. The contract this feature exists to guarantee."""

    def test_cli_called_grades_the_generated_log_with_no_log_path_configured(self):
        sandbox = _sandbox("record_roundtrip", record_cli=[RecordedCli(tool="uip")])
        try:
            sandbox_dir = sandbox.setup()
            _run_shim(
                sandbox_dir,
                "uip",
                ["ixp", "projects", "configure-model", "proj-1", "--model", "gemini_2_5_pro", "--output", "json"],
            )
            # No `log:` — the default points at where record_cli writes.
            criterion = CliCalledCriterion(
                description="switched to the capable model",
                verb="ixp projects configure-model",
                positional=["proj-1"],
                flags={"model": "gemini_2_5_pro"},
            )
            result = SuccessChecker(sandbox).check(criterion)
            assert result.score == 1.0, result.details
            assert result.error is None
        finally:
            sandbox.cleanup(preserve=False)

    def test_negative_guard_passes_on_a_seeded_empty_log(self):
        """A correct run that calls nothing must satisfy max_count: 0 — the seeded
        empty log is what separates that from a mock that never ran."""
        sandbox = _sandbox("record_roundtrip_neg", record_cli=[RecordedCli(tool="uip")])
        try:
            sandbox.setup()
            criterion = CliCalledCriterion(
                description="did not delete anything",
                verb="ixp projects delete",
                min_count=0,
                max_count=0,
            )
            result = SuccessChecker(sandbox).check(criterion)
            assert result.score == 1.0
            assert result.error is None
        finally:
            sandbox.cleanup(preserve=False)

    def test_negative_guard_catches_the_forbidden_call(self):
        sandbox = _sandbox("record_roundtrip_neg2", record_cli=[RecordedCli(tool="uip")])
        try:
            sandbox_dir = sandbox.setup()
            _run_shim(sandbox_dir, "uip", ["ixp", "projects", "delete", "proj-1", "-y"])
            criterion = CliCalledCriterion(
                description="did not delete anything",
                verb="ixp projects delete",
                min_count=0,
                max_count=0,
            )
            assert SuccessChecker(sandbox).check(criterion).score == 0.0
        finally:
            sandbox.cleanup(preserve=False)


class TestModelValidation:
    @pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", ".", "..", "", " uip"])
    def test_tool_must_be_a_bare_name(self, bad):
        with pytest.raises(ValidationError):
            RecordedCli(tool=bad)

    @pytest.mark.parametrize("field", ["mode", "response", "passthrough"])
    def test_unknown_field_rejected(self, field):
        """extra='forbid' catches a typo — and a config written against a shape
        this model does not (yet) have, such as a mode or a canned response."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RecordedCli(tool="uip", **{field: "x"})

    def test_defaults(self):
        spec = RecordedCli(tool="uip")
        # exit_code 1: an unconfigured tool should look like a failing one rather
        # than silently succeeding.
        assert spec.exit_code == 1
        assert (spec.stdout, spec.stderr) == ("", "")


class TestRenderedSource:
    def test_rendered_shim_is_valid_python_and_embeds_config(self):
        spec = RecordedCli(tool="uip", exit_code=3, stderr="boom\n")
        source = render_recorder(spec)
        compile(source, "uip", "exec")
        # Config arrives as literals; exec the module to read them back rather
        # than pattern-matching the rendered text.
        # __file__ must be present: the shim derives its log path from it.
        namespace: dict = {"__name__": "shim", "__file__": "uip"}
        exec(compile(source, "uip", "exec"), namespace)
        assert namespace["TOOL"] == "uip"
        assert namespace["EXIT_CODE"] == 3
        assert namespace["STDERR_TEXT"] == "boom\n"

    def test_rendered_shim_does_not_execute_anything(self):
        """It stubs a tool rather than proxying one: no subprocess, no exec."""
        source = render_recorder(RecordedCli(tool="uip"))
        for forbidden in ("subprocess", "execv", "execvp", "popen", "system("):
            assert forbidden not in source

    def test_rendered_shim_imports_nothing_from_coder_eval(self):
        """It runs inside the sandbox, where this package is not installed."""
        source = render_recorder(RecordedCli(tool="uip"))
        imports = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from ")) and "coder_eval" in line
        ]
        assert imports == []

    def test_rendered_shim_is_pure_ascii(self):
        """Written into arbitrary sandboxes and read by whatever python3 is there."""
        source = render_recorder(RecordedCli(tool="uip"))
        source.encode("ascii")

    def test_parse_log_skips_unparseable_lines(self):
        text = json.dumps({"tool": "uip", "argv": []}) + "\ngarbage\n\n"
        assert len(parse_log(text)) == 1
