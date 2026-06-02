"""Unit tests for the generic config-merge resolver (in isolation).

The pure engine (``merge_layers``) is tested against SYNTHETIC models carrying
explicit ``MergeField`` strategies, so these tests don't depend on the
production-model annotations added in Phase 2. ``resolve_root`` / ``validate_paths``
/ union-derivation are tested against the real ``agent`` / ``run_limits`` /
``sandbox`` roots.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from coder_eval.models import (
    ClaudeCodeAgentConfig,
    ConfigLineageEntry,
    MergeField,
    RunLimits,
    classify_annotation,
    merge_strategy_of,
)
from coder_eval.orchestration.config_merge import (
    Layer,
    MergeError,
    merge_layers,
    resolve_root,
    validate_paths,
)


# --------------------------------------------------------------------------
# Synthetic models for pure-engine tests
# --------------------------------------------------------------------------


class _Nested(BaseModel):
    model_config = ConfigDict(extra="forbid")

    network: str = "bridge"
    extras: list[str] = MergeField(strategy="append", default_factory=list)
    tags: list[str] = MergeField(strategy="replace", default_factory=list)


class _Root(BaseModel):
    model_config = ConfigDict(extra="forbid")

    _merge_exclusive_groups: ClassVar[tuple[tuple[str, ...], ...]] = (("prompt", "prompt_file"),)

    model: str | None = None
    nested: _Nested | None = None  # nested BaseModel -> deep (type-aware)
    opts: dict[str, Any] = MergeField(strategy="deep", default_factory=dict)
    tools: list[str] = MergeField(strategy="replace", default_factory=list)
    appended: list[str] = MergeField(strategy="append", default_factory=list)
    appended_rev: list[str] = MergeField(strategy="append", append_order="reverse", default_factory=list)
    prompt: str | None = None
    prompt_file: str | None = None


def _layer(source, patch, **kw):
    return Layer(source=source, patch=patch, **kw)


# --------------------------------------------------------------------------
# merge_strategy_of / _default_strategy_for
# --------------------------------------------------------------------------


class TestStrategyReader:
    def test_explicit_annotation_wins(self):
        assert merge_strategy_of(_Nested.model_fields["extras"]) == "append"
        assert merge_strategy_of(_Nested.model_fields["tags"]) == "replace"
        assert merge_strategy_of(_Root.model_fields["opts"]) == "deep"

    def test_unannotated_nested_model_is_deep(self):
        assert merge_strategy_of(_Root.model_fields["nested"]) == "deep"

    def test_unannotated_free_form_dict_is_deep(self):
        assert merge_strategy_of(ClaudeCodeAgentConfig.model_fields["sdk_options"]) == "deep"

    def test_unannotated_list_is_replace(self):
        assert merge_strategy_of(RunLimits.model_fields["max_turns"]) == "replace"  # scalar
        # an unannotated list field falls back to replace
        assert merge_strategy_of(ClaudeCodeAgentConfig.model_fields["allowed_tools"]) == "replace"

    def test_append_order_reader(self):
        from coder_eval.models.merge_strategy import append_order_of

        assert append_order_of(_Root.model_fields["appended"]) == "forward"  # default
        assert append_order_of(_Root.model_fields["appended_rev"]) == "reverse"
        assert append_order_of(_Root.model_fields["tools"]) == "forward"  # non-append → default

    def test_scalar_is_replace(self):
        assert merge_strategy_of(_Root.model_fields["model"]) == "replace"

    def test_classify_optional_model_and_str_dict_union(self):
        models, free = classify_annotation(_Root.model_fields["nested"].annotation)
        assert models == [_Nested] and free is False
        # str | dict | None -> deep (free-form dict present)
        _models2, free2 = classify_annotation(ClaudeCodeAgentConfig.model_fields["claude_settings"].annotation)
        assert free2 is True


# --------------------------------------------------------------------------
# merge_layers — replace / deep / append / exclusion / lineage seed
# --------------------------------------------------------------------------


class TestMergeLayersReplace:
    def test_last_layer_wins_and_lineage_records_winner(self):
        lineage: dict[str, ConfigLineageEntry] = {}
        merged = merge_layers(
            (_Root,),
            [_layer("task", {"model": "a"}), _layer("variant", {"model": "b"})],
            lineage_root="root",
            lineage=lineage,
        )
        assert merged["model"] == "b"
        assert lineage["root.model"].source == "variant"
        assert lineage["root.model"].value == "b"


class TestMergeLayersDeepModel:
    def test_nested_fields_merge_by_their_strategies(self):
        merged = merge_layers(
            (_Root,),
            [
                _layer("task", {"nested": {"network": "bridge", "extras": ["A"], "tags": ["x"]}}),
                _layer("variant", {"nested": {"network": "none", "extras": ["B"], "tags": ["y"]}}),
            ],
            lineage_root="root",
        )
        # network: replace -> none; extras: append -> A+B; tags: replace -> y; sibling preserved
        assert merged["nested"]["network"] == "none"
        assert merged["nested"]["extras"] == ["A", "B"]
        assert merged["nested"]["tags"] == ["y"]

    def test_sibling_key_preserved_across_layers(self):
        merged = merge_layers(
            (_Root,),
            [
                _layer("task", {"nested": {"network": "host"}}),
                _layer("variant", {"nested": {"extras": ["B"]}}),
            ],
            lineage_root="root",
        )
        assert merged["nested"]["network"] == "host"  # set by task, not dropped by variant
        assert merged["nested"]["extras"] == ["B"]

    def test_deep_model_lineage_is_coarse_single_entry(self):
        lineage: dict[str, ConfigLineageEntry] = {}
        merge_layers(
            (_Root,),
            [_layer("task", {"nested": {"network": "host"}}), _layer("variant", {"nested": {"extras": ["B"]}})],
            lineage_root="root",
            lineage=lineage,
        )
        assert "root.nested" in lineage
        assert lineage["root.nested"].source == "variant"  # highest layer touching the subtree
        # no per-leaf entries
        assert not any(k.startswith("root.nested.") for k in lineage)

    def test_unknown_nested_key_raises_with_suggestion(self):
        """A typo inside a deep-model nested block (the layers-1-4 path, which never
        calls validate_paths) is caught by _merge_dict_by_model's per-key check."""
        with pytest.raises(MergeError) as ei:
            merge_layers(
                (_Root,),
                [_layer("variant", {"nested": {"netwrok": "none"}})],
                lineage_root="root",
            )
        assert "netwrok" in str(ei.value)
        assert "root.nested" in str(ei.value)  # context names the nested path
        assert ei.value.suggestion == "network"  # did-you-mean across _Nested's fields

    def test_non_dict_replaces_nested_model_wholesale(self):
        """A deep nested-model field receiving a non-dict value (e.g. None) replaces
        wholesale rather than deep-merging onto the lower layer's dict."""
        merged = merge_layers(
            (_Root,),
            [_layer("task", {"nested": {"network": "host", "extras": ["A"]}}), _layer("variant", {"nested": None})],
            lineage_root="root",
        )
        assert merged["nested"] is None  # the task's nested dict is discarded, not merged


class TestMergeLayersDeepDict:
    def test_result_does_not_alias_input_patch_leaves(self):
        """The merged result must share NO mutable structure with the input patches —
        mutating a resolved nested leaf must not leak back into a patch reused across
        rows (e.g. a variant's sdk_options dict)."""
        shared_patch = {"opts": {"nested": {"budget": 1000}, "tags": ["a"]}}
        merged = merge_layers((_Root,), [_layer("variant", shared_patch)], lineage_root="root")
        merged["opts"]["nested"]["budget"] = 99999
        merged["opts"]["tags"].append("b")
        # the source patch is untouched
        assert shared_patch["opts"]["nested"]["budget"] == 1000
        assert shared_patch["opts"]["tags"] == ["a"]

    def test_recursive_merge_preserves_siblings(self):
        merged = merge_layers(
            (_Root,),
            [
                _layer("task", {"opts": {"a": 1, "shape": {"x": 1, "y": 2}}}),
                _layer("variant", {"opts": {"b": 2, "shape": {"y": 9}}}),
            ],
            lineage_root="root",
        )
        assert merged["opts"]["a"] == 1
        assert merged["opts"]["b"] == 2
        # nested dict merges (not replaces): x survives, y overridden
        assert merged["opts"]["shape"] == {"x": 1, "y": 9}

    def test_deep_dict_lineage_per_top_level_leaf(self):
        lineage: dict[str, ConfigLineageEntry] = {}
        merge_layers(
            (_Root,),
            [_layer("task", {"opts": {"a": 1}}), _layer("variant", {"opts": {"b": 2}})],
            lineage_root="root",
            lineage=lineage,
        )
        assert lineage["root.opts.a"].source == "task"
        assert lineage["root.opts.b"].source == "variant"


class TestMergeLayersAppend:
    def test_concatenation_in_layer_order(self):
        merged = merge_layers(
            (_Root,),
            [
                _layer("default", {"appended": ["A"]}),
                _layer("task", {"appended": ["B"]}),
                _layer("variant", {"appended": ["C"]}),
            ],
            lineage_root="root",
        )
        assert merged["appended"] == ["A", "B", "C"]

    def test_lineage_at_field_path_records_highest_contributor(self):
        lineage: dict[str, ConfigLineageEntry] = {}
        merge_layers(
            (_Root,),
            [_layer("task", {"appended": ["A"]}), _layer("variant", {"appended": ["B"]})],
            lineage_root="root",
            lineage=lineage,
        )
        assert lineage["root.appended"].source == "variant"
        assert lineage["root.appended"].value == ["A", "B"]

    def test_inputs_not_mutated(self):
        a = ["A"]
        b = ["B"]
        merge_layers(
            (_Root,), [_layer("task", {"appended": a}), _layer("variant", {"appended": b})], lineage_root="root"
        )
        assert a == ["A"] and b == ["B"]

    def test_reverse_append_puts_higher_layer_first(self):
        """append_order='reverse' (e.g. post_run): each higher layer's items go FIRST."""
        merged = merge_layers(
            (_Root,),
            [_layer("default", {"appended_rev": ["A"]}), _layer("task", {"appended_rev": ["B"]})],
            lineage_root="root",
        )
        # forward would give [A, B]; reverse gives [B, A]
        assert merged["appended_rev"] == ["B", "A"]

    def test_non_list_under_append_raises(self):
        with pytest.raises(MergeError, match="expected a list for append-strategy"):
            merge_layers((_Root,), [_layer("task", {"appended": "oops"})], lineage_root="root")

    def test_none_under_append_is_noop(self):
        """A None value for an append field (e.g. a full-dump seed of an unset
        Optional list) contributes nothing instead of erroring."""
        merged = merge_layers(
            (_Root,),
            [_layer("cli", {"appended": None}, record_lineage=False), _layer("variant", {"appended": ["B"]})],
            lineage_root="root",
        )
        assert merged["appended"] == ["B"]


class TestExclusionGroups:
    def test_setting_one_member_drops_sibling_value_and_lineage(self):
        lineage: dict[str, ConfigLineageEntry] = {}
        merged = merge_layers(
            (_Root,),
            [_layer("task", {"prompt": "inline"}), _layer("variant", {"prompt_file": "p.txt"})],
            lineage_root="root",
            lineage=lineage,
        )
        assert "prompt" not in merged
        assert merged["prompt_file"] == "p.txt"
        assert "root.prompt" not in lineage
        assert lineage["root.prompt_file"].source == "variant"

    def test_silent_seed_clears_value_but_not_sibling_lineage(self):
        """A record_lineage=False seed setting one group member clears the sibling's
        VALUE from the accumulator but must NOT drop the sibling's lineage entry —
        the silent seed writes/removes no lineage."""
        lineage: dict[str, ConfigLineageEntry] = {
            "root.prompt_file": ConfigLineageEntry(value="f.txt", source="task"),
        }
        merged = merge_layers(
            (_Root,),
            [_layer("cli", {"prompt": "seeded"}, record_lineage=False)],
            lineage_root="root",
            lineage=lineage,
        )
        assert merged["prompt"] == "seeded"
        assert "prompt_file" not in merged  # value cleared by exclusion
        # but the lineage entry survived (silent seed touches no lineage)
        assert lineage["root.prompt_file"].source == "task"

    def test_full_dump_seed_with_both_members_one_none(self):
        """A full-``model_dump`` seed sets BOTH members (one None). The None member
        must not count as "set" (so it doesn't clear the other); the non-None
        member survives and reconstruction tolerates the None sibling."""
        merged = merge_layers(
            (_Root,),
            [_layer("cli", {"prompt": "x", "prompt_file": None}, record_lineage=False)],
            lineage_root="root",
        )
        assert merged["prompt"] == "x"
        assert merged.get("prompt_file") is None

    def test_real_agent_seed_with_none_sibling_reconstructs(self):
        """A full-dump-style agent seed carrying system_prompt + system_prompt_file=None
        reconstructs cleanly (the None sibling doesn't trip check_prompt_exclusivity)."""
        agent = resolve_root(
            "agent",
            [
                Layer(
                    source="cli",
                    patch={"type": "claude-code", "system_prompt": "x", "system_prompt_file": None},
                    record_lineage=False,
                )
            ],
        )
        assert agent is not None
        assert agent.system_prompt == "x"
        assert agent.system_prompt_file is None


class TestConfigSourceContract:
    def test_config_source_subset_of_lineage_source(self):
        """Every ``Layer.source`` value must be a valid ``ConfigLineageEntry.source``
        (the two hand-maintained Literals must not drift)."""
        from typing import get_args

        from coder_eval.orchestration.config_merge import ConfigSource

        layer_sources = set(get_args(ConfigSource))
        lineage_sources = set(get_args(ConfigLineageEntry.model_fields["source"].annotation))
        assert layer_sources <= lineage_sources, (
            f"ConfigSource has values ConfigLineageEntry.source rejects: {layer_sources - lineage_sources}"
        )


class TestLineageSeed:
    def test_record_lineage_false_contributes_values_only(self):
        lineage: dict[str, ConfigLineageEntry] = {
            "root.model": ConfigLineageEntry(value="from-lower", source="task"),
        }
        merged = merge_layers(
            (_Root,),
            [
                _layer("cli", {"model": "seeded", "tools": ["X"]}, record_lineage=False),
                _layer("cli", {"tools": ["Y"]}),
            ],
            lineage_root="root",
            lineage=lineage,
        )
        # seed value flows in then is overridden by the recording layer for tools
        assert merged["model"] == "seeded"
        assert merged["tools"] == ["Y"]
        # the seed wrote NO lineage: the pre-seeded task entry for model survives
        assert lineage["root.model"].source == "task"
        # only the recording layer's key gets a cli entry
        assert lineage["root.tools"].source == "cli"


class TestUnknownKey:
    def test_unknown_key_raises_with_suggestion(self):
        with pytest.raises(MergeError, match="did you mean 'model'"):
            merge_layers((_Root,), [_layer("task", {"modle": "x"})], lineage_root="root")

    def test_empty_layers(self):
        assert merge_layers((_Root,), [], lineage_root="root") == {}


# --------------------------------------------------------------------------
# Root derivation + resolve_root + validate_paths (real roots)
# --------------------------------------------------------------------------


class TestUnionDerivation:
    def test_agent_resolves_to_all_members_including_claude_only_field(self):
        # A Claude-only field (sdk_options) is recognized -> resolve_root accepts it.
        agent = resolve_root("agent", [_layer("task", {"type": "claude-code", "sdk_options": {"effort": "low"}})])
        assert agent is not None
        assert agent.sdk_options == {"effort": "low"}

    def test_no_hardcoded_union_tuple(self):
        """Union members come from TaskDefinition.model_fields, not a literal tuple."""
        from coder_eval.orchestration.config_merge import _root_model_types

        members = {m.__name__ for m in _root_model_types("agent")}
        assert {"ClaudeCodeAgentConfig", "CodexAgentConfig", "BaseAgentConfig"} <= members


class TestResolveRoot:
    def test_run_limits_field_merge_survives_unset_keys(self):
        rl = resolve_root(
            "run_limits",
            [_layer("exp", {"turn_timeout": 60}), _layer("variant", {"task_timeout": 120})],
        )
        assert isinstance(rl, RunLimits)
        assert rl.turn_timeout == 60  # set in layer 1, not clobbered by a layer that only set task_timeout
        assert rl.task_timeout == 120

    def test_run_limits_empty_returns_none(self):
        assert resolve_root("run_limits", [_layer("cli", {}, record_lineage=False)]) is None

    def test_unknown_root_raises(self):
        with pytest.raises(MergeError, match="unknown override root"):
            resolve_root("bogus", [])  # type: ignore[arg-type]


class TestValidatePaths:
    @pytest.mark.parametrize(
        "path",
        [
            "agent.model",
            "agent.permission_mode",
            "run_limits.max_turns",
            "sandbox.driver",
            "sandbox.docker.network",
            "agent.sdk_options.effort",
            "agent.sdk_options.anything.nested",
        ],
    )
    def test_happy_paths(self, path):
        validate_paths([path])

    def test_unknown_root(self):
        with pytest.raises(MergeError, match="unknown override root 'foo'"):
            validate_paths(["foo.bar"])

    def test_root_only_rejected(self):
        with pytest.raises(MergeError, match="must target a field"):
            validate_paths(["agent"])

    def test_typo_suggests_field(self):
        with pytest.raises(MergeError, match="did you mean 'model'"):
            validate_paths(["agent.modle"])

    def test_extra_segment_past_scalar(self):
        with pytest.raises(MergeError, match="scalar or list leaf"):
            validate_paths(["agent.model.x"])
