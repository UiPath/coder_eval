"""The executable architectural contract for the declarative-merge refactor.

Two gates:

1. **Unification invariant** — for a representative patch P, the resolved
   ``agent``/``run_limits``/``retry``/``sandbox`` is identical whether P arrives as a
   higher config layer (layers 1-4) or as a ``-D`` override (layer 5). If this
   fails, the two paths are not one engine.
2. **Lineage parity** — applying a ``-D`` touching one field leaves every other
   field's layers-1-4 provenance byte-identical, adding only a ``source="cli"``
   entry for the touched field. The value-only invariant cannot catch a lineage
   regression, so this is a separate gate.
"""

from __future__ import annotations

from coder_eval.models import (
    DockerDriverConfig,
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    ResourceLimits,
    RetryPolicy,
    RunLimits,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.orchestration.experiment import resolve_task_for_variant
from coder_eval.orchestration.overrides import apply_overrides


def _default_exp() -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={"type": "claude-code", "permission_mode": "acceptEdits"}),
        variants=[ExperimentVariant(variant_id="default")],
    )


def _task(**kwargs) -> TaskDefinition:
    base = {
        "task_id": "t",
        "description": "x",
        "initial_prompt": "hi",
        "agent": {"type": "claude-code"},
        "sandbox": SandboxConfig(driver="tempdir"),
        "success_criteria": [{"type": "file_exists", "path": "f.txt", "description": "x"}],
    }
    base.update(kwargs)
    return TaskDefinition(**base)


def _resolve(task, *, exp_defaults=None, variant=None):
    """Resolve through layers 1-4 with an optional exp-defaults / variant override."""
    experiment = ExperimentDefinition(
        experiment_id="test",
        defaults=exp_defaults,
        variants=[variant or ExperimentVariant(variant_id="v")],
    )
    resolved, lineage, _ = resolve_task_for_variant(_default_exp(), task, experiment, experiment.variants[0])
    return resolved, lineage


class TestUnificationInvariant:
    """resolve(P as a config layer) == resolve(P as -D), for every root."""

    def test_agent_sdk_options(self):
        # Path A: P at the variant layer.
        a, _ = _resolve(_task(), variant=ExperimentVariant(variant_id="v", agent={"sdk_options": {"effort": "high"}}))
        # Path B: base resolved, then P as -D.
        b, _ = _resolve(_task())
        apply_overrides(b, {"agent.sdk_options.effort": "high"})
        assert a.agent.model_dump() == b.agent.model_dump()
        assert a.agent.sdk_options == {"effort": "high"}

    def test_agent_system_prompt_file_clears_sibling(self):
        task_a = _task(agent={"type": "claude-code", "system_prompt": "inherited"})
        a, _ = _resolve(task_a, variant=ExperimentVariant(variant_id="v", agent={"system_prompt_file": "p.txt"}))
        b, _ = _resolve(_task(agent={"type": "claude-code", "system_prompt": "inherited"}))
        apply_overrides(b, {"agent.system_prompt_file": "p.txt"})
        assert a.agent.model_dump() == b.agent.model_dump()
        assert a.agent.system_prompt is None
        assert a.agent.system_prompt_file == "p.txt"

    def test_run_limits_max_turns(self):
        a, _ = _resolve(_task(), variant=ExperimentVariant(variant_id="v", run_limits=RunLimits(max_turns=5)))
        b, _ = _resolve(_task())
        apply_overrides(b, {"run_limits.max_turns": 5})
        assert a.run_limits.model_dump() == b.run_limits.model_dump()

    def test_retry_max_retries(self):
        a, _ = _resolve(_task(), variant=ExperimentVariant(variant_id="v", retry=RetryPolicy(max_retries=0)))
        b, _ = _resolve(_task())
        apply_overrides(b, {"retry.max_retries": 0})
        assert a.retry is not None and b.retry is not None
        assert a.retry.model_dump() == b.retry.model_dump()
        assert a.retry.max_retries == 0

    def test_retry_field_merge_keeps_lower_layer_keys(self):
        """A variant setting one retry key must not replace the task's whole block."""
        task = _task(retry=RetryPolicy(initial_delay=9.0))
        a, _ = _resolve(task, variant=ExperimentVariant(variant_id="v", retry=RetryPolicy(max_retries=1)))
        b, _ = _resolve(_task(retry=RetryPolicy(initial_delay=9.0)))
        apply_overrides(b, {"retry.max_retries": 1})
        assert a.retry.model_dump() == b.retry.model_dump()
        assert (a.retry.max_retries, a.retry.initial_delay) == (1, 9.0)

    def test_sandbox_driver(self):
        a, _ = _resolve(_task(), variant=ExperimentVariant(variant_id="v", driver="docker"))
        b, _ = _resolve(_task())
        apply_overrides(b, {"sandbox.driver": "docker"})
        assert a.sandbox.model_dump() == b.sandbox.model_dump()

    def test_sandbox_template_sources_append(self):
        a, _ = _resolve(
            _task(),
            variant=ExperimentVariant(variant_id="v", template_sources=[{"type": "template_dir", "path": "/x"}]),
        )
        b, _ = _resolve(_task())
        apply_overrides(b, {"sandbox.template_sources": [{"type": "template_dir", "path": "/x"}]})
        assert a.sandbox.model_dump() == b.sandbox.model_dump()

    def test_sandbox_docker_env_passthrough_extra_append(self):
        # Not variant-expressible — place P in exp-defaults (the task sets no extras,
        # so nothing else contributes and the append position is irrelevant).
        exp_defaults = ExperimentDefaults(sandbox=SandboxConfig(docker=DockerDriverConfig(env_passthrough_extra=["Y"])))
        a, _ = _resolve(_task(sandbox=SandboxConfig(driver="docker")), exp_defaults=exp_defaults)
        b, _ = _resolve(_task(sandbox=SandboxConfig(driver="docker")))
        apply_overrides(b, {"sandbox.docker.env_passthrough_extra": ["Y"]})
        assert a.sandbox.model_dump() == b.sandbox.model_dump()
        assert a.sandbox.docker.env_passthrough_extra == ["Y"]

    def test_sandbox_template_sources_append_with_lower_contribution(self):
        """The strongest append case: a lower layer (task) already contributed to the
        append list, and P adds more. The variant path appends after the task; the -D
        path appends after the full resolved list (which already includes the task) —
        they must produce the identical merged list."""

        def base_task():
            return _task(
                sandbox=SandboxConfig(driver="tempdir", template_sources=[{"type": "template_dir", "path": "/base"}])
            )

        a, _ = _resolve(
            base_task(),
            variant=ExperimentVariant(variant_id="v", template_sources=[{"type": "template_dir", "path": "/x"}]),
        )
        b, _ = _resolve(base_task())
        apply_overrides(b, {"sandbox.template_sources": [{"type": "template_dir", "path": "/x"}]})
        assert a.sandbox.model_dump() == b.sandbox.model_dump()
        assert [s.path for s in a.sandbox.template_sources] == ["/base", "/x"]

    def test_sandbox_partial_nested_limits_preserves_sibling(self):
        """A partial nested-model override (limits.timeout) over a task that set a
        DIFFERENT nested key (limits.max_memory_mb) — the sibling survives at BOTH
        layers (deep-model merge), and the two paths agree."""
        task = _task(sandbox=SandboxConfig(driver="docker", limits=ResourceLimits(max_memory_mb=512)))
        exp_defaults = ExperimentDefaults(sandbox=SandboxConfig(limits=ResourceLimits(timeout=60)))
        a, _ = _resolve(task, exp_defaults=exp_defaults)
        b, _ = _resolve(_task(sandbox=SandboxConfig(driver="docker", limits=ResourceLimits(max_memory_mb=512))))
        apply_overrides(b, {"sandbox.limits.timeout": 60})
        assert a.sandbox.model_dump() == b.sandbox.model_dump()
        assert a.sandbox.limits.timeout == 60
        assert a.sandbox.limits.max_memory_mb == 512  # sibling preserved at both


class TestSeedSafety:
    def test_exclude_unset_seeded_roots_have_no_append_fields(self):
        """agent / run_limits use an ``exclude_unset`` layer-5 seed; that is only
        invariant-safe while no field on them is ``append`` (an append field needs
        the FULL resolved list as its base, which exclude_unset can omit). sandbox
        uses a full-dump seed precisely because it has append fields. If a future
        agent/run_limits field declares ``append``, switch its root's seed to a full
        dump — this test fails loudly to force that."""
        from coder_eval.models import merge_strategy_of
        from coder_eval.orchestration.config_merge import _root_model_types

        for root in ("agent", "run_limits"):
            for model in _root_model_types(root):
                for name, field_info in model.model_fields.items():
                    assert merge_strategy_of(field_info) != "append", (
                        f"{root}.{name} declares strategy='append', but {root} uses an exclude_unset "
                        "layer-5 seed (overrides._resolve). Switch that seed to a full model_dump() or "
                        "the unification invariant will break silently for this field."
                    )


class TestLineageParity:
    def test_layer5_preserves_untouched_layers_1_4_lineage(self):
        """Resolve a rich fixture, snapshot lineage, apply a -D touching ONE field;
        every untouched entry must be byte-identical and only the touched field
        gains a source='cli' entry."""
        task = _task(
            agent={"type": "claude-code", "sdk_options": {"effort": "high"}},
            run_limits=RunLimits(task_timeout=300),
        )
        exp_defaults = ExperimentDefaults(
            agent={"model": "base-model"},
            run_limits=RunLimits(turn_timeout=60),
            driver="docker",
        )
        variant = ExperimentVariant(variant_id="v", agent={"model": "claude-haiku-4-5-20251001"})
        experiment = ExperimentDefinition(experiment_id="test", defaults=exp_defaults, variants=[variant])
        resolved, lineage = resolve_task_for_variant(_default_exp(), task, experiment, variant)[:2]

        import copy

        before = copy.deepcopy(lineage)
        apply_overrides(resolved, {"run_limits.turn_timeout": 45}, lineage=lineage)

        # the touched field flipped to cli
        assert lineage["run_limits.turn_timeout"].source == "cli"
        assert lineage["run_limits.turn_timeout"].source_detail == "-D run_limits.turn_timeout"
        # every other field's provenance is byte-identical
        for key, entry in before.items():
            if key == "run_limits.turn_timeout":
                continue
            assert lineage[key] == entry, f"lineage for {key!r} changed: {before[key]} -> {lineage[key]}"
