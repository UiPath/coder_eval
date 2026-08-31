"""Executable contract for the argv ``action.yml`` builds from its inputs.

The composite action's two bash steps assemble two command lines — a ``uv tool
install`` and a ``coder-eval run`` — out of ten string inputs. Everything that can
go wrong there goes wrong *silently*: an extra dropped from the requirement string
installs a working CLI that is missing an agent, and a value mangled by word
splitting or pathname expansion reaches the CLI as a different value than the
workflow wrote, so the run measures something else and still exits 0.

These tests therefore execute the shipped script rather than reimplementing it.
Each step's ``run:`` body is pulled straight out of ``action.yml`` and run under
bash with ``uv`` / ``coder-eval`` replaced by stubs that record their argv, so the
assertions are about the real text that ships to consumers. A rewrite of the
script that changes the resulting command line fails here even if it looks
equivalent.

The motivating bug is the ``args``/``extra-args`` split
(``test_bracketed_override_*``): ``extra-args`` is deliberately word-split, which
also means it is pathname-expanded, so a ``-D`` override whose value is a
bracketed list (``key=[A,B,C]`` — a bash character class) is intact only while no
file in the working directory happens to match. One file named
``...=A`` silently rewrites a three-name list to one name.
"""

from __future__ import annotations

import json
import os
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
    "CE_TASKS": "",
    "CE_TAGS": "",
    "CE_MODEL": "",
    "CE_EXTRA_ARGS": "",
    "CE_ARGS": "",
    "CE_RUN_DIR": "runs/ci",
    "CE_JUNIT": "junit.xml",
    "CE_SUMMARY": "false",
    "CE_ENV": "",
    "CE_MIN_SCORE": "",
}

INSTALL_ENV_DEFAULTS = {
    "CE_VERSION": "9.9.9",
    "CE_EXTRAS": "",
    "CE_EXTRA_PACKAGES": "",
    "CE_PRERELEASE": "false",
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

    ``CE_PROBE`` is how the env-passthrough test observes what the child actually
    received: the passthrough exports into the step's own shell, so only a process
    the script itself launches can report it.
    """
    record = dir_ / "argv.json"
    exe = dir_ / name
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, pathlib\n"
        f"d = pathlib.Path({str(dir_)!r})\n"
        "p = d / 'argv.json'\n"
        "lines = p.read_text().splitlines() if p.exists() else []\n"
        "lines.append(json.dumps(sys.argv[1:]))\n"
        "p.write_text('\\n'.join(lines) + '\\n')\n"
        "(d / 'probe.txt').write_text(os.environ.get('CE_PROBE', '<unset>'))\n",
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
        # Git Bash (the `shell: bash` of a Windows runner) rewrites arguments
        # that look like absolute POSIX paths into Windows form on the way to a
        # native binary, so `/action-checkout` reached the stub as
        # `C:/Program Files/Git/action-checkout` and the composition assertions
        # failed for a reason that has nothing to do with the action. These two
        # switch that conversion off. Harmless on POSIX, where nothing reads them.
        "MSYS2_ARG_CONV_EXCL": "*",
        "MSYS_NO_PATHCONV": "1",
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
        for line in record.read_text(encoding="utf-8").splitlines():
            if line.strip():
                argv = json.loads(line)
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

    # The value is interpolated into a spec that reaches a resolver, so it is
    # validated rather than trusted. Rejecting is the point: a silently accepted
    # `codex extra` would resolve to something other than what was asked for.
    @pytest.mark.parametrize(
        "bad",
        ["codex;echo pwned", "codex extra", "-codex", "codex,", ",codex", "code x", "codex]"],
    )
    def test_malformed_extras_fail_the_step(self, install_script, tmp_path, bad):
        rc, argv, out = _install(install_script, tmp_path, CE_EXTRAS=bad)
        assert rc != 0
        assert "::error::extras must be" in out
        assert argv == [], "install must not run with an unvalidated extras value"

    def test_extra_packages_become_one_with_flag_each(self, install_script, tmp_path):
        rc, argv, out = _install(
            install_script,
            tmp_path,
            CE_EXTRA_PACKAGES="./vendor/plugin\nsome-plugin>=1.2",
        )
        assert rc == 0, out
        assert argv == [
            "tool",
            "install",
            "--with",
            "./vendor/plugin",
            "--with",
            "some-plugin>=1.2",
            "coder-eval==9.9.9",
        ]

    # A specifier contains `>` and `[`, and a local path can contain spaces; each
    # line is therefore one argv entry rather than a word-split string.
    def test_extra_package_specifiers_survive_verbatim(self, install_script, tmp_path):
        rc, argv, out = _install(
            install_script,
            tmp_path,
            CE_EXTRA_PACKAGES="pkg[all]>=1.0,<2.0\n./a dir/plugin",
        )
        assert rc == 0, out
        assert argv[2:] == [
            "--with",
            "pkg[all]>=1.0,<2.0",
            "--with",
            "./a dir/plugin",
            "coder-eval==9.9.9",
        ]

    def test_blank_lines_comments_and_padding_are_ignored(self, install_script, tmp_path):
        rc, argv, out = _install(
            install_script,
            tmp_path,
            CE_EXTRA_PACKAGES="  ./plugin  \n\n# a comment\n\t\n./other\r\n",
        )
        assert rc == 0, out
        assert argv == [
            "tool",
            "install",
            "--with",
            "./plugin",
            "--with",
            "./other",
            "coder-eval==9.9.9",
        ]

    def test_prerelease_true_allows_prereleases(self, install_script, tmp_path):
        rc, argv, out = _install(install_script, tmp_path, CE_PRERELEASE="true")
        assert rc == 0, out
        assert argv == ["tool", "install", "--prerelease=allow", "coder-eval==9.9.9"]

    @pytest.mark.parametrize("falsy", ["false", ""])
    def test_prerelease_off_passes_no_flag(self, install_script, tmp_path, falsy):
        rc, argv, out = _install(install_script, tmp_path, CE_PRERELEASE=falsy)
        assert rc == 0, out
        assert "--prerelease=allow" not in argv

    # "yes"/"1"/"True" are the plausible typos, and a silently-ignored one would
    # let a resolution failure look like a missing release.
    @pytest.mark.parametrize("bad", ["yes", "1", "True", "allow"])
    def test_non_boolean_prerelease_fails_the_step(self, install_script, tmp_path, bad):
        rc, argv, out = _install(install_script, tmp_path, CE_PRERELEASE=bad)
        assert rc != 0
        assert "::error::prerelease must be" in out
        assert argv == []


class TestRunArgs:
    def test_baseline_argv(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path)
        assert rc == 0, out
        assert argv == ["run", "--run-dir", "runs/ci", "--junit-xml", "junit.xml"]

    def test_args_are_appended_one_entry_per_line(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ARGS="-e\nexperiments/nightly.yaml\n-v")
        assert rc == 0, out
        assert argv[-3:] == ["-e", "experiments/nightly.yaml", "-v"]

    def test_args_blank_lines_comments_and_padding_are_ignored(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ARGS="  -v  \n\n# why not\n\t\n-q\r\n")
        assert rc == 0, out
        assert argv[-2:] == ["-v", "-q"]

    # `args` runs before `extra-args`, which is the documented order; a reordering
    # would change precedence for a repeated flag.
    def test_args_precede_extra_args_and_tasks(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(
            run_script,
            tmp_path,
            CE_ARGS="-e\nexperiments/nightly.yaml",
            CE_EXTRA_ARGS="-j 4",
            CE_TASKS="tasks/a.yaml tasks/b.yaml",
        )
        assert rc == 0, out
        assert argv[-6:] == [
            "-e",
            "experiments/nightly.yaml",
            "-j",
            "4",
            "tasks/a.yaml",
            "tasks/b.yaml",
        ]

    # THE reason `args` exists. `[A,B,C]` is a bash character class, and a file in
    # the working directory matching it rewrites the value. Both halves of the pair
    # run with that file present, so the difference is the channel and nothing else.
    def test_bracketed_override_survives_args(self, run_script, tmp_path):
        override = "sandbox.docker.env_passthrough_extra=[AUTH_TOKEN,BASE_URL]"
        (tmp_path / "sandbox.docker.env_passthrough_extra=A").touch()
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ARGS=f"-D\n{override}")
        assert rc == 0, out
        assert argv[-2:] == ["-D", override]

    def test_bracketed_override_is_mangled_by_extra_args(self, run_script, tmp_path):
        override = "sandbox.docker.env_passthrough_extra=[AUTH_TOKEN,BASE_URL]"
        (tmp_path / "sandbox.docker.env_passthrough_extra=A").touch()
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_EXTRA_ARGS=f"-D {override}")
        assert rc == 0, out
        # Documenting the hazard, not endorsing it: extra-args stays word-split for
        # compatibility, so this is why a value like this must go through `args`.
        assert argv[-2:] == ["-D", "sandbox.docker.env_passthrough_extra=A"]

    def test_values_with_spaces_survive_args(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_ARGS="--title\ntwo words")
        assert rc == 0, out
        assert argv[-2:] == ["--title", "two words"]

    # Every existing input keeps its shape — the `args` insertion sits between
    # --model and extra-args and must not disturb either side.
    def test_tags_and_model_still_pass_through(self, run_script, tmp_path):
        rc, argv, out = _coder_eval(run_script, tmp_path, CE_TAGS="smoke,fast", CE_MODEL="claude-sonnet-5")
        assert rc == 0, out
        assert argv == [
            "run",
            "--run-dir",
            "runs/ci",
            "--junit-xml",
            "junit.xml",
            "--tags",
            "smoke,fast",
            "--model",
            "claude-sonnet-5",
        ]

    def test_env_passthrough_still_reaches_the_child(self, run_script, tmp_path):
        # Not a new input, but the `args` loop is a second `while read` in the same
        # script, inserted downstream of this one. A heredoc wired to the wrong
        # variable would leave the argv assertions above green while silently
        # dropping every forwarded credential, so pin the passthrough here too.
        rc, _, out = _coder_eval(run_script, tmp_path, CE_ENV="CE_PROBE=hello\n# note\n")
        assert rc == 0, out
        assert (tmp_path / "_stubbin" / "probe.txt").read_text(encoding="utf-8") == "hello"

    def test_args_and_env_do_not_bleed_into_each_other(self, run_script, tmp_path):
        # The concrete confusion the two loops invite: an `args` entry must never be
        # exported, and an `env` entry must never become an argument.
        rc, argv, out = _coder_eval(
            run_script,
            tmp_path,
            CE_ENV="CE_PROBE=from-env",
            CE_ARGS="--model=x",
        )
        assert rc == 0, out
        assert argv[-1] == "--model=x"
        assert (tmp_path / "_stubbin" / "probe.txt").read_text(encoding="utf-8") == "from-env"
