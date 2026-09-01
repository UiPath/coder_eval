"""Executable contract for the argv ``action.yml`` builds from its inputs.

The composite action's two bash steps assemble two command lines — a ``uv tool
install`` and a ``coder-eval run`` — out of eight string inputs. Everything that
can go wrong there goes wrong *silently*: an extra dropped from the requirement
string installs a working CLI that is missing an agent, and a value mangled by
word splitting or pathname expansion reaches the CLI as a different value than
the workflow wrote, so the run measures something else and still exits 0.

These tests therefore execute the shipped script rather than reimplementing it.
Each step's ``run:`` body is pulled straight out of ``action.yml`` and run under
bash with ``uv`` / ``coder-eval`` replaced by stubs that record their argv, so the
assertions are about the real text that ships to consumers. A rewrite of the
script that changes the resulting command line fails here even if it looks
equivalent.

The design these tests pin: the action promotes NONE of ``coder-eval run``'s 21
flags to a named input. Everything goes through ``args``, one argv entry per
line, appended verbatim. That is what makes a ``-D`` override whose value is a
bracketed list (``key=[A,B,C]`` — a bash character class) survive; the earlier
whitespace-split input silently rewrote it to one name whenever a file in the
working directory happened to match.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION_YML = REPO_ROOT / "action.yml"

BASH = shutil.which("bash")

# The run step needs every CE_* name defined (`set -u`), so each case supplies only
# what it varies.
RUN_ENV_DEFAULTS = {
    "CE_ARGS": "",
    "CE_RUN_DIR": "runs/ci",
    "CE_ENV": "",
}

INSTALL_ENV_DEFAULTS = {
    "CE_VERSION": "9.9.9",
    "CE_EXTRAS": "",
    "CE_EXTRA_PACKAGES": "",
    "CE_INSTALL_FLAGS": "",
    "CE_ACTION_PATH": "/action-checkout",
}


def _step_script(step_name: str) -> str:
    """The ``run:`` body of a named step, as it ships.

    Read from action.yml rather than duplicated here: a test holding its own copy
    of the script asserts nothing about what consumers get.
    """
    data = yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))
    for step in data["runs"]["steps"]:
        if step.get("name") == step_name:
            return step["run"]
    raise AssertionError(f"action.yml has no step named {step_name!r}")


def _stub(dir_: Path, name: str) -> Path:
    """A fake executable recording its argv and its inherited ``CE_PROBE``, exit 0.

    Written in bash rather than Python on purpose. ``shell: bash`` on a Windows
    runner is Git Bash, which rewrites arguments that look like absolute POSIX
    paths on the way to a *native* Windows binary: a python-shebang stub gets
    ``/action-checkout`` as ``C:/Program Files/Git/action-checkout``, and
    switching that conversion off only moves the failure, because the shebang
    launcher then cannot hand python its own script path either. A bash stub
    never crosses that boundary, so argv arrives byte-for-byte everywhere.

    argv is recorded NUL-delimited instead of as JSON so a value carrying a
    quote, a backslash or a space needs no escaping on the way out of bash. Each
    invocation truncates the file; no test invokes the stub twice.

    ``CE_PROBE`` is how the env-passthrough test observes what the child actually
    received: the passthrough exports into the step's own shell, so only a process
    the script itself launches can report it.
    """
    record = dir_ / "argv.bin"
    # Forward slashes, not the native separator: the consumer is MSYS bash, which
    # reads `C:/...` but not every backslash form.
    quoted_dir = shlex.quote(str(dir_).replace("\\", "/"))
    exe = dir_ / name
    exe.write_text(
        "#!/usr/bin/env bash\n"
        f"d={quoted_dir}\n"
        ': > "$d/argv.bin"\n'
        'for a in "$@"; do printf "%s\\0" "$a" >> "$d/argv.bin"; done\n'
        'printf "%s" "${CE_PROBE-<unset>}" > "$d/probe.txt"\n',
        encoding="utf-8",
    )
    exe.chmod(0o755)
    return record


def _run(script: str, env: dict[str, str], *, cwd: Path, stub: str) -> tuple[int, list[str], str]:
    """Execute ``script`` with ``stub`` shadowing the real binary; return (rc, argv, stderr+stdout)."""
    bindir = cwd / "_stubbin"
    bindir.mkdir(exist_ok=True)
    record = _stub(bindir, stub)

    full_env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(cwd),
        "GITHUB_OUTPUT": str(cwd / "gh_output"),
        "GITHUB_STEP_SUMMARY": str(cwd / "gh_summary"),
        **env,
    }
    (cwd / "gh_output").touch()
    (cwd / "gh_summary").touch()

    proc = subprocess.run(
        [BASH, "-c", script],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    argv: list[str] = []
    if record.exists():
        # Trailing NUL terminates the last entry, so the split leaves an empty tail.
        argv = [part.decode() for part in record.read_bytes().split(b"\0")[:-1]]
    return proc.returncode, argv, proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def install_script() -> str:
    return _step_script("Install coder-eval")


@pytest.fixture(scope="module")
def run_script() -> str:
    return _step_script("Run coder-eval")


def _install(script: str, tmp_path: Path, **overrides: str) -> tuple[int, list[str], str]:
    return _run(script, {**INSTALL_ENV_DEFAULTS, **overrides}, cwd=tmp_path, stub="uv")


def _coder_eval(script: str, tmp_path: Path, **overrides: str) -> tuple[int, list[str], str]:
    return _run(script, {**RUN_ENV_DEFAULTS, **overrides}, cwd=tmp_path, stub="coder-eval")


def _outputs(tmp_path: Path) -> dict[str, str]:
    """The step's `$GITHUB_OUTPUT` writes, parsed."""
    text = (tmp_path / "gh_output").read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


class TestSharedParser:
    # `install-flags`, `extra-packages` and `args` are all "one entry per line,
    # verbatim". One implementation, copied into both step scripts because they
    # are separate bash processes. Copies drift; this is what stops them.
    def test_clean_lines_is_byte_identical_in_both_steps(self, install_script, run_script):
        pattern = re.compile(r"^clean_lines\(\) \{\n.*?^\}\n", re.S | re.M)
        a = pattern.search(install_script)
        b = pattern.search(run_script)
        assert a and b, "clean_lines() is missing from one of the step scripts"
        assert a.group(0) == b.group(0), "the two clean_lines() copies have drifted apart"


class TestInstallSpec:
    def test_defaults_install_the_pinned_release(self, install_script, tmp_path):
        rc, argv, out = _install(install_script, tmp_path)
        assert rc == 0, out
        assert argv == ["tool", "install", "coder-eval==9.9.9"]

    def test_local_installs_the_action_checkout(self, install_script, tmp_path):
        rc, argv, out = _install(install_script, tmp_path, CE_VERSION="local")
        assert rc == 0, out
        assert argv == ["tool", "install", "/action-checkout"]

    # Extras must land in the requirement string, not a follow-up install: the tool
    # environment's shims shadow every other coder-eval on PATH, so an extra added
    # beside it is never imported by the CLI the action goes on to invoke.
    def test_extras_are_composed_into_the_requirement(self, install_script, tmp_path):
        rc, argv, out = _install(install_script, tmp_path, CE_EXTRAS="codex")
        assert rc == 0, out
        assert argv == ["tool", "install", "coder-eval[codex]==9.9.9"]

    def test_extras_compose_onto_a_local_install_too(self, install_script, tmp_path):
        rc, argv, out = _install(install_script, tmp_path, CE_VERSION="local", CE_EXTRAS="codex")
        assert rc == 0, out
        assert argv == ["tool", "install", "/action-checkout[codex]"]

    def test_multiple_extras_stay_comma_joined(self, install_script, tmp_path):
        rc, argv, out = _install(install_script, tmp_path, CE_EXTRAS="antigravity,litellm")
        assert rc == 0, out
        assert argv == ["tool", "install", "coder-eval[antigravity,litellm]==9.9.9"]

    @pytest.mark.parametrize(
        "bad",
        [
            "codex;rm -rf /",  # shell metacharacters
            "codex litellm",  # space instead of comma
            "codex,",  # trailing comma
            ",codex",  # leading comma
            "-codex",  # must start alphanumeric
            "$(id)",  # command substitution
        ],
    )
    def test_malformed_extras_fail_before_installing(self, install_script, tmp_path, bad):
        rc, argv, out = _install(install_script, tmp_path, CE_EXTRAS=bad)
        assert rc != 0
        assert argv == [], "install ran despite malformed extras"
        assert "extras must be a comma-separated list" in out

    def test_extra_packages_become_one_with_flag_each(self, install_script, tmp_path):
        rc, argv, out = _install(install_script, tmp_path, CE_EXTRA_PACKAGES="./plugin-a\n../plugin-b\n")
        assert rc == 0, out
        assert argv == [
            "tool",
            "install",
            "--with",
            "./plugin-a",
            "--with",
            "../plugin-b",
            "coder-eval==9.9.9",
        ]

    # A specifier can contain `[`, `]`, `>` and `=`; a local path can contain a
    # space. None of those survive an unquoted expansion.
    def test_extra_package_specifiers_survive_verbatim(self, install_script, tmp_path):
        specs = ["coder-eval-uipath[dev]>=1.2,<2.0", "/opt/my plugin", "pkg!=0.2.144"]
        rc, argv, out = _install(install_script, tmp_path, CE_EXTRA_PACKAGES="\n".join(specs))
        assert rc == 0, out
        expected = ["tool", "install"]
        for spec in specs:
            expected += ["--with", spec]
        assert argv == [*expected, "coder-eval==9.9.9"]

    def test_install_flags_are_appended_one_per_line(self, install_script, tmp_path):
        rc, argv, out = _install(
            install_script,
            tmp_path,
            CE_INSTALL_FLAGS="--prerelease=allow\n--extra-index-url\nhttps://example.test/simple\n",
        )
        assert rc == 0, out
        assert argv == [
            "tool",
            "install",
            "--prerelease=allow",
            "--extra-index-url",
            "https://example.test/simple",
            "coder-eval==9.9.9",
        ]

    # Install flags precede --with and the requirement: uv accepts flags anywhere,
    # but a stable order is what makes these assertions meaningful at all.
    def test_install_flags_precede_extra_packages(self, install_script, tmp_path):
        rc, argv, out = _install(
            install_script,
            tmp_path,
            CE_INSTALL_FLAGS="--prerelease=allow",
            CE_EXTRA_PACKAGES="./plugin",
        )
        assert rc == 0, out
        assert argv == [
            "tool",
            "install",
            "--prerelease=allow",
            "--with",
            "./plugin",
            "coder-eval==9.9.9",
        ]

    @pytest.mark.parametrize("var", ["CE_EXTRA_PACKAGES", "CE_INSTALL_FLAGS"])
    def test_blank_lines_comments_and_padding_are_ignored(self, install_script, tmp_path, var):
        rc, argv, out = _install(install_script, tmp_path, **{var: "\n  ./plugin-a  \n\n# a comment\n\t./plugin-b\n\n"})
        assert rc == 0, out
        assert "./plugin-a" in argv and "./plugin-b" in argv
        assert not any("comment" in a for a in argv)
        assert not any(a.strip() != a for a in argv), f"an entry kept its padding: {argv}"


class TestRunArgs:
    def test_baseline_argv(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path)
        assert rc == 0, out
        assert argv == ["run", "--run-dir", "runs/ci", "--junit-xml", "runs/ci/junit.xml"]

    # There is no `tasks` input: task paths and globs are `args` entries like any
    # other. The CLI expands globs itself (`expand_task_files`), so passing them
    # unexpanded is not a loss — and it exits 1 when nothing matches, where a
    # shell would have silently passed the literal through.
    def test_task_globs_are_ordinary_args(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ARGS="skills/**/*.yaml\nrpa/*.yaml\n")
        assert rc == 0, out
        assert argv[-2:] == ["skills/**/*.yaml", "rpa/*.yaml"]

    def test_args_are_appended_one_entry_per_line(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ARGS="--tags\nsmoke\n--type\ncodex\n")
        assert rc == 0, out
        assert argv == [
            "run",
            "--run-dir",
            "runs/ci",
            "--junit-xml",
            "runs/ci/junit.xml",
            "--tags",
            "smoke",
            "--type",
            "codex",
        ]

    def test_args_blank_lines_comments_and_padding_are_ignored(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ARGS="\n  -v  \n\n# note\n\t--stream\n\n")
        assert rc == 0, out
        assert argv[-2:] == ["-v", "--stream"]

    # THE motivating case. `[...]` is a bash character class, so a whitespace-split
    # input drops list members whenever a file in the working directory matches.
    # A file is planted here so the test would fail under any implementation that
    # word-splits or glob-expands.
    def test_bracketed_override_survives_verbatim(self, run_script, tmp_path):
        (tmp_path / "agent.allowed_tools=Read").touch()
        override = "agent.allowed_tools=[Read,Write,Bash]"
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ARGS=f"-D\n{override}\n")
        assert rc == 0, out
        assert argv[-2:] == ["-D", override]

    def test_values_with_spaces_survive(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ARGS="--model\nmodel with spaces\n")
        assert rc == 0, out
        assert argv[-2:] == ["--model", "model with spaces"]

    def test_run_dir_reaches_the_cli(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_RUN_DIR="/tmp/runs")
        assert rc == 0, out
        assert argv[:5] == ["run", "--run-dir", "/tmp/runs", "--junit-xml", "/tmp/runs/junit.xml"]


class TestOutputs:
    # There is no `junit-path` input. The report belongs with the run it
    # describes, and both consumers that had the choice already put it there.
    def test_report_paths_are_derived_from_run_dir(self, run_script, tmp_path):
        rc, _, out = _coder_eval(run_script, tmp_path, CE_RUN_DIR="/tmp/runs")
        assert rc == 0, out
        assert _outputs(tmp_path) == {
            "run-dir": "/tmp/runs",
            "junit-path": "/tmp/runs/junit.xml",
            "run-md-path": "/tmp/runs/run.md",
        }

    def test_a_trailing_slash_does_not_double_the_separator(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_RUN_DIR="runs/ci/")
        assert rc == 0, out
        o = _outputs(tmp_path)
        assert o["junit-path"] == "runs/ci/junit.xml"
        assert o["run-md-path"] == "runs/ci/run.md"
        # --run-dir is forwarded as given; only the derived paths are normalised.
        assert argv[2] == "runs/ci/"

    # run.json/run.md are written even on a red run, so a consumer uploading
    # artifacts after a failure needs the paths.
    def test_outputs_are_written_before_a_failing_exit(self, run_script, tmp_path):
        bindir = tmp_path / "_stubbin"
        bindir.mkdir()
        _stub(bindir, "coder-eval")
        (bindir / "coder-eval").write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
        (bindir / "coder-eval").chmod(0o755)
        (tmp_path / "gh_output").touch()
        proc = subprocess.run(
            [BASH, "-c", _step_script("Run coder-eval")],
            cwd=tmp_path,
            env={
                "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                "HOME": str(tmp_path),
                "GITHUB_OUTPUT": str(tmp_path / "gh_output"),
                "GITHUB_STEP_SUMMARY": str(tmp_path / "gh_summary"),
                **RUN_ENV_DEFAULTS,
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 3, "the step must exit with coder-eval's own code"
        assert _outputs(tmp_path)["run-dir"] == "runs/ci"

    # The action no longer appends run.md to the job summary: a consumer that has
    # to redact the report first cannot undo a write that already happened.
    def test_nothing_is_written_to_the_job_summary(self, run_script, tmp_path):
        (tmp_path / "runs" / "ci").mkdir(parents=True)
        (tmp_path / "runs" / "ci" / "run.md").write_text("# report\n", encoding="utf-8")
        rc, _, out = _coder_eval(run_script, tmp_path)
        assert rc == 0, out
        assert (tmp_path / "gh_summary").read_text(encoding="utf-8") == ""


class TestEnvPassthrough:
    def test_env_reaches_the_child(self, run_script, tmp_path):
        rc, _, out = _coder_eval(run_script, tmp_path, CE_ENV="CE_PROBE=hello\n")
        assert rc == 0, out
        assert (tmp_path / "_stubbin" / "probe.txt").read_text(encoding="utf-8") == "hello"

    # The two multi-line inputs are parsed by different loops (env values are not
    # right-trimmed, because a value is the caller's data). Neither may consume
    # the other's content.
    def test_args_and_env_do_not_bleed_into_each_other(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ARGS="--stream\n", CE_ENV="CE_PROBE=v\n")
        assert rc == 0, out
        assert argv[-1] == "--stream"
        assert "CE_PROBE=v" not in argv
        assert (tmp_path / "_stubbin" / "probe.txt").read_text(encoding="utf-8") == "v"

    def test_a_value_containing_equals_is_kept_whole(self, run_script, tmp_path):
        rc, _, out = _coder_eval(run_script, tmp_path, CE_ENV="CE_PROBE=a=b=c\n")
        assert rc == 0, out
        assert (tmp_path / "_stubbin" / "probe.txt").read_text(encoding="utf-8") == "a=b=c"

    @pytest.mark.parametrize(
        ("bad", "message"),
        [
            ("NOEQUALS", "is not NAME=VALUE"),
            ("2LEADING_DIGIT=x", "invalid name"),
            ("has space=x", "invalid name"),
            ("has-dash=x", "invalid name"),
        ],
    )
    def test_malformed_env_fails_before_running(self, run_script, tmp_path, bad, message):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ENV=bad)
        assert rc != 0
        assert argv == [], "coder-eval ran despite a malformed env entry"
        assert message in out

    # A caller who omits `NAME=` would otherwise have the secret printed verbatim
    # in the error, and GitHub only masks values it already knows are secrets.
    def test_a_malformed_entry_is_reported_by_position_not_by_value(self, run_script, tmp_path):
        rc, _, out = _coder_eval(run_script, tmp_path, CE_ENV="s3cr3t-token-value")
        assert rc != 0
        assert "s3cr3t-token-value" not in out
        assert "entry #1" in out

    # An `env` value carrying a newline splits into a second entry, because the
    # loop is line-based. That used to be an argv-rewrite: the pairs were
    # `export`ed into the step's own shell, which is where CE_ARGS and
    # CE_RUN_DIR are read from AFTER the loop. They are collected and handed to
    # `env` now, so the injected entry reaches the child as data and nothing
    # else. Reachable without a hostile author: any interpolated value or a
    # rotated multi-line secret.
    @pytest.mark.parametrize("hijack", ["CE_ARGS", "CE_RUN_DIR", "GITHUB_OUTPUT"])
    def test_a_newline_in_a_value_cannot_rewrite_the_step(self, run_script, tmp_path, hijack):
        rc, argv, out = _coder_eval(
            run_script,
            tmp_path,
            CE_ARGS="tasks/real.yaml\n",
            CE_RUN_DIR="runs/ci",
            CE_ENV=f"API_BASE=x\n{hijack}=/tmp/hijacked\n",
        )
        assert rc == 0, out
        assert "tasks/real.yaml" in argv, "the caller's task path was dropped"
        assert "/tmp/hijacked" not in argv
        assert argv[:5] == ["run", "--run-dir", "runs/ci", "--junit-xml", "runs/ci/junit.xml"]

    # PATH is the sharpest of these: it decides WHICH coder-eval runs, and the
    # name filter admits it. $GITHUB_PATH is the scoped, log-visible alternative.
    @pytest.mark.parametrize("name", ["PATH", "BASH_ENV", "LD_PRELOAD", "IFS"])
    def test_reserved_names_are_rejected_before_running(self, run_script, tmp_path, name):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ENV=f"{name}=/tmp/evil")
        assert rc != 0
        assert argv == [], "coder-eval ran despite a reserved env name"
        assert "reserved name" in out
        assert name in out
