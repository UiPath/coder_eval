"""The single generic config-merge resolver.

One implementation that merges any of the three ``-D``-reachable root models
(``agent`` / ``run_limits`` / ``sandbox``) across an ordered list of partial-dict
:class:`Layer` records, applying each field's declared merge strategy
(:func:`coder_eval.models.merge_strategy.merge_strategy_of`) uniformly at every
layer. Both the layers-1-4 path (``resolve_task_for_variant``) and the layer-5
path (``apply_overrides``) call :func:`resolve_root`, so a given field merges
identically regardless of which layer supplied its value — that value-equality
is the unification invariant the refactor exists to guarantee.

The walk validates *paths* (unknown key -> :class:`MergeError` with a difflib
"did you mean?") and emits dotted lineage as a side effect; *value* validation
stays in Pydantic via reconstruction in :func:`resolve_root`.

Deliberately CLI-free (no ``typer`` import — lint rule CE004). The CLI boundary
wraps :class:`MergeError` into ``typer.BadParameter``.

See c/2026-06-01-declarative-merge-strategies.md.
"""

from __future__ import annotations

import copy
import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, overload

from pydantic import BaseModel

from ..models import (
    BaseAgentConfig,
    ClaudeCodeAgentConfig,
    CodexAgentConfig,
    ConfigLineageEntry,
    RunLimits,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from ..models.merge_strategy import AppendOrder, MergeStrategy, append_order_of, classify_annotation, merge_strategy_of


# Layer sources, ordered low -> high precedence. A subset of
# ``ConfigLineageEntry.source`` (which also carries "mutation"); kept in sync by
# tests/test_config_merge_engine.py::test_config_source_subset_of_lineage_source.
ConfigSource = Literal[
    "default",
    "experiment-defaults",
    "task",
    "variant",
    "cli",
]

ALLOWED_OVERRIDE_ROOTS: tuple[str, ...] = ("agent", "run_limits", "sandbox")

RootName = Literal["agent", "run_limits", "sandbox"]


class MergeError(ValueError):
    """Raised for an unknown ``-D`` path / nested key during the merge walk.

    Carries an optional ``suggestion`` (the closest known field name) so the CLI
    boundary can surface a "did you mean?" hint.
    """

    def __init__(self, message: str, *, suggestion: str | None = None) -> None:
        super().__init__(message)
        self.suggestion = suggestion


@dataclass(frozen=True)
class Layer:
    """One precedence layer fed to :func:`merge_layers`.

    ``patch`` is a partial nested dict (e.g. ``model_dump(exclude_unset=True)``
    or a ``-D``-built patch). ``detail`` is the ``source_detail`` recorded in
    lineage: a single string applied to every key this layer sets, OR a mapping
    of dotted path (relative to the root, e.g. ``"docker.network"``) to detail
    for per-key provenance (used by the ``.env`` layer). When ``detail`` is None
    and ``source == "cli"``, the detail auto-derives as ``"-D <root>.<path>"``.
    ``record_lineage=False`` (the layer-5 value seed) contributes values only and
    writes no lineage, so lower layers' provenance for untouched keys survives.
    """

    source: ConfigSource
    patch: Mapping[str, Any]
    detail: str | Mapping[str, str] | None = None
    record_lineage: bool = True


# ---------------------------------------------------------------------------
# Root model-type derivation (programmatic — no hardcoded union tuple)
# ---------------------------------------------------------------------------


def _root_model_types(root: str) -> tuple[type[BaseModel], ...]:
    """Concrete model types a root resolves to, derived from ``TaskDefinition``.

    For ``agent`` this is the ``AgentConfig`` union members plus ``BaseAgentConfig``
    (so subclass-only fields like ``sdk_options`` are recognized) — derived via
    :func:`classify_annotation` (the single annotation-walker for the feature),
    never a hardcoded tuple. Raises :class:`MergeError` for an unknown root.
    """
    if root not in ALLOWED_OVERRIDE_ROOTS:
        raise MergeError(f"unknown override root {root!r}; allowed roots: {', '.join(ALLOWED_OVERRIDE_ROOTS)}")
    annotation = TaskDefinition.model_fields[root].annotation
    # Reuse the shared walker; we only need its nested-model list here and
    # discard the free-form-dict flag.
    models, _is_free_form_dict = classify_annotation(annotation)
    if root == "agent" and BaseAgentConfig not in models:
        models.append(BaseAgentConfig)
    # Dedupe preserving order.
    seen: set[type[BaseModel]] = set()
    deduped = [m for m in models if not (m in seen or seen.add(m))]
    return tuple(deduped)


def _exclusion_groups(model_types: Sequence[type[BaseModel]]) -> tuple[tuple[str, ...], ...]:
    """Union of every member's ``_merge_exclusive_groups`` class attribute."""
    groups: list[tuple[str, ...]] = []
    for m in model_types:
        for g in getattr(m, "_merge_exclusive_groups", ()):
            if g not in groups:
                groups.append(tuple(g))
    return tuple(groups)


def _unknown_key_error(segment: str, model_types: Sequence[type[BaseModel]], context: str) -> MergeError:
    known = sorted({name for m in model_types for name in m.model_fields})
    suggestion = difflib.get_close_matches(segment, known, n=1)
    hint = f"; did you mean {suggestion[0]!r}?" if suggestion else ""
    return MergeError(
        f"unknown field {segment!r} under {context!r}{hint}", suggestion=suggestion[0] if suggestion else None
    )


# ---------------------------------------------------------------------------
# Value merge (recursive; honors per-field strategies; no lineage)
# ---------------------------------------------------------------------------


def _deep_dict_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive dict merge: nested dicts merge; every other value (incl. lists)
    replaces. Shares NO structure with either input — nested dicts are merged into
    fresh dicts and leaf values (lists/scalars/etc.) are deep-copied — so the result
    can be mutated without leaking back into a patch reused across rows/layers
    (e.g. a variant's ``sdk_options`` dict reused for every dataset row)."""
    result: dict[str, Any] = {
        k: (_deep_dict_merge(v, {}) if isinstance(v, dict) else copy.deepcopy(v)) for k, v in base.items()
    }
    for key, value in patch.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_dict_merge(existing, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _merge_value(
    strategy: MergeStrategy,
    annotation: Any,
    key: str,
    base_val: Any,
    new_val: Any,
    context: str,
    append_order: AppendOrder = "forward",
) -> Any:
    """Merge one field's value per its ``strategy``. Validates nested keys (for
    deep-model fields). Never mutates inputs. Emits no lineage."""
    if strategy == "append":
        if new_val is None:
            # An Optional append-list left unset at this layer (e.g. a full-dump
            # seed dumping ``template_sources: None``) contributes nothing.
            return base_val
        if not isinstance(new_val, list):
            raise MergeError(
                f"expected a list for append-strategy field {context}.{key!r}, got {type(new_val).__name__}"
            )
        existing = base_val if isinstance(base_val, list) else []
        # ``reverse`` puts this (higher) layer's items first — e.g. post_run, where
        # the task's commands precede experiment-defaults cleanup.
        if append_order == "reverse":
            return list(new_val) + list(existing)
        return list(existing) + list(new_val)

    if strategy == "deep":
        child_models, _is_free_form_dict = classify_annotation(annotation)
        if child_models:
            if not isinstance(new_val, dict):
                return new_val  # wholesale replace of a nested model with a non-dict (unusual)
            base_dict = base_val if isinstance(base_val, dict) else {}
            return _merge_dict_by_model(base_dict, new_val, tuple(child_models), f"{context}.{key}")
        # free-form dict (or an explicit ``deep`` on a plain dict)
        if isinstance(new_val, dict):
            base_dict = base_val if isinstance(base_val, dict) else {}
            return _deep_dict_merge(base_dict, new_val)
        return new_val  # non-dict (e.g. claude_settings as a str / None) replaces

    return new_val  # replace


def _merge_dict_by_model(
    base: Mapping[str, Any],
    patch: Mapping[str, Any],
    model_types: tuple[type[BaseModel], ...],
    context: str,
) -> dict[str, Any]:
    """Field-by-field merge of two nested-model dicts honoring each field's
    strategy. Validates every patch key against ``model_types``."""
    result: dict[str, Any] = dict(base)
    for key, value in patch.items():
        matching = [m for m in model_types if key in m.model_fields]
        if not matching:
            raise _unknown_key_error(key, model_types, context)
        field_info = matching[0].model_fields[key]
        strategy = merge_strategy_of(field_info)
        result[key] = _merge_value(
            strategy, field_info.annotation, key, result.get(key), value, context, append_order_of(field_info)
        )
    return result


# ---------------------------------------------------------------------------
# Lineage emission
# ---------------------------------------------------------------------------


def _detail_for(layer: Layer, full_path: str) -> str | None:
    """Resolve the ``source_detail`` for a dotted path under this layer."""
    d = layer.detail
    if isinstance(d, Mapping):
        mapped = d.get(full_path)
        if mapped is not None:
            return mapped
    elif d is not None:
        return d
    if layer.source == "cli":
        return f"-D {full_path}"
    return None


def _record_lineage(
    lineage: dict[str, ConfigLineageEntry],
    root: str,
    key: str,
    strategy: MergeStrategy,
    annotation: Any,
    layer_value: Any,
    merged_value: Any,
    layer: Layer,
) -> None:
    """Write dotted lineage for one field a layer just set.

    Granularity: ``replace``/``append`` -> one entry at ``root.key``; ``deep-dict``
    -> one entry per top-level sub-key the layer set (parity with the old sdk
    handling); ``deep-model`` -> one coarse entry at ``root.key`` (the
    highest-precedence layer that touched the subtree)."""
    if strategy == "deep":
        child_models, _is_free_form_dict = classify_annotation(annotation)
        if not child_models and isinstance(layer_value, dict):
            # deep-dict: per-top-level-leaf provenance.
            for subkey, subval in layer_value.items():
                path = f"{key}.{subkey}"
                lineage[f"{root}.{path}"] = ConfigLineageEntry(
                    value=subval, source=layer.source, source_detail=_detail_for(layer, f"{root}.{path}")
                )
            return
        # deep-model (or non-dict deep value): one coarse entry at the field path.
        lineage[f"{root}.{key}"] = ConfigLineageEntry(
            value=merged_value, source=layer.source, source_detail=_detail_for(layer, f"{root}.{key}")
        )
        return
    # replace -> the layer's own value; append -> the merged list.
    recorded = merged_value if strategy == "append" else layer_value
    lineage[f"{root}.{key}"] = ConfigLineageEntry(
        value=recorded, source=layer.source, source_detail=_detail_for(layer, f"{root}.{key}")
    )


# ---------------------------------------------------------------------------
# The generic resolver
# ---------------------------------------------------------------------------


def merge_layers(
    model_types: tuple[type[BaseModel], ...],
    layers: Sequence[Layer],
    *,
    lineage_root: str,
    lineage: dict[str, ConfigLineageEntry] | None = None,
) -> dict[str, Any]:
    """Walk ``model_fields``, apply each field's strategy across ``layers`` in
    order, and (when ``lineage`` is given) record dotted lineage as a side
    effect. Returns the merged nested dict for the root's constructor — does NOT
    construct (the caller reconstructs so Pydantic re-validates values).

    Raises :class:`MergeError` on an unknown key (with a did-you-mean suggestion).
    Inputs are never mutated.
    """
    accum: dict[str, Any] = {}
    groups = _exclusion_groups(model_types)
    for layer in layers:
        # Exclusion groups: when a layer sets any group member, drop the OTHER
        # members (and their lineage) before writing — so a record never
        # advertises a sibling the resolved model no longer carries.
        for group in groups:
            set_members = {m for m in group if layer.patch.get(m) is not None}
            if set_members:
                for other in group:
                    if other not in set_members:
                        accum.pop(other, None)
                        # A lineage-silent seed (record_lineage=False) clears values
                        # but must not touch lineage — the layers-1-4 provenance is
                        # authoritative for fields it didn't introduce.
                        if lineage is not None and layer.record_lineage:
                            # Drop the coarse entry AND any per-leaf entries (a deep
                            # field's lineage lives under `root.other.<subkey>`), so a
                            # record never advertises a cleared sibling.
                            prefix = f"{lineage_root}.{other}"
                            for k in [k for k in lineage if k == prefix or k.startswith(prefix + ".")]:
                                lineage.pop(k, None)
        for key, value in layer.patch.items():
            matching = [m for m in model_types if key in m.model_fields]
            if not matching:
                raise _unknown_key_error(key, model_types, lineage_root)
            field_info = matching[0].model_fields[key]
            strategy = merge_strategy_of(field_info)
            accum[key] = _merge_value(
                strategy, field_info.annotation, key, accum.get(key), value, lineage_root, append_order_of(field_info)
            )
            if lineage is not None and layer.record_lineage:
                _record_lineage(lineage, lineage_root, key, strategy, field_info.annotation, value, accum[key], layer)
    return accum


@overload
def resolve_root(
    root: Literal["agent"], layers: Sequence[Layer], *, lineage: dict[str, ConfigLineageEntry] | None = ...
) -> ClaudeCodeAgentConfig | CodexAgentConfig | BaseAgentConfig | None:
    """Resolve the ``agent`` root to its concrete agent-config model."""


@overload
def resolve_root(
    root: Literal["run_limits"], layers: Sequence[Layer], *, lineage: dict[str, ConfigLineageEntry] | None = ...
) -> RunLimits | None:
    """Resolve the ``run_limits`` root (None when no layer set anything)."""


@overload
def resolve_root(
    root: Literal["sandbox"], layers: Sequence[Layer], *, lineage: dict[str, ConfigLineageEntry] | None = ...
) -> SandboxConfig | None:
    """Resolve the ``sandbox`` root (None when no layer set anything)."""


def resolve_root(
    root: RootName,
    layers: Sequence[Layer],
    *,
    lineage: dict[str, ConfigLineageEntry] | None = None,
) -> BaseModel | None:
    """:func:`merge_layers` + reconstruct via the root's constructor.

    The single merge-and-build entry point both resolution paths call. Value
    validation (``extra="forbid"``, field validators, SandboxConfig's
    template-sources model_validator) happens in the constructor. Returns None
    when no layer set anything for ``run_limits`` (an empty block is dropped)."""
    model_types = _root_model_types(root)  # raises MergeError for an unknown root
    merged = merge_layers(model_types, layers, lineage_root=root, lineage=lineage)
    if root == "agent":
        return parse_agent_config(**merged)
    if root == "run_limits":
        return RunLimits(**merged) if merged else None
    # root == "sandbox" — the only remaining RootName member.
    return SandboxConfig(**merged) if merged else None


# ---------------------------------------------------------------------------
# Path validation (shared with the layer-5 ``-D`` engine)
# ---------------------------------------------------------------------------


def validate_paths(paths: Sequence[str]) -> None:
    """Validate dotted ``-D`` paths against the root schemas (no values).

    Walks each path segment-by-segment exactly as :func:`merge_layers` looks up
    fields: unknown root / field -> :class:`MergeError` (with did-you-mean);
    stops (accepting everything below) at a free-form ``dict`` boundary; rejects
    extra segments past a scalar/list leaf.
    """
    for path in paths:
        _validate_path(path)


def _validate_path(path: str) -> None:
    segments = path.split(".")
    root = segments[0]
    model_types = _root_model_types(root)  # raises MergeError on unknown root
    rest = segments[1:]
    if not rest:
        raise MergeError(f"override path {path!r} must target a field under {root!r}, not the root itself")
    context = root
    for i, segment in enumerate(rest):
        matching = [m for m in model_types if segment in m.model_fields]
        if not matching:
            raise _unknown_key_error(segment, model_types, context)
        child_models: list[type[BaseModel]] = []
        is_free_form_dict = False
        for m in matching:
            sub_models, sub_dict = classify_annotation(m.model_fields[segment].annotation)
            child_models.extend(sub_models)
            is_free_form_dict = is_free_form_dict or sub_dict
        if is_free_form_dict:
            return  # free-form dict boundary — accept everything below
        if i == len(rest) - 1:
            return  # valid scalar / list / model leaf
        if not child_models:
            raise MergeError(f"cannot descend into {segment!r} under {context!r}: it is a scalar or list leaf")
        model_types = tuple(child_models)
        context = f"{context}.{segment}"
