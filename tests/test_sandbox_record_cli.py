"""Tests for SandboxConfig.record_cli — generated CLI recording shims.

The load-bearing test here is the ROUND TRIP: generate a shim, actually run it,
then grade the log it wrote with the `cli_called` criterion. Writer and reader
ship in the same package precisely so that contract can be tested, rather than
asserted in prose across two repositories.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.invocation_log import parse_log, render_recorder
from coder_eval.models import (
    RECORD_CLI_DIR,
    RECORD_CLI_LOG,
    CliCalledCriterion,
    CliResponse,
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


# The two rendered shim shapes. Only the second splices in argv_match.py, so an
# invariant asserted on the first alone proves nothing about the interesting half.
SHIM_SHAPES = (
    RecordedCli(tool="uip"),
    RecordedCli(tool="uip", responses=[CliResponse(when={"verb": "ixp dummy1"}, stdout="ok")]),
)


def _records(text: str) -> list[dict]:
    """Just the records; parse_log also returns the unusable count."""
    usable, _ = parse_log(text)
    return [record for _, record in usable]


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
            # The property resolves symlinks; comparing an unresolved path passes on
            # Linux/Windows and fails wherever the tempdir traverses one (macOS /var).
            assert resolved[0] == (sandbox_dir / RECORD_CLI_DIR).resolve()
        finally:
            sandbox.cleanup(preserve=False)

    def test_reused_target_dir_does_not_carry_a_prior_runs_log(self, tmp_path):
        """DIRECT_WRITE does not clear the target dir, so a preserved log let a
        previous run's invocations score this one with zero agent activity."""
        target = tmp_path / "artifacts"
        stale = target / RECORD_CLI_LOG
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(
            json.dumps({"tool": "uip", "argv": ["ixp", "projects", "delete", "proj-1"]}) + "\n",
            encoding="utf-8",
        )
        sandbox = _sandbox("record_reuse", record_cli=[RecordedCli(tool="uip")])
        sandbox.setup(target_dir=target)
        assert (target / RECORD_CLI_LOG).read_text(encoding="utf-8") == ""
        criterion = CliCalledCriterion(description="deleted the project", verb="ixp projects delete", min_count=1)
        assert SuccessChecker(sandbox).check(criterion).score == 0.0

    def test_stale_shim_for_an_undeclared_tool_is_removed(self, tmp_path):
        """A shim left by a previous run would stay on PATH shadowing the real tool."""
        target = tmp_path / "artifacts"
        (target / RECORD_CLI_DIR).mkdir(parents=True, exist_ok=True)
        (target / RECORD_CLI_DIR / "curl").write_text("stale", encoding="utf-8")
        sandbox = _sandbox("record_stale_shim", record_cli=[RecordedCli(tool="uip")])
        sandbox.setup(target_dir=target)
        assert not (target / RECORD_CLI_DIR / "curl").exists()
        assert (target / RECORD_CLI_DIR / "uip").is_file()

    def test_recorder_dir_is_excluded_from_plugin_discovery(self):
        """A shim must not win the `uip` lookup that pins PLUGIN_TOOLS_DIR.

        It is not inside a node_modules/@uipath tree, so letting it win made
        resolve_uipath_plugin_dir return None and silently stop exporting the pin
        to every run_command criterion -- for the documented `tool: uip` example.
        """
        sandbox = _sandbox("record_plugin_dir", record_cli=[RecordedCli(tool="uip")])
        try:
            sandbox_dir = sandbox.setup()
            recorder = str((sandbox_dir / RECORD_CLI_DIR).resolve())
            sandbox.set_command_base_path(f"{recorder}{os.pathsep}{os.environ.get('PATH', '')}")
            assert recorder in sandbox.uip_search_path.split(os.pathsep)
            assert recorder not in sandbox._plugin_discovery_path().split(os.pathsep)
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


class TestInvokedThroughPath:
    """Exercise the shim the way the agent does -- bare name, resolved via PATH.

    Every other test runs it as `sys.executable <abs path>`, which bypasses the
    three mechanisms the agent actually depends on: the baked shebang, the +x bit,
    and the PATH prepend. Without this, removing any of them kept the suite green.
    """

    @pytest.mark.skipif(os.name == "nt", reason="POSIX shebang + exec bit path")
    def test_bare_name_through_path_records_and_is_gradeable(self):
        sandbox = _sandbox("record_path_exec", record_cli=[RecordedCli(tool="uip", exit_code=3)])
        try:
            sandbox_dir = sandbox.setup()
            recorder_dir = sandbox_dir / RECORD_CLI_DIR
            env = {**os.environ, "PATH": f"{recorder_dir}{os.pathsep}{os.environ['PATH']}"}
            proc = subprocess.run(
                ["uip", "ixp", "projects", "list", "--output", "json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                check=False,
            )
            assert proc.returncode == 3, proc.stderr
            criterion = CliCalledCriterion(description="listed", verb="ixp projects list")
            assert SuccessChecker(sandbox).check(criterion).score == 1.0
        finally:
            sandbox.cleanup(preserve=False)

    def test_shim_returns_with_stdin_left_open(self):
        """The invariant the docstring claims: stdin is never read, so an open pipe
        cannot hang the task. Nothing asserted it before."""
        sandbox = _sandbox("record_stdin", record_cli=[RecordedCli(tool="uip", exit_code=2)])
        try:
            sandbox_dir = sandbox.setup()
            shim = sandbox_dir / RECORD_CLI_DIR / "uip"
            proc = subprocess.Popen(
                [sys.executable, str(shim), "ixp", "projects", "list"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Deliberately never write or close stdin before waiting.
                assert proc.wait(timeout=20) == 2
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()
            records = _records((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
            assert records[0]["argv"] == ["ixp", "projects", "list"]
        finally:
            sandbox.cleanup(preserve=False)

    def test_shebang_is_an_absolute_interpreter(self):
        """`#!/usr/bin/env python3` resolved through the PATH this feature prepends,
        so `tool: python3` made the shim re-exec itself until the task timed out."""
        source = render_recorder(RecordedCli(tool="uip"))
        shebang = source.splitlines()[0]
        assert shebang.startswith("#!")
        interpreter = shebang[2:]
        assert os.path.isabs(interpreter), shebang
        assert "env " not in shebang

    def test_cmd_twin_body_uses_the_absolute_interpreter(self):
        sandbox = _sandbox("record_cmd_body", record_cli=[RecordedCli(tool="uip")])
        try:
            sandbox_dir = sandbox.setup()
            # newline="" so the CRLF survives -- read_text() would translate it away
            # on Windows and the assertion below would pass vacuously.
            body = (sandbox_dir / RECORD_CLI_DIR / "uip.cmd").read_text(encoding="utf-8", newline="")
            assert "%~dp0uip" in body
            assert "\r\n" in body, "cmd needs CRLF"
            # A bare `python` would resolve through the prepended dir too.
            assert '"python"' not in body and "\npython " not in body
            assert os.path.isabs(body.splitlines()[-1].split('"')[1])
        finally:
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

            records = _records((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
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
            records = _records((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
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
            records = _records((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
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
            records = _records((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
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
            records = _records((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
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
    @pytest.mark.parametrize(
        "bad",
        [
            "../evil",
            "a/b",
            "a\\b",
            ".",
            "..",
            "",
            " uip",
            # Not path-shaped, but interpolated into generated source: these emitted
            # an unparseable shim, which the path-only cases never caught.
            'a"""b',
            'x") or 1 or ("',
            "a\nb",
            "a b",
            "a;b",
            "a$b",
            "a`b",
        ],
    )
    def test_tool_must_be_an_executable_name(self, bad):
        with pytest.raises(ValidationError):
            RecordedCli(tool=bad)

    @pytest.mark.parametrize("reserved", ["python", "python3", "env", "sh", "bash", "node", "git", "uv", "cmd"])
    def test_reserved_tool_names_rejected(self, reserved):
        """Shadowing these breaks the harness, not the tool under test: the shim's own
        interpreter, or the shell run_command criteria use. `tool: python3` hung the
        task outright by re-execing itself."""
        with pytest.raises(ValidationError, match="reserved"):
            RecordedCli(tool=reserved)

    @pytest.mark.parametrize("name", ["PYTHON3", "Python.exe"])
    def test_reserved_names_are_case_and_exe_aware(self, name):
        with pytest.raises(ValidationError, match="reserved"):
            RecordedCli(tool=name)

    def test_log_filename_as_tool_rejected(self):
        """It overwrote the log every cli_called criterion reads by default."""
        with pytest.raises(ValidationError, match="invocation log"):
            RecordedCli(tool="calls.jsonl")

    @pytest.mark.parametrize("name", ["uip.cmd", "uip.bat"])
    def test_windows_twin_name_as_tool_rejected(self, name):
        with pytest.raises(ValidationError, match="Windows twin"):
            RecordedCli(tool=name)

    @pytest.mark.parametrize("bad_exit", [256, -1, 300])
    def test_exit_code_outside_posix_range_rejected(self, bad_exit):
        """sys.exit truncates mod 256, so exit_code: 256 made a 'failing' tool exit 0
        while the log still recorded 256."""
        with pytest.raises(ValidationError):
            RecordedCli(tool="uip", exit_code=bad_exit)

    def test_exit_code_bounds_are_inclusive(self):
        assert RecordedCli(tool="uip", exit_code=0).exit_code == 0
        assert RecordedCli(tool="uip", exit_code=255).exit_code == 255

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

    @pytest.mark.parametrize("spec", SHIM_SHAPES, ids=("no_rules", "with_rules"))
    def test_rendered_shim_does_not_execute_anything(self, spec):
        """It stubs a tool rather than proxying one: no subprocess, no exec."""
        source = render_recorder(spec)
        for forbidden in ("subprocess", "execv", "execvp", "popen", "system("):
            assert forbidden not in source

    @pytest.mark.parametrize("spec", SHIM_SHAPES, ids=("no_rules", "with_rules"))
    def test_rendered_shim_imports_nothing_from_coder_eval(self, spec):
        """It runs inside the sandbox, where this package is not installed."""
        source = render_recorder(spec)
        imports = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from ")) and "coder_eval" in line
        ]
        assert imports == []

    @pytest.mark.parametrize("spec", SHIM_SHAPES, ids=("no_rules", "with_rules"))
    def test_rendered_shim_is_pure_ascii(self, spec):
        """Written into arbitrary sandboxes and read by whatever python3 is there.

        Parametrized over both shapes because only the rules-bearing one splices in
        another module's source -- the half that can actually break any of these
        three invariants, and the half the unparametrized versions never rendered.
        """
        source = render_recorder(spec)
        source.encode("ascii")

    def test_parse_log_separates_usable_from_unusable(self):
        text = (
            json.dumps({"tool": "uip", "argv": ["a"]})
            + "\ngarbage\n\n"
            + json.dumps({"tool": "uip", "argv": "not-a-list"})
            + "\n"
        )
        usable, unusable = parse_log(text)
        assert [argv for argv, _ in usable] == [["a"]]
        # An argv that is not list[str] is unusable, not a non-match.
        assert unusable == 2


class TestPerInvocationResponses:
    """`responses:` — one shadowed tool answering each subcommand differently.

    The reason the shim is more than a recorder: an agent that reads
    `ixp projects list` and acts on what came back cannot be evaluated by a stub
    that returns the same line for everything it types.
    """

    @staticmethod
    def _spec() -> RecordedCli:
        return RecordedCli(
            tool="uip",
            exit_code=1,
            stderr="uip: unknown command\n",
            responses=[
                CliResponse(when={"verb": "ixp dummy1"}, stdout="response1\n"),
                CliResponse(when={"verb": "ixp dummy2"}, stdout="response2\n"),
            ],
        )

    def test_each_verb_gets_its_own_response(self):
        sandbox = _sandbox("record_responses", record_cli=[self._spec()])
        try:
            sandbox_dir = sandbox.setup()
            first = _run_shim(sandbox_dir, "uip", ["ixp", "dummy1"])
            second = _run_shim(sandbox_dir, "uip", ["ixp", "dummy2"])
            assert (first.returncode, first.stdout) == (0, "response1\n")
            assert (second.returncode, second.stdout) == (0, "response2\n")
        finally:
            sandbox.cleanup(preserve=False)

    def test_unmatched_invocation_falls_back_to_the_entry_defaults(self):
        sandbox = _sandbox("record_responses_fallback", record_cli=[self._spec()])
        try:
            sandbox_dir = sandbox.setup()
            proc = _run_shim(sandbox_dir, "uip", ["ixp", "dummy3"])
            assert proc.returncode == 1
            assert proc.stdout == ""
            assert "unknown command" in proc.stderr
        finally:
            sandbox.cleanup(preserve=False)

    def test_log_names_the_rule_that_answered(self):
        """ "Returned the default" and "rule 1 answered" are otherwise the same line."""
        sandbox = _sandbox("record_responses_log", record_cli=[self._spec()])
        try:
            sandbox_dir = sandbox.setup()
            _run_shim(sandbox_dir, "uip", ["ixp", "dummy2"])
            _run_shim(sandbox_dir, "uip", ["ixp", "dummy3"])
            records = _records((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))
            assert records[0]["rule"] == 1
            assert records[0]["exit"] == 0
            assert "rule" not in records[1], "no rule matched, so none may be claimed"
            assert records[1]["exit"] == 1
        finally:
            sandbox.cleanup(preserve=False)

    def test_first_matching_rule_wins(self):
        """Order is the author's disambiguation tool, so the general rule last."""
        spec = RecordedCli(
            tool="uip",
            responses=[
                CliResponse(when={"verb": "ixp projects get proj-1"}, stdout="specific\n"),
                CliResponse(when={"verb": "ixp projects get"}, stdout="generic\n"),
            ],
        )
        sandbox = _sandbox("record_responses_order", record_cli=[spec])
        try:
            sandbox_dir = sandbox.setup()
            assert _run_shim(sandbox_dir, "uip", ["ixp", "projects", "get", "proj-1"]).stdout == "specific\n"
            assert _run_shim(sandbox_dir, "uip", ["ixp", "projects", "get", "proj-9"]).stdout == "generic\n"
        finally:
            sandbox.cleanup(preserve=False)

    def test_rule_can_match_on_flags_and_positional(self):
        spec = RecordedCli(
            tool="uip",
            responses=[
                CliResponse(
                    when={"verb": "ixp projects get", "positional": ["proj-1"], "flags": {"output": "json"}},
                    stdout='{"id": "proj-1"}',
                ),
                CliResponse(when={"verb": "ixp projects get"}, stdout="proj-1 (table)\n"),
            ],
        )
        sandbox = _sandbox("record_responses_flags", record_cli=[spec])
        try:
            sandbox_dir = sandbox.setup()
            asked_json = _run_shim(sandbox_dir, "uip", ["ixp", "projects", "get", "proj-1", "--output", "json"])
            asked_table = _run_shim(sandbox_dir, "uip", ["ixp", "projects", "get", "proj-1"])
            other_project = _run_shim(sandbox_dir, "uip", ["ixp", "projects", "get", "proj-2", "--output", "json"])
            assert asked_json.stdout == '{"id": "proj-1"}'
            assert asked_table.stdout == "proj-1 (table)\n"
            assert other_project.stdout == "proj-1 (table)\n"
        finally:
            sandbox.cleanup(preserve=False)

    def test_stderr_and_exit_code_are_per_rule(self):
        spec = RecordedCli(
            tool="uip",
            exit_code=0,
            responses=[CliResponse(when={"verb": "ixp projects get missing"}, exit_code=4, stderr="not found\n")],
        )
        sandbox = _sandbox("record_responses_failure", record_cli=[spec])
        try:
            sandbox_dir = sandbox.setup()
            proc = _run_shim(sandbox_dir, "uip", ["ixp", "projects", "get", "missing"])
            assert (proc.returncode, proc.stderr) == (4, "not found\n")
            # The entry default still applies to everything else, including its 0.
            assert _run_shim(sandbox_dir, "uip", ["ixp", "projects", "list"]).returncode == 0
        finally:
            sandbox.cleanup(preserve=False)

    def test_the_pattern_that_served_the_response_also_grades_it(self):
        """One semantic across both surfaces: same facets, same verdict.

        A rule and a criterion written from the same pattern must agree, or a task
        stubs one invocation and grades another.
        """
        pattern = {"verb": "ixp projects configure-model", "positional": ["proj-1"], "flags": {"model": "pro"}}
        spec = RecordedCli(tool="uip", responses=[CliResponse(when=dict(pattern), stdout="ok\n")])
        sandbox = _sandbox("record_responses_parity", record_cli=[spec])
        try:
            sandbox_dir = sandbox.setup()
            served = _run_shim(sandbox_dir, "uip", ["ixp", "projects", "configure-model", "proj-1", "--model", "pro"])
            assert served.stdout == "ok\n", "the rule did not match, so the grading half proves nothing"
            criterion = CliCalledCriterion(description="configured the model", **pattern)
            assert SuccessChecker(sandbox).check(criterion).score == 1.0
        finally:
            sandbox.cleanup(preserve=False)

    def test_a_rule_evaluation_fault_is_recorded_and_fails_the_grading(self):
        """The shim swallows a matcher fault so the stub does not crash, but the
        record must say so: without it, an eval-config fault is byte-identical to a
        legitimate no-match and the task scores as if the agent never made the call.

        FlagMatch compiles at load, so the only way to reach this is to corrupt a
        rendered shim -- which is the point: the branch is defense in depth, and
        nothing else exercises it.
        """
        sandbox = _sandbox("record_rule_fault", record_cli=[self._spec()])
        try:
            sandbox_dir = sandbox.setup()
            shim = sandbox_dir / RECORD_CLI_DIR / "uip"
            source = shim.read_text(encoding="utf-8")
            # A spec no matcher can evaluate, standing in for any future shim fault.
            broken = source.replace("'verb_spellings': [['ixp', 'dummy1']]", "'verb_spellings': 5", 1)
            assert broken != source, "the rule literal moved; update this test"
            shim.write_text(broken, encoding="utf-8")

            proc = _run_shim(sandbox_dir, "uip", ["ixp", "dummy1"])
            assert proc.returncode == 1, "the stub must still answer, not crash"
            assert "response matching failed" in proc.stderr

            record = _records((sandbox_dir / RECORD_CLI_LOG).read_text(encoding="utf-8"))[0]
            assert "rule" not in record
            assert "TypeError" in record["rule_error"]

            criterion = CliCalledCriterion(description="called dummy1", verb="ixp dummy1")
            result = SuccessChecker(sandbox).check(criterion)
            assert result.score == 0.0
            assert "could not evaluate its response rules" in (result.error or "")
        finally:
            sandbox.cleanup(preserve=False)

    def test_matcher_is_embedded_only_when_rules_exist(self):
        """A shim with no rules never consults the matcher, so it does not carry it."""
        plain = render_recorder(RecordedCli(tool="uip"))
        with_rules = render_recorder(RecordedCli(tool="uip", responses=[CliResponse(when={"verb": "ixp dummy1"})]))
        assert "argv_match.py" not in plain
        assert "def argv_matches" not in plain
        assert "def argv_matches" in with_rules
        # Both must be valid Python: the embedded half lands mid-file.
        compile(plain, "shim", "exec")
        compile(with_rules, "shim", "exec")

    def test_embedded_matcher_is_the_shipped_source_verbatim(self):
        """Not a paraphrase: the shim's matcher IS coder_eval/argv_match.py."""
        from coder_eval import argv_match

        shipped = Path(argv_match.__file__).read_text(encoding="utf-8")
        rendered = render_recorder(RecordedCli(tool="uip", responses=[CliResponse(when={"verb": "ixp dummy1"})]))
        assert shipped.strip() in rendered

    def test_response_rule_needs_a_facet(self):
        """A catch-all rule is the entry's own default; two ways to say it is one too many."""
        with pytest.raises(ValidationError, match="at least one of verb"):
            CliResponse(when={})

    @pytest.mark.parametrize(
        ("responses", "expected"),
        [
            ([{"when": {"verb": "ixp x"}, "stdout": "a"}, {"when": {"verb": "ixp x"}, "stdout": "b"}], "duplicate"),
            ([{"when": {"verb": "ixp projects"}}, {"when": {"verb": "ixp projects get"}}], "already claimed"),
            (
                [{"when": {"verb": "ixp projects"}}, {"when": {"verb_any_of": ["ixp projects get", "ixp projects x"]}}],
                "already claimed",
            ),
        ],
        ids=("exact_duplicate", "general_above_specific", "every_alternative_covered"),
    )
    def test_a_rule_an_earlier_rule_already_claims_is_rejected(self, responses, expected):
        """First-match-wins makes such a rule dead, and the rest of this surface
        hard-errors on every declaration that cannot take effect."""
        with pytest.raises(ValidationError, match=expected):
            RecordedCli(tool="uip", responses=responses)

    @pytest.mark.parametrize(
        "responses",
        [
            [{"when": {"verb": "ixp projects get"}}, {"when": {"verb": "ixp projects"}}],
            [{"when": {"verb": "ixp projects", "flags": {"o": "j"}}}, {"when": {"verb": "ixp projects get"}}],
            [{"when": {"verb": "ixp projects", "positional": ["p1"]}}, {"when": {"verb": "ixp projects get"}}],
            [{"when": {"verb": "ixp projects", "value_flags": []}}, {"when": {"verb": "ixp projects get"}}],
            [{"when": {"verb": "ixp a"}}, {"when": {"verb": "ixp b"}}],
        ],
        ids=("specific_first", "general_has_flag", "general_has_positional", "parsing_differs", "unrelated"),
    )
    def test_a_reachable_rule_is_not_rejected(self, responses):
        """The check must stay narrow: an earlier rule that constrains anything
        beyond its verb does NOT claim everything a later rule would, and two
        rules parsing argv differently cannot be compared by verb prefix at all."""
        assert len(RecordedCli(tool="uip", responses=responses).responses) == 2

    def test_a_bare_string_when_is_rejected_with_the_fix(self):
        """One shape for a pattern. A lone string leaves which of six facets it sets
        to inference, and reads enough like a command line to invite flags."""
        with pytest.raises(ValidationError, match=r'use \{verb: "ixp dummy1"\}'):
            CliResponse(when="ixp dummy1")
