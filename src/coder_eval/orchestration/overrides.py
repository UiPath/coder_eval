"""Layer-5 ``-D path=value`` / ``--set`` task-config override engine.

The thin layer-5 wrapper over the generic resolver (:mod:`config_merge`). It

  1. parses a scalar value (YAML-safe, with the YAML-1.1 truthy-alias guard),
  2. groups dotted ``path=value`` overrides into one nested ``-D`` patch per
     root (``agent`` / ``run_limits`` / ``sandbox``),
  3. calls :func:`config_merge.resolve_root` with a lineage-silent value seed
     (the already-resolved model) + the ``-D`` patch layer, so a field merges
     under exactly the same strategy at layer 5 as at the variant layer.

Path *validation* (did-you-mean on typos) lives in
:func:`config_merge.validate_paths`; value validation happens in the root
constructors during reconstruction.

Deliberately CLI-free (no ``typer`` import — lint rule CE004). The CLI boundary
wraps :class:`OverrideError` / :class:`config_merge.MergeError` into
``typer.BadParameter``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import ValidationError

from ..models import AgentKind, ConfigLineageEntry, TaskDefinition
from .config_merge import ALLOWED_OVERRIDE_ROOTS, Layer, MergeError, RootName, resolve_root


# YAML 1.1 truthy aliases PyYAML coerces to bool. We keep them as strings so
# e.g. ``-D agent.model=on`` doesn't silently become ``True``. This is the single
# scalar-parsing implementation for all ``-D``/``--set`` values.
_YAML_TRUTHY_ALIASES = frozenset({"y", "n", "yes", "no", "on", "off"})


class OverrideError(ValueError):
    """Raised for malformed or schema-invalid ``-D`` overrides."""


def parse_scalar(value: str) -> Any:
    """YAML-safe-load a scalar, keeping YAML-1.1 truthy aliases as strings.

    ``on``/``off``/``yes``/``no``/``y``/``n`` (case-insensitive) stay strings to
    avoid the foot-gun where a string-valued field colliding with a YAML keyword
    is silently turned into a bool. Explicit ``true``/``false`` (YAML 1.2
    canonical) coerce normally. Raises :class:`OverrideError` on invalid YAML.
    """
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError as e:
        raise OverrideError(f"value is not valid YAML: {e}") from e
    if isinstance(parsed, bool) and value.strip().lower() in _YAML_TRUTHY_ALIASES:
        parsed = value
    return parsed


def parse_override(raw: str) -> tuple[str, Any]:
    """Split ``path=value`` on the FIRST ``=`` into ``(path, parsed_value)``.

    Raises :class:`OverrideError` when ``=`` is absent or the path is empty.
    The value is coerced via :func:`parse_scalar`.
    """
    if "=" not in raw:
        raise OverrideError(f"override must be PATH=VALUE, got: {raw!r}")
    path, _, value = raw.partition("=")
    path = path.strip()
    if not path:
        raise OverrideError(f"override path cannot be empty: {raw!r}")
    return path, parse_scalar(value)


def _assign_nested(patch: dict[str, Any], segments: list[str], value: Any) -> None:
    """Set ``patch[seg0][seg1]... = value``, creating intermediate dicts.

    Merges multiple ``-D`` entries that share a prefix (e.g.
    ``agent.sdk_options.effort`` and ``agent.sdk_options.max_thinking_tokens``)
    into one nested patch.
    """
    cursor = patch
    for seg in segments[:-1]:
        nxt = cursor.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[seg] = nxt
        cursor = nxt
    cursor[segments[-1]] = value


def apply_overrides(
    task: TaskDefinition,
    overrides: Mapping[str, Any],
    *,
    agent_type: str | None = None,
    lineage: dict[str, ConfigLineageEntry] | None = None,
    detail: Mapping[str, str] | None = None,
) -> None:
    """Apply layer-5 overrides onto a resolved ``TaskDefinition`` in place.

    Groups ``overrides`` (dotted ``path=value``) into one nested ``-D`` patch per
    root, then resolves each affected root via :func:`config_merge.resolve_root`
    with a **lineage-silent** value seed (the already-resolved model) so the
    layers-1-4 provenance for fields ``-D`` doesn't touch is preserved. Each
    ``-D`` field obeys its declared merge strategy (append fields append, etc.) —
    the same rule the variant layer uses.

    ``agent_type`` (from ``--type``) is injected into the agent patch's ``type``
    via ``setdefault`` (an explicit ``-D agent.type`` wins). ``detail`` maps a
    full dotted path to a ``source_detail`` string (used for ``.env`` provenance);
    paths absent from it auto-derive ``"-D <path>"``. Unknown roots/keys raise
    :class:`OverrideError` (the latter re-raised from
    :class:`config_merge.MergeError`).
    """
    agent_patch: dict[str, Any] = {}
    rl_patch: dict[str, Any] = {}
    sandbox_patch: dict[str, Any] = {}
    by_root = {"agent": agent_patch, "run_limits": rl_patch, "sandbox": sandbox_patch}

    for path, value in overrides.items():
        root, _, rest = path.partition(".")
        if root not in by_root:
            raise OverrideError(f"unknown override root {root!r}; allowed roots: {', '.join(ALLOWED_OVERRIDE_ROOTS)}")
        _assign_nested(by_root[root], rest.split("."), value)

    agent_detail: Mapping[str, str] | None = detail
    if agent_type is not None and "type" not in agent_patch:
        # `--type` injection (no explicit `-D agent.type`): record its provenance
        # as "--type". An explicit `-D agent.type` keeps its own "-D" detail.
        agent_patch["type"] = agent_type
        agent_detail = {**(detail or {}), "agent.type": "--type"}

    if agent_patch:
        assert task.agent is not None, f"Task '{task.task_id}' has no agent config"
        # Preserve the friendly "sdk_options only for claude-code" message before
        # reconstruction, keyed on the type the agent is *becoming*.
        if "sdk_options" in agent_patch:
            becoming = agent_patch.get("type", task.agent.type)
            type_value = becoming.value if isinstance(becoming, AgentKind) else becoming
            if type_value != AgentKind.CLAUDE_CODE.value:
                where = "no agent type is set" if type_value is None else f"agent type {type_value}"
                raise OverrideError(
                    f"sdk_options cannot be used with {where}. This option is only supported for claude-code agents."
                )
        # Seed with only the explicitly-set fields (exclude_unset) so switching the
        # agent subclass via --type doesn't drag subclass-only defaults into a model
        # that forbids them.
        task.agent = _resolve(
            "agent", task.agent.model_dump(exclude_unset=True), agent_patch, detail=agent_detail, lineage=lineage
        )

    if rl_patch:
        seed = task.run_limits.model_dump(exclude_unset=True) if task.run_limits else {}
        task.run_limits = _resolve("run_limits", seed, rl_patch, detail=detail, lineage=lineage)

    if sandbox_patch:
        # Full dump: the resolved sandbox already folded layers 1-4 (incl. the
        # append-semantics env_passthrough_extra/template_sources merges), so the
        # full dump is the authoritative value base to patch onto.
        resolved = _resolve("sandbox", task.sandbox.model_dump(), sandbox_patch, detail=detail, lineage=lineage)
        assert resolved is not None  # sandbox seed is always non-empty
        task.sandbox = resolved


def _resolve(
    root: RootName,
    seed_patch: dict[str, Any],
    cli_patch: dict[str, Any],
    *,
    detail: Mapping[str, str] | None,
    lineage: dict[str, ConfigLineageEntry] | None,
) -> Any:
    """resolve_root over a lineage-silent seed + the recording ``-D`` layer.

    Re-raises both failure modes as :class:`OverrideError` so the core stays
    typer-free and both read alike at the CLI: a :class:`config_merge.MergeError`
    (unknown ``-D`` key, with a did-you-mean) and a Pydantic
    :class:`ValidationError` (a bad ``-D`` *value*, condensed to a path-prefixed
    line instead of the raw multi-line dump + ``errors.pydantic.dev`` URL).
    """
    layers = [
        Layer(source="cli", patch=seed_patch, record_lineage=False),
        Layer(source="cli", patch=cli_patch, detail=detail),
    ]
    try:
        return resolve_root(root, layers, lineage=lineage)
    except MergeError as e:
        raise OverrideError(str(e)) from e
    except ValidationError as e:
        raise OverrideError(_format_validation_error(root, e)) from e


def _format_validation_error(root: RootName, exc: ValidationError) -> str:
    """Condense a Pydantic ``ValidationError`` into one path-prefixed ``-D`` line
    per error, dropping the verbose type tags and ``errors.pydantic.dev`` URL so a
    bad value reads like the (already-clean) unknown-key errors."""
    parts: list[str] = []
    for err in exc.errors():
        leaf = ".".join(str(p) for p in err["loc"])
        path = f"{root}.{leaf}" if leaf else root
        msg = err.get("msg", "invalid value")
        if "input" in err:
            parts.append(f"-D {path}: {msg} (got {err['input']!r})")
        else:
            parts.append(f"-D {path}: {msg}")
    return "; ".join(parts) if parts else f"invalid -D override for {root!r}: {exc}"
