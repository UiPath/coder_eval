"""Regression guard for the docker criteria/grader leak — closed by ABSENCE.

Under ``--driver docker`` the COPY/PRUNE + GRADE-OUTSIDE design gives the agent
container ONLY a sanitized, criteria-free view of its inputs:

- the staged ``task.yaml`` is ``agent_safe_dump``-stripped (``success_criteria: []``,
  ``reference: null``) and ``context.json``'s ``source_yaml`` is nulled, so no
  grading material is in ``/work/input``;
- plugins are projected through ``PLUGIN_AGENT_ALLOWED_SUBDIRS`` into a sanitized
  copy (skills/commands/agents/hooks/.claude-plugin only) mounted read-only at
  ``CONTAINER_SKILL_DOCS_DIR`` — grader trees (``tests/``, ``check_*.py``),
  ``RESOLUTION.md``, reference agents and fixtures are never copied;
- the raw skills-repo checkout, the reference, and the host task dir are NOT
  mounted into the agent container at all.

So the leak is closed by ABSENCE — the material simply is not in the agent's
mount namespace — NOT by a permission barrier. These tests reproduce the closure
deterministically, with NO model and NO docker daemon: they stage a task exactly
as the docker driver does and scan the ENTIRE agent-readable mount view.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation.docker_runner import DockerRunError, DockerRunner, _overlaps_grader_dir
from coder_eval.models import (
    AGENT_HIDDEN_TASK_FIELDS,
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
    TemplateDirSource,
)
from coder_eval.orchestration.task_loader import load_task


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")

# Each fixture pairs a staged task with the expected-value "sentinels" from its own
# success_criteria. If a sentinel is reachable anywhere in the agent mount view, the
# answer key leaked.
#   - adversarial_criteria_probe: synthetic sentinel in the criteria rubric.
#   - template_aware_create_adversarial: the REAL rubric value a model lifted.
_FIX = Path(__file__).parent / "_fixtures" / "tasks"
FIXTURES = [
    pytest.param(_FIX / "adversarial_criteria_probe.yaml", ["LEAKED-ANSWER-SENTINEL-9f3a2b"], id="synthetic"),
    pytest.param(
        # The genuinely-hidden expected VALUE is the packageId
        # `UiPath.Template.REFramework` (the answer to `template_package_id`). The
        # strategy name `template_package` is NOT a hidden answer — it appears in the
        # task's own initial_prompt, so it legitimately survives the criteria strip.
        _FIX / "template_aware_create_adversarial.yaml",
        ["UiPath.Template.REFramework"],
        id="real-peeked",
    ),
]


def _make_runner(task, source_yaml: str) -> DockerRunner:
    """A DockerRunner over a MagicMock ResolvedTask (mirrors test_docker_runner_mounts.py)."""
    rt = MagicMock()
    rt.task = task
    rt.run_dir = Path(tempfile.gettempdir()) / "test_criteria_iso_run"
    rt.variant_id = None
    rt.replicate_index = 0
    rt.config_lineage = {}
    rt.source_yaml = source_yaml
    rt.task_file = None
    return DockerRunner(rt)


def _stage_agent_mount_view(task, source_yaml: str) -> Path:
    """Stage the task exactly as the docker driver does and return a root dir
    containing the FULL agent-readable mount view: /work/input (task.yaml +
    context.json) AND the sanitized skills copy (/work/skills). Nothing here is
    permission-locked — everything the agent uid could read is present."""
    runner = _make_runner(task, source_yaml)
    root = Path(tempfile.mkdtemp())
    input_dir = root / "input"
    input_dir.mkdir()
    asyncio.run(runner._stage_inputs(input_dir))
    # The sanitized skills copy (mounted :ro at CONTAINER_SKILL_DOCS_DIR).
    staging = root / "staging"
    staging.mkdir()
    runner._prepare_host_mounts(staging)
    return root


def _agent_reachable_text(root: Path) -> str:
    """Concatenate every file an agent could read across the full mount view."""
    return "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted(root.rglob("*"))
        # Exclude the ~/.claude / ~/.uipath copies (host dotfiles, not task inputs)
        # so the scan targets the task-input + skills mount surface.
        if p.is_file() and "claude-home" not in p.parts and "uipath-home" not in p.parts
    )


@pytest.mark.parametrize(("fixture", "sentinels"), FIXTURES)
def test_success_criteria_not_reachable_from_agent_mount(fixture: Path, sentinels: list[str]) -> None:
    """ABSENCE GUARD: no grading-rubric expected value appears anywhere in the full
    agent mount view (/work/input + the sanitized skills copy). The criteria are
    stripped from the staged task.yaml and the raw skills/reference/task-dir are
    never mounted — so the answer key is absent, not merely unreadable."""
    task, source_yaml = load_task(fixture)
    # POSITIVE CONTROL: the sentinel MUST be present in the raw source YAML before
    # staging, otherwise the "not reachable" assert below could pass vacuously (e.g.
    # a typo'd sentinel that never existed anywhere). This proves the strip removed
    # a value that was genuinely there.
    missing = [s for s in sentinels if s not in source_yaml]
    assert not missing, f"positive control failed: sentinels {missing} not in the pre-strip source_yaml"
    reachable = _agent_reachable_text(_stage_agent_mount_view(task, source_yaml))
    hits = [s for s in sentinels if s in reachable]
    assert not hits, f"answer-key leak: agent can read {hits} from the agent mount view"


def test_stripped_fields_are_the_ssot_set() -> None:
    """The fields agent_safe_dump strips are exactly AGENT_HIDDEN_TASK_FIELDS
    (the SSOT), so this test reasons about the same field set the strip enforces."""
    assert frozenset({"success_criteria", "reference"}) == AGENT_HIDDEN_TASK_FIELDS


# ---- Detector B: zero grading material in the agent mount --------------------


def _build_plugin_with_grader(root: Path) -> Path:
    """A plugin root carrying a legitimate skill AND grader/reference material
    that must be pruned out of the agent bundle."""
    plugin = root / "myplugin"
    (plugin / "skills").mkdir(parents=True)
    (plugin / "skills" / "SKILL.md").write_text("How to do the task (docs).", encoding="utf-8")
    (plugin / "tests").mkdir()
    (plugin / "tests" / "check_answer.py").write_text("assert result == 'GRADER-ONLY-SENTINEL-7c2e'", encoding="utf-8")
    (plugin / "RESOLUTION.md").write_text("The reference solution is X.", encoding="utf-8")
    (plugin / "reference_agents").mkdir()
    (plugin / "reference_agents" / "golden.py").write_text("REFERENCE-GOLDEN-42", encoding="utf-8")
    return plugin


def test_detector_b_zero_grading_material_in_agent_mount(tmp_path) -> None:
    """DETECTOR B: stage a task with real criteria + a plugin bundling grader /
    reference material; scan the ENTIRE agent mount view and assert zero grading
    hits. Positive control: the HOST still holds the full criteria, so a vacuous
    'staged nothing' bug cannot pass."""
    from coder_eval.models import FileExistsCriterion, ReferenceSource, SandboxConfig, TaskDefinition

    plugin = _build_plugin_with_grader(tmp_path)
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="do it",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code", "plugins": [{"type": "local", "path": str(plugin)}]},
        reference=ReferenceSource(code="REFERENCE-SOLUTION-SENTINEL"),
        success_criteria=[
            FileExistsCriterion(description="c", path="out.txt"),
        ],
    )
    runner = _make_runner(task, source_yaml="raw yaml carrying REFERENCE-SOLUTION-SENTINEL")
    root = Path(tempfile.mkdtemp())
    input_dir = root / "input"
    input_dir.mkdir()
    asyncio.run(runner._stage_inputs(input_dir))
    staging = root / "staging"
    staging.mkdir()
    runner._prepare_host_mounts(staging)

    reachable = _agent_reachable_text(root)
    # Grading-material sentinels: reference value, grader token, grader/reference files.
    assert "REFERENCE-SOLUTION-SENTINEL" not in reachable  # reference value gone
    assert "GRADER-ONLY-SENTINEL-7c2e" not in reachable  # grader token gone
    assert "REFERENCE-GOLDEN-42" not in reachable  # reference_agents pruned
    # No grader/reference FILES are present in the sanitized skills copy.
    skills_copy = staging / "skills"
    assert list(skills_copy.rglob("check_*.py")) == []
    assert list(skills_copy.rglob("RESOLUTION.md")) == []
    assert list(skills_copy.rglob("reference_agents")) == []
    # But the legitimate skill doc IS present (the agent still gets its docs).
    assert (skills_copy / "myplugin" / "skills" / "SKILL.md").is_file()

    # Positive control: the HOST task still carries the full criteria + reference,
    # so this is a real strip, not a vacuous "staged nothing".
    assert task.reference is not None and task.reference.code == "REFERENCE-SOLUTION-SENTINEL"
    assert len(task.success_criteria) == 1


class TestOverlapsGraderDir:
    """The grade-outside overlap guard is a CONTENT test for the descendant case: a
    child subtree of the grader dir is blocked only if it itself holds a grader
    artifact (check_*.py or the task's reference); a clean child is allowed. The
    task-dir-itself and ancestor cases block unconditionally."""

    def test_clean_child_is_allowed(self, tmp_path):
        """A clean child beside a root check_*.py must NOT overlap."""
        taskdir = tmp_path / "taskdir"
        fixtures = taskdir / "fixtures"
        fixtures.mkdir(parents=True)
        assert _overlaps_grader_dir(fixtures, taskdir, [taskdir / "check_grade.py"]) is False

    def test_task_dir_itself_always_blocks(self, tmp_path):
        """The grader dir itself blocks regardless of the artifact set."""
        taskdir = tmp_path / "taskdir"
        taskdir.mkdir()
        assert _overlaps_grader_dir(taskdir, taskdir, [taskdir / "check_grade.py"]) is True
        assert _overlaps_grader_dir(taskdir, taskdir, []) is True

    def test_ancestor_always_blocks(self, tmp_path):
        """A parent of the grader dir contains it → block."""
        taskdir = tmp_path / "parent" / "taskdir"
        taskdir.mkdir(parents=True)
        assert _overlaps_grader_dir(taskdir.parent, taskdir, []) is True

    def test_grader_containing_child_via_check_script_blocks(self, tmp_path):
        """A child whose subtree holds a check_*.py → block."""
        taskdir = tmp_path / "taskdir"
        graders = taskdir / "graders"
        graders.mkdir(parents=True)
        assert _overlaps_grader_dir(graders, taskdir, [graders / "check_x.py"]) is True

    def test_grader_containing_child_via_reference_blocks(self, tmp_path):
        """A child whose subtree holds the resolved reference path → block."""
        taskdir = tmp_path / "taskdir"
        refdir = taskdir / "solution"
        refdir.mkdir(parents=True)
        assert _overlaps_grader_dir(refdir, taskdir, [(refdir / "answer.py").resolve()]) is True

    def test_reference_directory_itself_blocks(self, tmp_path):
        """Mounting the resolved reference DIRECTORY itself → block (the a == target arm)."""
        taskdir = tmp_path / "taskdir"
        refdir = taskdir / "solution"
        refdir.mkdir(parents=True)
        assert _overlaps_grader_dir(refdir.resolve(), taskdir, [refdir.resolve()]) is True

    def test_subdir_of_reference_directory_blocks(self, tmp_path):
        """A child that is a SUBDIR of the reference directory re-exposes the golden
        solution → block. The content test is directional: an artifact that is a
        DIRECTORY must protect its whole subtree, not just itself and its ancestors."""
        taskdir = tmp_path / "taskdir"
        refdir = taskdir / "solution"
        sub = refdir / "src"
        sub.mkdir(parents=True)
        assert _overlaps_grader_dir(sub.resolve(), taskdir, [refdir.resolve()]) is True

    def test_none_grader_dir_never_overlaps(self, tmp_path):
        """grader_dir is None (library/test path without a task_file) → never overlaps."""
        assert _overlaps_grader_dir(tmp_path / "anything", None) is False


class TestArgvGraderDirLeakDetector:
    """Argv-level proof that a CLEAN child mount does not materialize the graders,
    and that a grader-containing child is still hard-rejected. Runs under
    ``make test-docker-detectors`` (daemon-less, no model)."""

    def _make_runner(
        self, tmp_path: Path, *, template_path: str, task_file: Path, reference_directory: str | None = None
    ) -> DockerRunner:
        from coder_eval.models import ReferenceSource

        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            sandbox=SandboxConfig(template_sources=[TemplateDirSource(path=template_path)]),
            agent={"type": "claude-code"},
            reference=ReferenceSource(directory=reference_directory) if reference_directory else None,
            success_criteria=[FileExistsCriterion(description="c", path="o.txt")],
        )
        rt = MagicMock()
        rt.task = task
        rt.run_dir = tmp_path / "run"
        rt.task_file = task_file
        return DockerRunner(rt)

    def _argv(self, runner: DockerRunner, tmp_path: Path) -> list[str]:
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        input_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)
        return runner._build_argv(input_dir, output_dir, container_name="c", image="img")

    @staticmethod
    def _mount_sources(argv: list[str]) -> list[str]:
        """The host SOURCE component (leading path before the first POSIX ``:``) of every -v."""
        specs = [argv[i + 1] for i, a in enumerate(argv) if a == "-v" and i + 1 < len(argv)]
        return [spec.split(":", 1)[0] for spec in specs]

    def test_clean_child_mounted_graders_absent(self, tmp_path):
        """A clean fixtures/ child beside a root check_grade.py is mounted, and no
        -v source is the grader dir or a check_*.py."""
        taskdir = tmp_path / "taskdir"
        fixtures = taskdir / "fixtures"
        fixtures.mkdir(parents=True)
        (taskdir / "check_grade.py").write_text("assert False\n", encoding="utf-8")
        (fixtures / "seed.txt").write_text("benign\n", encoding="utf-8")
        task_file = taskdir / "task.yaml"
        task_file.write_text("x", encoding="utf-8")

        runner = self._make_runner(tmp_path, template_path=str(fixtures), task_file=task_file)
        argv = self._argv(runner, tmp_path)  # must NOT raise

        sources = self._mount_sources(argv)
        assert str(fixtures.resolve()) in sources  # the clean child IS mounted
        assert str(taskdir.resolve()) not in sources  # the grader dir is NOT a mount source
        assert all("check_grade.py" not in s for s in sources)  # no grader script mounted

    def test_grader_containing_child_is_rejected(self, tmp_path):
        """A child whose subtree holds a check_*.py still hard-rejects."""
        taskdir = tmp_path / "taskdir"
        graders = taskdir / "graders"
        graders.mkdir(parents=True)
        (graders / "check_x.py").write_text("assert False\n", encoding="utf-8")
        task_file = taskdir / "task.yaml"
        task_file.write_text("x", encoding="utf-8")

        runner = self._make_runner(tmp_path, template_path=str(graders), task_file=task_file)
        with pytest.raises(DockerRunError, match="grader dir"):
            self._argv(runner, tmp_path)

    def test_subdir_of_reference_directory_is_rejected(self, tmp_path):
        """A template source that is a SUBDIR of the task's reference.directory
        would mount part of the golden solution into the agent → hard-reject.
        The content test protects a reference DIRECTORY's whole subtree."""
        taskdir = tmp_path / "taskdir"
        refdir = taskdir / "solution"
        sub = refdir / "src"
        sub.mkdir(parents=True)
        (sub / "answer.py").write_text("GOLDEN\n", encoding="utf-8")
        task_file = taskdir / "task.yaml"
        task_file.write_text("x", encoding="utf-8")

        runner = self._make_runner(
            tmp_path, template_path=str(sub), task_file=task_file, reference_directory="solution"
        )
        with pytest.raises(DockerRunError, match="grader dir"):
            self._argv(runner, tmp_path)
