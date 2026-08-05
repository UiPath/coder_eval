"""Docker user/permission isolation barrier: staging, permission primitives,
skill-DOCS projection, per-harness uid-drop wiring, and the root-in-container
EACCES acceptance proof.

Layers:
- Host-runnable (no root, no docker): staging strips criteria; the skill-DOCS
  projection copies only docs; argv adds the skill-DOCS mount and NO ``--user``;
  the Dockerfile bakes the ``agent`` uid; ``container_perms`` no-ops off-root; the
  per-harness spawn-seam wiring flips on ``agent_run_uid``.
- ``@pytest.mark.docker_root`` (root + Linux, via ``make test-docker-isolation``):
  the six-surface EACCES-as-agent-uid proof, lock/chown sequencing, AND a real
  dropped-CLI acceptance (``TestRealDroppedCliAcceptance``) that runs subprocesses
  through the actual baked setpriv drop shim and asserts uid==2000 + agent-HOME
  writable + grader/task_full EACCES. Bind-mount EACCES is Linux-authoritative
  (native overlayfs); on macOS Docker Desktop the uid-remap defeats bind-mount
  chmod, so these assertions are validated only on Linux CI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from coder_eval.isolation import container_perms
from coder_eval.isolation.docker_runner import DockerRunner
from coder_eval.models import (
    AGENT_GID,
    AGENT_HOME,
    AGENT_UID,
    CONTAINER_DROP_SHIM,
    CONTAINER_INPUT_DIR,
    CONTAINER_SKILL_DOCS_DIR,
    PLUGIN_AGENT_ALLOWED_SUBDIRS,
    plugin_path,
    project_plugin_for_agent,
)
from coder_eval.orchestration.task_loader import load_task


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO_ROOT / "docker" / "Dockerfile"
_DROP_SHIM = _REPO_ROOT / "docker" / "coder_eval_drop_privilege.sh"
_FIX = Path(__file__).parent / "_fixtures" / "tasks"


def _raise_erofs(*_a, **_k):
    """Stand-in for a chown/chmod failing on a :ro bind mount (EROFS)."""
    raise OSError(30, "Read-only file system")


# ---------------------------------------------------------------------------
# project_plugin_for_agent — allowlist projection
# ---------------------------------------------------------------------------
class TestProjectPluginForAgent:
    def _make_plugin(self, root: Path) -> None:
        (root / "skills").mkdir(parents=True)
        (root / "skills" / "SKILL.md").write_text("skill doc", encoding="utf-8")
        (root / "commands").mkdir()
        (root / "commands" / "x.md").write_text("cmd", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "check_x.py").write_text("assert EXPECTED == observed", encoding="utf-8")
        (root / "reference_agents").mkdir()
        (root / "reference_agents" / "ref.py").write_text("ref", encoding="utf-8")
        (root / "RESOLUTION.md").write_text("the answer is 42", encoding="utf-8")

    def test_copies_only_allowed_subdirs(self, tmp_path):
        src = tmp_path / "plugin"
        self._make_plugin(src)
        dst = tmp_path / "docs"
        project_plugin_for_agent(src, dst)

        assert (dst / "skills" / "SKILL.md").is_file()
        assert (dst / "commands" / "x.md").is_file()
        # Grader / reference / RESOLUTION never copied.
        assert not (dst / "tests").exists()
        assert not (dst / "reference_agents").exists()
        assert not (dst / "RESOLUTION.md").exists()

    def test_empty_when_no_allowed_subdirs(self, tmp_path):
        src = tmp_path / "plugin"
        (src / "tests").mkdir(parents=True)
        (src / "tests" / "check.py").write_text("x", encoding="utf-8")
        dst = tmp_path / "docs"
        project_plugin_for_agent(src, dst)
        assert dst.is_dir()
        assert list(dst.iterdir()) == []

    def test_allowlist_membership(self):
        assert frozenset({"skills", "commands", "agents", ".claude-plugin", "hooks"}) == PLUGIN_AGENT_ALLOWED_SUBDIRS


# ---------------------------------------------------------------------------
# container_perms primitives
# ---------------------------------------------------------------------------
_LINUX_ROOT = pytest.mark.skipif(
    sys.platform != "linux" or (getattr(os, "geteuid", lambda: 1)() != 0),
    reason="requires Linux + root to observe real chown/chmod",
)


class TestContainerPerms:
    def test_lock_and_grant_noop_off_root(self, tmp_path, monkeypatch):
        # Force the barrier inactive (simulate non-root) and assert no raise + no chmod.
        monkeypatch.setattr(container_perms, "_barrier_active", lambda: False)
        p = tmp_path / "d"
        p.mkdir()
        before = p.stat().st_mode
        container_perms.lock_harness_root_0700([p])
        container_perms.grant_agent_ownership([p])
        assert p.stat().st_mode == before  # untouched

    def test_missing_paths_are_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(container_perms, "_barrier_active", lambda: True)
        # Non-existent path must not raise even with the barrier "active".
        container_perms.lock_harness_root_0700([tmp_path / "nope"])

    def test_lock_raises_loud_on_chmod_failure(self, tmp_path, monkeypatch):
        """H2 regression: when the barrier is active, a failed lock must RAISE (not
        silently no-op) — a swallowed EROFS is exactly how the answer key stayed
        agent-readable. The grant path stays best-effort (below)."""
        monkeypatch.setattr(container_perms, "_barrier_active", lambda: True)
        p = tmp_path / "harness"
        p.mkdir()
        # Force os.chown to fail like a :ro bind mount (EROFS) would.
        monkeypatch.setattr(container_perms.os, "chown", _raise_erofs)
        with pytest.raises(OSError, match="failed to lock"):
            container_perms.lock_harness_root_0700([p])

    def test_grant_stays_best_effort_on_failure(self, tmp_path, monkeypatch):
        # Grant must NOT raise on a chown failure (only a convenience is lost).
        monkeypatch.setattr(container_perms, "_barrier_active", lambda: True)
        p = tmp_path / "workspace"
        p.mkdir()
        monkeypatch.setattr(container_perms.os, "chown", _raise_erofs)
        container_perms.grant_agent_ownership([p])  # no raise

    @_LINUX_ROOT
    def test_lock_harness_root_0700_as_root(self, tmp_path):
        d = tmp_path / "harness"
        d.mkdir()
        (d / "check_x.py").write_text("x", encoding="utf-8")
        container_perms.lock_harness_root_0700([d])
        st = d.stat()
        assert st.st_uid == 0 and st.st_gid == 0
        assert (st.st_mode & 0o777) == 0o700
        assert (d / "check_x.py").stat().st_uid == 0

    @_LINUX_ROOT
    def test_grant_agent_ownership_as_root(self, tmp_path):
        d = tmp_path / "workspace"
        d.mkdir()
        (d / "f.txt").write_text("x", encoding="utf-8")
        container_perms.grant_agent_ownership([d])
        assert d.stat().st_uid == AGENT_UID
        assert (d / "f.txt").stat().st_uid == AGENT_UID


# ---------------------------------------------------------------------------
# Dockerfile / drop shim drift guards
# ---------------------------------------------------------------------------
class TestDockerfileDrift:
    def test_dockerfile_bakes_agent_uid_matching_constant(self):
        text = _DOCKERFILE.read_text(encoding="utf-8")
        m = re.search(r"useradd\s+-u\s+\$\{AGENT_UID\}", text)
        assert m, "Dockerfile must useradd -u ${AGENT_UID}"
        arg = re.search(r"ARG\s+AGENT_UID=(\d+)", text)
        assert arg, "Dockerfile must default ARG AGENT_UID"
        assert int(arg.group(1)) == AGENT_UID

    def test_dockerfile_installs_util_linux_and_no_user_directive(self):
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert "util-linux" in text
        # No `USER` directive — the container must stay root for grading.
        assert not re.search(r"(?m)^\s*USER\s+", text)

    def test_dockerfile_bakes_agent_gid_matching_constant(self):
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"groupadd\s+-g\s+\$\{AGENT_GID\}", text), "Dockerfile must groupadd -g ${AGENT_GID}"
        arg = re.search(r"ARG\s+AGENT_GID=(\d+)", text)
        assert arg, "Dockerfile must default ARG AGENT_GID"
        assert int(arg.group(1)) == AGENT_GID

    def test_dockerfile_shim_path_matches_constant(self):
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert CONTAINER_DROP_SHIM in text, f"Dockerfile must COPY the shim to {CONTAINER_DROP_SHIM}"

    def test_dockerfile_bakes_agent_home_matching_constant(self):
        """H3: the agent user must have an agent-owned HOME so the dropped CLI's HOME
        can point somewhere writable instead of root's 0700 /root."""
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert AGENT_HOME == "/home/agent"
        assert f"-d {AGENT_HOME}" in text, f"Dockerfile useradd must set -d {AGENT_HOME}"
        assert re.search(r"useradd[^\n]*\s-m\b", text), "Dockerfile useradd must create the home dir (-m)"

    def test_dockerfile_copies_drop_shim(self):
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert "coder_eval_drop_privilege.sh" in text
        assert "/usr/local/bin/coder_eval_drop_privilege.sh" in text

    def test_drop_shim_execs_setpriv_agent(self):
        text = _DROP_SHIM.read_text(encoding="utf-8")
        assert "setpriv --reuid=agent --regid=agent --clear-groups" in text
        assert "exec setpriv" in text and '"$@"' in text


# ---------------------------------------------------------------------------
# Host-side staging: stripped task.yaml + root-only full sibling + skill-DOCS
# ---------------------------------------------------------------------------
def _make_runner(task, source_yaml: str) -> DockerRunner:
    rt = MagicMock()
    rt.task = task
    rt.run_dir = Path(tempfile.gettempdir()) / "test_user_sep_run"
    rt.variant_id = None
    rt.replicate_index = 0
    rt.config_lineage = {}
    rt.source_yaml = source_yaml
    rt.task_file = None
    return DockerRunner(rt)


class TestStaging:
    def test_stripped_task_yaml_and_full_sibling(self, tmp_path):
        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        asyncio.run(runner._stage_inputs(input_dir))

        staged = yaml.safe_load((input_dir / "task.yaml").read_text(encoding="utf-8"))
        assert staged["success_criteria"] == []
        assert staged.get("reference") is None

        # The sentinel expected value must NOT appear in the agent-readable task.yaml.
        assert "LEAKED-ANSWER-SENTINEL-9f3a2b" not in (input_dir / "task.yaml").read_text(encoding="utf-8")

        # context.json.source_yaml is nulled; agent_run_uid forwarded.
        ctx = json.loads((input_dir / "context.json").read_text(encoding="utf-8"))
        assert ctx["source_yaml"] is None
        assert ctx["agent_run_uid"] == AGENT_UID

        # The root-only sibling carries the FULL criteria (for the entrypoint to merge back).
        full = json.loads((input_dir / "task_full.json").read_text(encoding="utf-8"))
        assert full["success_criteria"], "full criteria must travel to grading"
        assert "LEAKED-ANSWER-SENTINEL-9f3a2b" in json.dumps(full)

    def test_skill_docs_copy_carries_docs_only(self, tmp_path):
        # Build a plugin root with docs + graders, wire it onto a task.
        plugin_root = tmp_path / "myskill"
        (plugin_root / "skills").mkdir(parents=True)
        (plugin_root / "skills" / "SKILL.md").write_text("do the thing", encoding="utf-8")
        (plugin_root / "tests").mkdir()
        (plugin_root / "tests" / "check_x.py").write_text("EXPECTED", encoding="utf-8")
        (plugin_root / "RESOLUTION.md").write_text("answer", encoding="utf-8")

        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        # Inject a plugin pointing at the built root.
        task.agent.plugins = [{"type": "local", "path": str(plugin_root)}]

        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        asyncio.run(runner._stage_inputs(input_dir))

        docs_root = input_dir.parent / "skills" / "myskill"
        assert (docs_root / "skills" / "SKILL.md").is_file()
        assert not (docs_root / "tests").exists()
        assert not (docs_root / "RESOLUTION.md").exists()

        # The staged task.yaml rewrote the plugin path to the in-container mount.
        staged = yaml.safe_load((input_dir / "task.yaml").read_text(encoding="utf-8"))
        assert staged["agent"]["plugins"][0]["path"] == f"{CONTAINER_SKILL_DOCS_DIR}/myskill"

    def test_same_basename_plugins_disambiguated(self, tmp_path):
        a = tmp_path / "a" / "myskill"
        b = tmp_path / "b" / "myskill"
        for root, doc in ((a, "A doc"), (b, "B doc")):
            (root / "skills").mkdir(parents=True)
            (root / "skills" / "SKILL.md").write_text(doc, encoding="utf-8")

        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        task.agent.plugins = [{"type": "local", "path": str(a)}, {"type": "local", "path": str(b)}]
        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        asyncio.run(runner._stage_inputs(input_dir))

        staged = yaml.safe_load((input_dir / "task.yaml").read_text(encoding="utf-8"))
        paths = [p["path"] for p in staged["agent"]["plugins"]]
        # Two distinct in-container paths (no collapse), both under the skill-docs mount.
        assert len(set(paths)) == 2
        assert all(p.startswith(f"{CONTAINER_SKILL_DOCS_DIR}/myskill") for p in paths)
        # Both docs copies exist and carry the right content.
        skills_root = input_dir.parent / "skills"
        docs = sorted(d.name for d in skills_root.iterdir())
        assert docs == ["myskill", "myskill-1"]

    def test_no_plugins_no_skill_docs(self, tmp_path):
        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        task.agent.plugins = None
        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        asyncio.run(runner._stage_inputs(input_dir))
        assert runner._skill_docs_src is None

    def test_plugin_host_paths_forwarded_to_context(self, tmp_path):
        """C1 regression: the ORIGINAL (resolved) host plugin mount paths must ride on
        context.json so the entrypoint can lock the raw grader-bearing mount — the
        staged task.yaml's plugin path is rewritten to /work/skills and can't."""
        plugin_root = (tmp_path / "skills_repo").resolve()
        (plugin_root / "skills").mkdir(parents=True)
        (plugin_root / "skills" / "SKILL.md").write_text("doc", encoding="utf-8")
        (plugin_root / "tests").mkdir()
        (plugin_root / "tests" / "check_x.py").write_text("EXPECTED", encoding="utf-8")

        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        task.agent.plugins = [{"type": "local", "path": str(plugin_root)}]
        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        asyncio.run(runner._stage_inputs(input_dir))

        ctx = json.loads((input_dir / "context.json").read_text(encoding="utf-8"))
        # The RESOLVED raw host path (== the in-container mount path), NOT /work/skills.
        assert ctx["plugin_host_paths"] == [str(plugin_root)]
        assert not any(p.startswith(CONTAINER_SKILL_DOCS_DIR) for p in ctx["plugin_host_paths"])
        # And the staged task.yaml's plugin path IS rewritten to /work/skills (proving
        # the barrier can't recover the raw mount from the task alone).
        staged = yaml.safe_load((input_dir / "task.yaml").read_text(encoding="utf-8"))
        assert staged["agent"]["plugins"][0]["path"].startswith(CONTAINER_SKILL_DOCS_DIR)

    def test_no_plugins_empty_host_paths(self, tmp_path):
        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        task.agent.plugins = None
        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        asyncio.run(runner._stage_inputs(input_dir))
        ctx = json.loads((input_dir / "context.json").read_text(encoding="utf-8"))
        assert ctx["plugin_host_paths"] == []

    def test_absolute_reference_forwarded_and_mounted_rw(self, tmp_path):
        """C2 (reference surface): an absolute reference.file is grading material bind-
        mounted rw and its resolved mount target rides on context.json so the entrypoint
        can lock it root-0700 (else the dropped agent could `cat` the answer off disk)."""
        from coder_eval.models import ReferenceSource

        ref_dir = (tmp_path / "solution").resolve()
        ref_dir.mkdir()
        ref_file = ref_dir / "answer.py"
        ref_file.write_text("SECRET_REFERENCE_SOLUTION = 42", encoding="utf-8")

        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        task.reference = ReferenceSource(file=str(ref_file))
        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        asyncio.run(runner._stage_inputs(input_dir))

        # The reference mount PARENT is forwarded (a file mounts its parent dir).
        ctx = json.loads((input_dir / "context.json").read_text(encoding="utf-8"))
        assert str(ref_dir) in ctx["reference_host_paths"]
        # And the reference is stripped from the agent-readable task.yaml.
        assert "SECRET_REFERENCE_SOLUTION" not in (input_dir / "task.yaml").read_text(encoding="utf-8")

        # _build_argv mounts the reference dir read-WRITE (so the 0700 lock isn't EROFS'd).
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        argv = runner._build_argv(input_dir, output_dir, container_name="c")
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v" and i + 1 < len(argv)]
        ref_spec = next((m for m in mounts if m.startswith(f"{ref_dir}:{ref_dir}")), None)
        assert ref_spec is not None and not ref_spec.endswith(":ro"), ref_spec

    def test_relative_reference_not_forwarded(self, tmp_path):
        """A relative reference under task_dir is covered by the locked task_dir mount;
        it must NOT be separately forwarded (nothing to auto-mount)."""
        from coder_eval.models import ReferenceSource

        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        task.reference = ReferenceSource(file="solution/answer.py")  # relative, unresolved
        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        asyncio.run(runner._stage_inputs(input_dir))
        ctx = json.loads((input_dir / "context.json").read_text(encoding="utf-8"))
        # Relative path doesn't resolve to an existing dir here => not forwarded.
        assert ctx["reference_host_paths"] == []


class TestPluginPathHelper:
    """H5: one shared accessor for a plugin entry's path, dict- AND model-shaped."""

    def test_dict_plugin(self):
        assert plugin_path({"type": "local", "path": "/a/b"}) == "/a/b"

    def test_model_like_plugin(self):
        class _P:
            path = "/x/y"

        assert plugin_path(_P()) == "/x/y"

    def test_missing_or_empty_path_is_none(self):
        assert plugin_path({"type": "local"}) is None
        assert plugin_path({"type": "local", "path": ""}) is None
        assert plugin_path(object()) is None
        assert plugin_path({"type": "local", "path": 123}) is None


class TestArgv:
    def _runner_with_skill_docs(self, tmp_path):
        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        runner = _make_runner(task, source_yaml)
        runner._skill_docs_src = tmp_path / "skills"
        (tmp_path / "skills").mkdir()
        return runner

    def test_argv_adds_skill_docs_mount_and_no_user_flag(self, tmp_path):
        runner = self._runner_with_skill_docs(tmp_path)
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        argv = runner._build_argv(input_dir, output_dir, container_name="c")

        joined = " ".join(argv)
        assert f"{(tmp_path / 'skills').resolve()}:{CONTAINER_SKILL_DOCS_DIR}:ro" in argv
        # No container-level --user flag anywhere.
        assert "--user" not in argv
        assert "--user" not in joined

    def test_argv_omits_skill_docs_mount_when_unset(self, tmp_path):
        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        argv = runner._build_argv(input_dir, output_dir, container_name="c")
        assert CONTAINER_SKILL_DOCS_DIR not in " ".join(argv)
        assert "--user" not in argv

    def test_locked_mounts_are_read_write_not_ro(self, tmp_path):
        """C2 regression: /work/input, the task-dir mount, and the raw plugin mount are
        bind-mounted READ-WRITE (no :ro) so the in-container root-0700 chmod lock
        applies (chmod EROFS-fails silently on a :ro bind mount)."""
        plugin_root = (tmp_path / "skills_repo").resolve()
        (plugin_root / "skills").mkdir(parents=True)
        (plugin_root / "skills" / "SKILL.md").write_text("doc", encoding="utf-8")
        (plugin_root / "tests").mkdir()
        (plugin_root / "tests" / "check_x.py").write_text("EXPECTED", encoding="utf-8")

        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        task.agent.plugins = [{"type": "local", "path": str(plugin_root)}]
        task_dir = (tmp_path / "task_dir").resolve()
        task_dir.mkdir()
        rt = MagicMock()
        rt.task = task
        rt.run_dir = tmp_path / "run"
        rt.variant_id = None
        rt.replicate_index = 0
        rt.config_lineage = {}
        rt.source_yaml = source_yaml
        rt.task_file = task_dir / "task.yaml"
        runner = DockerRunner(rt)

        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        argv = runner._build_argv(input_dir, output_dir, container_name="c")

        # -v entries are the args after each "-v".
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v" and i + 1 < len(argv)]

        def _spec(host_prefix: str) -> str | None:
            return next((m for m in mounts if m.startswith(host_prefix)), None)

        # /work/input mount: rw (no :ro suffix).
        input_spec = next(m for m in mounts if f":{CONTAINER_INPUT_DIR}" in m)
        assert not input_spec.endswith(":ro"), input_spec
        # task-dir mount: rw.
        td_spec = _spec(f"{task_dir}:{task_dir}")
        assert td_spec is not None and not td_spec.endswith(":ro"), td_spec
        # raw plugin/skills-repo mount: rw.
        plug_spec = _spec(f"{plugin_root}:{plugin_root}")
        assert plug_spec is not None and not plug_spec.endswith(":ro"), plug_spec


# ---------------------------------------------------------------------------
# Per-harness spawn-seam wiring (mocked SDKs; no real CLI, no model)
# ---------------------------------------------------------------------------
class TestCodexWiring:
    def _agent(self, *, agent_run_uid: int | None):
        from coder_eval.agents.codex_agent import CodexAgent
        from coder_eval.models import parse_agent_config

        cfg = parse_agent_config(type="codex")
        cfg.agent_run_uid = agent_run_uid
        return CodexAgent(cfg)

    def test_launch_args_override_routes_through_shim(self, monkeypatch):
        from coder_eval.models import CONTAINER_DROP_SHIM

        fake_bin = Path("/opt/codex/bin/codex")
        monkeypatch.setitem(
            sys.modules, "codex_cli_bin", type("M", (), {"bundled_codex_path": staticmethod(lambda: fake_bin)})
        )
        agent = self._agent(agent_run_uid=2000)
        args = agent._drop_privilege_launch_args()
        assert args == (CONTAINER_DROP_SHIM, str(fake_bin), "app-server", "--listen", "stdio://")
        # shim is first, bundled codex second.
        assert args[0] == CONTAINER_DROP_SHIM
        assert args[1] == str(fake_bin)

    def test_no_run_uid_no_override(self):
        agent = self._agent(agent_run_uid=None)
        assert agent._drop_privilege_launch_args() is None

    def test_home_and_codex_home_relocated_under_drop_without_mocks(self, monkeypatch, tmp_path):
        # H3 (codex, mock-free case): under the drop with NO mock-PATH override,
        # _setup_login_shell_home no-ops (_login_shell_home stays None). HOME must be
        # relocated to the agent-owned AGENT_HOME (not left as root's 0700 /root) and
        # CODEX_HOME must resolve under it (not the unreachable /root/.codex).
        from coder_eval.isolation import container_perms

        # Point AGENT_HOME at a writable tmp dir (the real /home/agent is baked in the
        # image, absent on a dev host); _build_codex_env mkdir's CODEX_HOME under it.
        # _build_codex_env does `from coder_eval.models import AGENT_HOME`, so patch it there.
        fake_home = tmp_path / "home_agent"
        fake_home.mkdir()
        monkeypatch.setattr("coder_eval.models.AGENT_HOME", str(fake_home))
        monkeypatch.setattr(container_perms, "grant_agent_ownership", lambda paths, **k: None)
        monkeypatch.setenv("HOME", "/root")
        monkeypatch.delenv("CODEX_HOME", raising=False)
        agent = self._agent(agent_run_uid=2000)
        assert agent._login_shell_home is None  # no mocks configured
        env = agent._build_codex_env()
        assert env is not None
        assert env["HOME"] == str(fake_home)
        assert env["CODEX_HOME"] == str(fake_home / ".codex")
        # Parent-side rollout recovery (_codex_home reads os.environ) must agree.
        assert str(agent._codex_home()) == str(fake_home / ".codex")

    def test_home_untouched_off_drop_without_mocks(self, monkeypatch):
        monkeypatch.setenv("HOME", "/root")
        agent = self._agent(agent_run_uid=None)
        env = agent._build_codex_env()
        # No drop, no mocks => no HOME relocation (env may be None or lack HOME).
        assert env is None or "HOME" not in env

    def test_login_home_chowned_to_agent_when_dropped(self, monkeypatch, tmp_path):
        # When the drop is active, the root-owned login-shell HOME must be granted to
        # the agent uid (else the dropped app-server EACCESes on its own profile).
        from coder_eval.isolation import container_perms

        granted: list = []
        monkeypatch.setattr(container_perms, "grant_agent_ownership", lambda paths, **kw: granted.extend(paths))
        # Stub the SDK + login-home setup so start() reaches the chown branch cheaply.
        home = tmp_path / "login_home"
        home.mkdir()
        agent = self._agent(agent_run_uid=2000)
        monkeypatch.setattr(agent, "_setup_login_shell_home", lambda: setattr(agent, "_login_shell_home", home))
        monkeypatch.setattr(agent, "_close_client", lambda: None)
        monkeypatch.setattr(agent, "_setup_skills", lambda *a, **k: None)

        class _FakeCodex:
            def __init__(self, *a, **k):
                pass

        monkeypatch.setitem(
            sys.modules, "openai_codex", type("M", (), {"Codex": _FakeCodex, "CodexConfig": lambda **k: None})
        )
        monkeypatch.setattr("coder_eval.agents.codex_agent.bundled_codex_path", lambda: Path("/x"), raising=False)
        # We only assert the chown fired before any downstream failure.
        with contextlib.suppress(Exception):
            asyncio.run(agent.start(str(tmp_path / "wd")))
        assert home in granted


class TestAntigravityWiring:
    def _agent(self, *, agent_run_uid: int | None):
        from coder_eval.agents.antigravity_agent import AntigravityAgent
        from coder_eval.models import parse_agent_config

        cfg = parse_agent_config(type="antigravity")
        cfg.agent_run_uid = agent_run_uid
        return AntigravityAgent(cfg)

    def test_stages_localharness_wrapper(self, monkeypatch, tmp_path):
        from coder_eval.models import CONTAINER_DROP_SHIM

        real = tmp_path / "bin" / "localharness"
        real.parent.mkdir(parents=True)
        real.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr("shutil.which", lambda name: str(real) if name == "localharness" else None)

        agent = self._agent(agent_run_uid=2000)
        shim_dir = agent._stage_localharness_drop_shim()
        assert shim_dir is not None
        wrapper = shim_dir / "localharness"
        assert wrapper.is_file()
        assert os.access(wrapper, os.X_OK)
        text = wrapper.read_text(encoding="utf-8")
        assert CONTAINER_DROP_SHIM in text
        assert str(real) in text  # execs the REAL localharness by absolute path
        import shutil as _sh

        _sh.rmtree(shim_dir, ignore_errors=True)

    def test_missing_localharness_returns_none(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        agent = self._agent(agent_run_uid=2000)
        assert agent._stage_localharness_drop_shim() is None

    def test_harness_spawn_guard_relocates_home_under_drop(self, monkeypatch):
        """H3 (antigravity): under the drop, the guarded spawn window must relocate HOME
        to the agent-owned AGENT_HOME (the setpriv shim doesn't set HOME), then restore."""
        agent = self._agent(agent_run_uid=2000)
        monkeypatch.setenv("HOME", "/root")
        observed: dict[str, str | None] = {}

        async def _drive():
            async with agent._harness_spawn_guard():
                observed["home"] = os.environ.get("HOME")

        asyncio.run(_drive())
        assert observed["home"] == AGENT_HOME
        # Restored after the window.
        assert os.environ.get("HOME") == "/root"

    def test_harness_spawn_guard_leaves_home_when_not_dropped(self, monkeypatch):
        agent = self._agent(agent_run_uid=None)
        monkeypatch.setenv("HOME", "/root")
        observed: dict[str, str | None] = {}

        async def _drive():
            async with agent._harness_spawn_guard():
                observed["home"] = os.environ.get("HOME")

        asyncio.run(_drive())
        assert observed["home"] == "/root"


class TestClaudeHomeRelocation:
    """H3 (claude): Popen(user='agent') drops the uid but not HOME; the dropped CLI
    would EACCES on ~/.claude under root's 0700 home. _relocate_home_for_drop points
    HOME at the agent-owned AGENT_HOME and stages/grants ~/.claude there."""

    def _agent(self, *, agent_run_uid: int | None):
        from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
        from coder_eval.models import parse_agent_config

        cfg = parse_agent_config(type="claude-code")
        cfg.agent_run_uid = agent_run_uid
        return ClaudeCodeAgent(cfg)

    def test_home_relocated_and_claude_staged_under_drop(self, tmp_path, monkeypatch):
        from coder_eval.isolation import container_perms

        # Fake host HOME with a ~/.claude to relocate.
        host_home = tmp_path / "root"
        (host_home / ".claude").mkdir(parents=True)
        (host_home / ".claude" / "creds.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: host_home))
        agent_home = tmp_path / "home_agent"
        monkeypatch.setattr("coder_eval.agents.claude_code_agent.AGENT_HOME", str(agent_home))

        granted: list = []
        monkeypatch.setattr(container_perms, "grant_agent_ownership", lambda paths, **k: granted.extend(paths))

        agent = self._agent(agent_run_uid=2000)
        env: dict[str, str] = {}
        agent._relocate_home_for_drop(env)

        assert env["HOME"] == str(agent_home)
        # ~/.claude was staged under the new HOME and the HOME was granted to the agent.
        assert (agent_home / ".claude" / "creds.json").is_file()
        assert agent_home in granted

    def test_home_untouched_off_drop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        agent = self._agent(agent_run_uid=None)
        env: dict[str, str] = {}
        agent._relocate_home_for_drop(env)
        assert "HOME" not in env


# ---------------------------------------------------------------------------
# Entrypoint: merge-back + fail-loud
# ---------------------------------------------------------------------------
class TestEntrypointBarrier:
    def test_merge_full_task_restores_criteria(self, tmp_path):
        """Regression: the barrier stages a criteria-STRIPPED ``task.yaml``
        (``success_criteria: []``) plus a root-only ``task_full.json``.
        ``_merge_full_task`` must restore the real criteria from the sibling BEFORE
        parsing -- the stripped yaml cannot pass ``TaskDefinition`` validation on its
        own, so the earlier "parse then merge" ordering crashed every barrier run."""
        import yaml as _yaml

        from coder_eval.cli.run_task_internal_command import _merge_full_task

        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        # stage exactly as the host does: a genuinely stripped task.yaml on disk ...
        stripped = task.agent_safe_dump()
        assert stripped["success_criteria"] == [], "precondition: staged task.yaml is stripped"
        (input_dir / "task.yaml").write_text(_yaml.safe_dump(stripped, sort_keys=False), encoding="utf-8")
        # ... plus the root-only sibling carrying the real criteria.
        full = {
            "success_criteria": task.model_dump(mode="json")["success_criteria"],
            "reference": None,
            "source_yaml": "raw: yaml",
        }
        (input_dir / "task_full.json").write_text(json.dumps(full), encoding="utf-8")

        merged, raw_yaml = _merge_full_task(input_dir / "task.yaml", input_dir)
        assert len(merged.success_criteria) == len(task.success_criteria) > 0
        assert raw_yaml == "raw: yaml"

    def test_merge_full_task_missing_sibling_falls_back(self, tmp_path):
        """Defensive: with no ``task_full.json`` the merge parses the staged yaml
        as-is (best effort) and returns ``source_yaml=None``. In the barrier path the
        host always writes the sibling, so this is the never-hit safety net."""
        import yaml as _yaml

        from coder_eval.cli.run_task_internal_command import _merge_full_task

        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        # a NON-stripped yaml so the raw-parse fallback still yields a valid task.
        (input_dir / "task.yaml").write_text(
            _yaml.safe_dump(task.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
        )
        merged, raw_yaml = _merge_full_task(input_dir / "task.yaml", input_dir)
        assert len(merged.success_criteria) == len(task.success_criteria) > 0
        assert raw_yaml is None

    def test_barrier_fails_loud_when_not_root(self, tmp_path, monkeypatch):
        import typer

        from coder_eval.cli.run_task_internal_command import _apply_isolation_barrier

        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        # Force non-root euid.
        monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
        with pytest.raises(typer.Exit):
            _apply_isolation_barrier(
                agent_run_uid=2000,
                task=task,
                input_dir=tmp_path / "input",
                output_dir=tmp_path / "output",
                task_dir=tmp_path / "task_dir",
                workspace_dir=None,
            )

    def test_barrier_locks_forwarded_plugin_host_paths(self, tmp_path, monkeypatch):
        """C1 regression: the barrier locks the raw plugin host paths forwarded via
        context.json (their in-container path == the host path), NOT the /work/skills
        rewritten path in the staged task."""
        from coder_eval.cli.run_task_internal_command import _apply_isolation_barrier

        raw_plugin = (tmp_path / "skills_repo").resolve()
        raw_plugin.mkdir()
        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        # Staged task points at /work/skills (the rewritten copy) — the barrier must
        # NOT rely on it, and must lock the forwarded raw path instead.
        task.agent.plugins = [{"type": "local", "path": f"{CONTAINER_SKILL_DOCS_DIR}/skills_repo"}]
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

        locked: list = []
        from coder_eval.isolation import container_perms

        monkeypatch.setattr(container_perms, "lock_harness_root_0700", lambda paths: locked.extend(paths))
        monkeypatch.setattr(container_perms, "grant_agent_ownership", lambda paths, **k: None)

        input_dir = tmp_path / "input"
        _apply_isolation_barrier(
            agent_run_uid=2000,
            task=task,
            input_dir=input_dir,
            output_dir=tmp_path / "output",
            task_dir=tmp_path / "task_dir",
            workspace_dir=tmp_path / "ws",
            plugin_host_paths=[str(raw_plugin)],
        )
        assert raw_plugin in locked, "raw plugin host path must be locked"
        # The /work/skills rewritten path must NOT be locked (agent-legitimate copy).
        assert Path(f"{CONTAINER_SKILL_DOCS_DIR}/skills_repo") not in locked

    def test_barrier_locks_forwarded_reference_host_paths(self, tmp_path, monkeypatch):
        """C2 (reference surface): the barrier must lock the forwarded reference mount
        targets root-0700 so the dropped agent uid can't read the reference solution."""
        from coder_eval.cli.run_task_internal_command import _apply_isolation_barrier
        from coder_eval.isolation import container_perms

        ref_dir = (tmp_path / "solution").resolve()
        ref_dir.mkdir()
        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        task.agent.plugins = None
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

        locked: list = []
        monkeypatch.setattr(container_perms, "lock_harness_root_0700", lambda paths: locked.extend(paths))
        monkeypatch.setattr(container_perms, "grant_agent_ownership", lambda paths, **k: None)

        _apply_isolation_barrier(
            agent_run_uid=2000,
            task=task,
            input_dir=tmp_path / "input",
            output_dir=tmp_path / "output",
            task_dir=tmp_path / "task_dir",
            workspace_dir=tmp_path / "ws",
            plugin_host_paths=[],
            reference_host_paths=[str(ref_dir)],
        )
        assert ref_dir in locked, "reference solution mount must be locked root-0700"

    def test_barrier_raises_on_unparseable_plugin_path(self, tmp_path, monkeypatch):
        """H5 regression: a plugin entry with no parseable path, while the barrier is
        active, is a HARD ERROR (a silently-skipped lock is the C1 bug class)."""
        import typer

        from coder_eval.cli.run_task_internal_command import _apply_isolation_barrier
        from coder_eval.isolation import container_perms

        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        # Inject a pathless plugin entry WITHOUT triggering validate_assignment (the
        # model enforces `path`; this simulates a future refactor / bypass where
        # plugin_path() cannot recover a path). Mutating the list in place skips
        # re-validation.
        task.agent.plugins = [{"type": "local", "path": "/tmp/x"}]
        task.agent.plugins[0].pop("path")
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(container_perms, "lock_harness_root_0700", lambda paths: None)
        monkeypatch.setattr(container_perms, "grant_agent_ownership", lambda paths, **k: None)
        with pytest.raises(typer.Exit):
            _apply_isolation_barrier(
                agent_run_uid=2000,
                task=task,
                input_dir=tmp_path / "input",
                output_dir=tmp_path / "output",
                task_dir=tmp_path / "task_dir",
                workspace_dir=tmp_path / "ws",
                plugin_host_paths=[],
            )


class TestAgentRunUidAuthoringRejected:
    """M1: agent_run_uid is framework-set only; YAML / -D authoring is refused."""

    def _raw(self, uid) -> dict:
        # A round-trip dump of the fixture task, with an authored agent_run_uid.
        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        raw = task.model_dump(mode="json")
        raw["agent"]["agent_run_uid"] = uid
        return raw

    def test_yaml_authored_agent_run_uid_rejected(self, tmp_path):
        from coder_eval.orchestration.task_loader import parse_task_dict

        with pytest.raises(ValueError, match="agent_run_uid is framework-set only"):
            parse_task_dict(self._raw(2000), tmp_path)

    def test_none_agent_run_uid_allowed(self, tmp_path):
        # None (the model_dump round-trip default) must still parse — the container
        # re-parses the staged, stripped task.yaml which carries agent_run_uid: null.
        from coder_eval.orchestration.task_loader import parse_task_dict

        task = parse_task_dict(self._raw(None), tmp_path)
        assert task.agent.agent_run_uid is None

    def test_cli_override_agent_run_uid_rejected(self):
        from coder_eval.orchestration.overrides import OverrideError, apply_overrides

        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        with pytest.raises(OverrideError, match="framework-set only"):
            apply_overrides(task, {"agent.agent_run_uid": 2000})

    def test_variant_merge_agent_run_uid_rejected(self):
        # Third authoring path: the experiment variant / experiment-defaults merge
        # resolves the agent root via resolve_root('agent') -> parse_agent_config,
        # NOT through parse_task_dict/apply_overrides. A variant that sets
        # agent_run_uid must be rejected at that choke point too.
        from coder_eval.orchestration.config_merge import Layer, resolve_root

        with pytest.raises(ValueError, match="framework-set only"):
            resolve_root("agent", [Layer(source="variant", patch={"type": "claude-code", "agent_run_uid": 2000})])

    def test_parse_agent_config_rejects_authored_uid(self):
        # The single construction choke point: every authoring path funnels through
        # parse_agent_config, which rejects a non-None agent_run_uid kwarg.
        from coder_eval.models import parse_agent_config

        with pytest.raises(ValueError, match="framework-set only"):
            parse_agent_config(type="claude-code", agent_run_uid=2000)
        # None (the round-trip default) is allowed; the framework sets it by direct write.
        cfg = parse_agent_config(type="claude-code", agent_run_uid=None)
        cfg.agent_run_uid = AGENT_UID
        assert cfg.agent_run_uid == AGENT_UID

    def test_framework_direct_assignment_still_allowed(self):
        # The framework sets it by direct attribute write on the resolved config —
        # that path must remain open (the barrier depends on it).
        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        task.agent.agent_run_uid = AGENT_UID
        assert task.agent.agent_run_uid == AGENT_UID

    def test_dropped_config_does_not_persist_and_round_trips(self):
        # Regression (CI-caught, E2E docker): under the barrier the container sets
        # agent_run_uid by direct write, serializes the result to task.json, and the
        # host re-parses it. agent_run_uid is runtime-only (Field exclude=True) so it
        # must NOT persist into the dump, and the host read-back (model_validate ->
        # parse_agent_config) must NOT trip the framework-set-only authoring guard.
        from coder_eval.models import TaskDefinition

        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        task.agent.agent_run_uid = AGENT_UID
        dumped = task.model_dump(mode="json")
        assert "agent_run_uid" not in dumped["agent"], "runtime-only uid must not persist to the dump"
        TaskDefinition.model_validate(dumped)  # host read-back must not raise


class TestWorkspaceAutoFallback:
    """H3: `working_dir: auto` must NOT fall back to /root under the drop (0700 root
    home → EACCES on every agent write); it falls back to the agent-owned AGENT_HOME."""

    def test_auto_falls_back_to_agent_home_on_inspect_failure(self, monkeypatch):
        import subprocess

        from coder_eval.isolation.docker_runner import _resolve_workspace_dir

        def _boom(*a, **k):
            raise subprocess.CalledProcessError(1, "docker")

        monkeypatch.setattr(subprocess, "run", _boom)
        assert _resolve_workspace_dir("auto", "img") == AGENT_HOME

    def test_auto_honours_declared_image_workdir(self, monkeypatch):
        import subprocess

        from coder_eval.isolation.docker_runner import _resolve_workspace_dir

        class _R:
            stdout = "/app\n"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
        assert _resolve_workspace_dir("auto", "img") == "/app"

    def test_none_stays_none(self):
        from coder_eval.isolation.docker_runner import _resolve_workspace_dir

        assert _resolve_workspace_dir(None, "img") is None


class TestTempdirDriverUnchanged:
    def test_tempdir_driver_criteria_in_memory(self):
        # Under driver: tempdir there is no container, no uid barrier, and criteria
        # live only in memory — no agent-readable task.yaml is staged. Assert the
        # loaded task carries its full criteria and no isolation staging runs.
        task, _ = load_task(_FIX / "adversarial_criteria_probe.yaml")
        assert task.sandbox.driver == "tempdir"
        assert task.success_criteria, "tempdir task keeps its criteria in-memory"
        # agent_run_uid is None (no drop) unless a docker entrypoint sets it.
        assert task.agent.agent_run_uid is None


# ---------------------------------------------------------------------------
# Six-surface EACCES-as-agent-uid proof (root + Linux; run via make test-docker-isolation)
# ---------------------------------------------------------------------------
def _read_as_agent_uid(path: Path) -> str:
    """Fork a child dropped to AGENT_UID, attempt to read ``path``, and return one of
    'EACCES' | 'OK' | 'MISSING' via the exit code. Parent stays root."""
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.setgid(AGENT_GID)
            os.setuid(AGENT_UID)
            try:
                path.read_bytes()
                os._exit(0)  # OK — readable (a leak, unless a positive control)
            except PermissionError:
                os._exit(13)  # EACCES
            except FileNotFoundError:
                os._exit(2)  # MISSING
            except OSError:
                os._exit(13)
        except Exception:
            os._exit(99)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return {0: "OK", 13: "EACCES", 2: "MISSING"}.get(code, f"ERR{code}")


@pytest.mark.docker_root
class TestSixSurfaceEacces:
    def test_agent_uid_gets_eacces_on_every_surface(self, tmp_path):
        assert sys.platform == "linux" and os.geteuid() == 0, "root-in-container only"

        # Stage a task + a plugin with graders + a full-criteria sibling.
        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        plugin_root = tmp_path / "skills_repo"
        (plugin_root / "skills").mkdir(parents=True)
        (plugin_root / "skills" / "SKILL.md").write_text("docs", encoding="utf-8")
        (plugin_root / "tests").mkdir()
        (plugin_root / "tests" / "check_x.py").write_text("EXPECTED", encoding="utf-8")
        task.agent.plugins = [{"type": "local", "path": str(plugin_root)}]

        # Absolute reference solution (grading material) — bind-mounted for the grader,
        # must be locked so the agent can't read the answer.
        from coder_eval.models import ReferenceSource

        ref_dir = (tmp_path / "solution").resolve()
        ref_dir.mkdir()
        (ref_dir / "answer.py").write_text("SECRET_REFERENCE = 42", encoding="utf-8")
        task.reference = ReferenceSource(file=str(ref_dir / "answer.py"))

        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        asyncio.run(runner._stage_inputs(input_dir))
        skill_docs = input_dir.parent / "skills"

        output_dir = tmp_path / "output"
        (output_dir / "artifacts" / task.task_id).mkdir(parents=True)
        (output_dir / "task.json").write_text('{"criteria": "secret"}', encoding="utf-8")

        # In production /work/<...> ancestors are world-traversable mounts; pytest's
        # mkdtemp ancestors (/tmp/pytest-of-root/pytest-N/...) are 0700-root, which
        # would deny the agent uid even the positive-control read. Make the whole
        # temp ancestor chain traversable so this test exercises the LEAF permissions
        # the barrier sets, not mkdtemp's default.
        anc = tmp_path
        while anc != anc.parent and str(anc).startswith("/tmp"):
            os.chmod(anc, 0o711)  # traverse-only (o+x); NOT world-readable
            anc = anc.parent
        for extra in (output_dir, output_dir / "artifacts"):
            os.chmod(extra, 0o711)  # traverse-only (o+x); NOT world-readable

        # Apply the barrier as root.
        from coder_eval.cli.run_task_internal_command import _apply_isolation_barrier

        _apply_isolation_barrier(
            agent_run_uid=AGENT_UID,
            task=task,
            input_dir=input_dir,
            output_dir=output_dir,
            task_dir=plugin_root,
            workspace_dir=output_dir / "artifacts" / task.task_id,
            # C1: the raw plugin mount is locked via the forwarded host path (the
            # staged task's plugin path is /work/skills and cannot be locked).
            plugin_host_paths=[str(plugin_root.resolve())],
            reference_host_paths=runner._reference_host_paths,
        )
        # _apply_isolation_barrier grants /work/skills (the production mount); here the
        # skill-DOCS copy lives at a temp path, so grant it explicitly for the control.
        container_perms.grant_agent_ownership([skill_docs])

        # #1 staged criteria: task.yaml is stripped (readable but clean) but the full
        #    sibling + the whole input dir is root-0700 => EACCES.
        assert _read_as_agent_uid(input_dir / "task_full.json") == "EACCES"
        assert _read_as_agent_uid(input_dir / "task.yaml") == "EACCES"
        # #2 skills-repo grader tree => EACCES; skill-DOCS copy => OK (positive control).
        assert _read_as_agent_uid(plugin_root / "tests" / "check_x.py") == "EACCES"
        assert _read_as_agent_uid(skill_docs / plugin_root.name / "skills" / "SKILL.md") == "OK"
        # #3 per-task-dir mount => EACCES.
        assert _read_as_agent_uid(plugin_root / "skills" / "SKILL.md") == "EACCES"
        # #4 The agent's own artifacts subdir is agent-owned (writable). /work/output
        #    itself stays a host-shared bind mount (NOT locked — the host writes the
        #    heartbeat there); task.json is written only AFTER the agent turn, so it is
        #    not a live read surface during the turn. A root-0700 file placed in output
        #    IS EACCES to the agent (mechanism check):
        artifact = output_dir / "artifacts" / task.task_id
        assert artifact.stat().st_uid == AGENT_UID  # agent can write here
        secret = output_dir / "secret_grader_output.json"
        secret.write_text("criteria", encoding="utf-8")
        container_perms.lock_harness_root_0700([secret])
        assert _read_as_agent_uid(secret) == "EACCES"

        # #5 /proc/1/environ (root PID1) => EACCES for the agent uid.
        proc_environ = Path("/proc/1/environ")
        if proc_environ.exists():
            assert _read_as_agent_uid(proc_environ) == "EACCES"

        # #6 reference solution (absolute reference.file) => EACCES. The reference mount
        #    target rides on reference_host_paths and is locked root-0700.
        assert _read_as_agent_uid(ref_dir / "answer.py") == "EACCES"


# ---------------------------------------------------------------------------
# H1: real dropped-CLI acceptance (root + Linux; via make test-docker-isolation)
#
# Exercises the REAL drop mechanism inside the built image: the real setpriv shim
# (CONTAINER_DROP_SHIM baked into the image) drops a subprocess to the agent uid,
# then that dropped subprocess proves (a) its uid is 2000, (b) it can read+write its
# own agent-owned HOME, and (c) a root-0700-locked grader file is EACCES to it.
#
# LINUX-AUTHORITATIVE: the bind-mount EACCES assertions only truly hold on Linux
# (native overlayfs). On macOS Docker Desktop the uid-remap defeats bind-mount
# chmod, so this class is gated to Linux+root and skipped elsewhere.
# ---------------------------------------------------------------------------
@pytest.mark.docker_root
class TestRealDroppedCliAcceptance:
    def _run_dropped(self, script: str, *, cwd: Path | None = None, env: dict | None = None):
        """Run ``bash -c script`` through the REAL drop shim (setpriv) baked in the
        image, returning the CompletedProcess. Uses the actual CONTAINER_DROP_SHIM so
        this covers the production drop path, not a hand-rolled os.setuid."""
        import subprocess

        argv = [CONTAINER_DROP_SHIM, "bash", "-c", script]
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            env=env,
            check=False,
        )

    def test_dropped_cli_runs_as_agent_uid_and_home_writable(self, tmp_path):
        assert sys.platform == "linux" and os.geteuid() == 0, "root-in-container only"
        assert Path(CONTAINER_DROP_SHIM).exists(), "drop shim must be baked into the image"

        # (a) the dropped subprocess reports uid 2000 (write it to a workspace file, as
        # the acceptance contract requires).
        ws = tmp_path / "ws"
        ws.mkdir()
        # pytest's mkdtemp ancestors (/tmp/pytest-of-root/...) are 0700-root, which
        # would deny the agent uid even traversal to the workspace; production /work
        # mounts are world-traversable. Make the temp ancestor chain traversable so we
        # exercise the LEAF (agent-owned ws) perms, not mkdtemp's default.
        anc = tmp_path
        while anc != anc.parent and str(anc).startswith("/tmp"):
            os.chmod(anc, 0o711)  # traverse-only (o+x); NOT world-readable
            anc = anc.parent
        container_perms.grant_agent_ownership([ws])
        uid_file = ws / "uid.txt"
        res = self._run_dropped(f"id -u > {uid_file}", cwd=ws)
        assert res.returncode == 0, res.stderr
        assert uid_file.read_text(encoding="utf-8").strip() == str(AGENT_UID)

        # (b) the dropped CLI can read+write its own agent-owned HOME (~/.claude).
        agent_home = Path(AGENT_HOME)
        if agent_home.exists():  # baked by the Dockerfile; guard for a partial image
            env = {**os.environ, "HOME": str(agent_home)}
            res2 = self._run_dropped('mkdir -p "$HOME/.claude" && echo ok > "$HOME/.claude/probe"', env=env)
            assert res2.returncode == 0, res2.stderr
            assert (agent_home / ".claude" / "probe").read_text(encoding="utf-8").strip() == "ok"

    def test_dropped_cli_gets_eacces_on_locked_grader_and_task_full(self, tmp_path):
        assert sys.platform == "linux" and os.geteuid() == 0, "root-in-container only"

        # Stage a real plugin-bearing task + full sibling, then apply the real barrier.
        task, source_yaml = load_task(_FIX / "adversarial_criteria_probe.yaml")
        plugin_root = (tmp_path / "skills_repo").resolve()
        (plugin_root / "skills").mkdir(parents=True)
        (plugin_root / "skills" / "SKILL.md").write_text("docs", encoding="utf-8")
        (plugin_root / "tests").mkdir()
        grader = plugin_root / "tests" / "check_x.py"
        grader.write_text("EXPECTED = 'UiPath.Template.REFramework'", encoding="utf-8")
        (plugin_root / "RESOLUTION.md").write_text("the answer", encoding="utf-8")
        task.agent.plugins = [{"type": "local", "path": str(plugin_root)}]

        runner = _make_runner(task, source_yaml)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        asyncio.run(runner._stage_inputs(input_dir))

        # Make the temp ancestor chain traversable (production /work mounts are).
        anc = tmp_path
        while anc != anc.parent and str(anc).startswith("/tmp"):
            os.chmod(anc, 0o711)  # traverse-only (o+x); NOT world-readable
            anc = anc.parent

        from coder_eval.cli.run_task_internal_command import _apply_isolation_barrier

        _apply_isolation_barrier(
            agent_run_uid=AGENT_UID,
            task=task,
            input_dir=input_dir,
            output_dir=tmp_path / "output",
            task_dir=plugin_root,
            workspace_dir=tmp_path / "output" / "ws",
            plugin_host_paths=[str(plugin_root)],
        )

        # The dropped CLI (via the REAL shim) must EACCES on the grader, RESOLUTION.md,
        # and task_full.json (assert via `cat` exit code, not a forked os.setuid).
        for target in (grader, plugin_root / "RESOLUTION.md", input_dir / "task_full.json"):
            res = self._run_dropped(f"cat {target}")
            assert res.returncode != 0, f"agent uid must NOT read {target}: {res.stdout!r}"
            assert "denied" in res.stderr.lower() or "permission" in res.stderr.lower(), res.stderr
