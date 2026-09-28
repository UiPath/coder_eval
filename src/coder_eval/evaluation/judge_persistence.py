"""Sibling-file persistence for ``JudgeCriterionResult.transcript``.

A judge transcript runs 10-100 KB, so it spills to a sibling YAML file next to
``task.json`` and the row keeps only a ``transcript_path``. The inline value is
left in place so in-memory HTML rendering still sees it; ``model_dump_json``
callers strip it via ``exclude={...}``.

- ``spill_judge_transcripts``: called by the orchestrator just before it writes
  ``task.json``.
- ``load_judge_transcripts``: called by re-render paths after
  ``EvaluationResult.model_validate_json``. Accepts both ``.yaml`` (current) and
  ``.json`` (previous), and treats ``transcript_path is None`` as a no-op, so old
  inline records keep working.

Rationale: .claude/notes/persistence.md § Judge persistence
"""

from __future__ import annotations

import json
import logging
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from coder_eval.models import JudgeCriterionResult, JudgeTranscript


if TYPE_CHECKING:
    from pathlib import Path

    from coder_eval.models import EvaluationResult


logger = logging.getLogger(__name__)


TASK_JSON_TRANSCRIPT_EXCLUDE = {
    "success_criteria_results": {"__all__": {"transcript"}},
    "post_failure_criteria_results": {"__all__": {"transcript"}},
}


# Win32 maps these to character devices wherever they sit, extension ignored, so
# ``con``, ``CON.yaml`` and ``nul.txt`` all open a device. Only COM1-9 / LPT1-9 are
# device names. Rationale: .claude/notes/persistence.md § transcript_path is untrusted input
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)


# Human-readable summary fields first, bulkiest (raw_verdict) last.
_TRANSCRIPT_FIELD_ORDER = (
    "duration_seconds",
    "truncated",
    "token_usage",
    "tool_calls",
    "judge_system_prompt",
    "judge_prompt",
    "raw_verdict",
)


class _BlockLiteralDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings as literal block scalars.

    Without the override, pyyaml dumps multi-line strings as quoted single-line
    strings with ``\\n`` escapes — unreadable for the rendered prompts and raw
    verdicts that are the whole point of this file. The override flips strings
    containing newlines to use the ``|`` style (literal block scalar) so they
    render as native indented paragraphs.
    """


def _str_presenter(dumper: _BlockLiteralDumper, data: str) -> Any:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockLiteralDumper.add_representer(str, _str_presenter)


def _ordered_transcript_dict(transcript_dump: dict[str, Any]) -> dict[str, Any]:
    """Return ``transcript_dump`` with keys in the human-friendly order above.

    Unknown keys (forward-compat) are appended after the known ones in their
    original insertion order.
    """
    ordered: dict[str, Any] = {}
    for key in _TRANSCRIPT_FIELD_ORDER:
        if key in transcript_dump:
            ordered[key] = transcript_dump[key]
    for key, value in transcript_dump.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def spill_judge_transcripts(result: EvaluationResult, output_dir: Path) -> int:
    """Write each judge result's inline transcript to a sibling YAML file.

    For each ``JudgeCriterionResult`` in the canonical or post-failure result
    list that carries a non-None ``transcript``, writes a distinct sibling YAML
    file in ``output_dir`` (creating the directory if needed) and sets
    ``transcript_path`` on the result to the sibling filename.

    The inline ``transcript`` is **left in place** so in-memory consumers
    (HTML rendering at the end of the orchestrator run) still see it.
    Callers writing ``task.json`` should exclude ``transcript`` from both result
    lists so the on-disk record carries only the path.

    Returns the count of transcripts spilled (informational; no-op when 0).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    spilled = 0
    # ORDER IS LOAD-BEARING: each filename is keyed off the criterion's position in
    # its result list, so each list must retain its order through persistence.
    result_groups = (
        ("judge", result.success_criteria_results),
        ("post-failure-judge", result.post_failure_criteria_results),
    )
    for prefix, criteria_results in result_groups:
        for idx, cr in enumerate(criteria_results):
            if not isinstance(cr, JudgeCriterionResult):
                continue
            if cr.transcript is None:
                continue
            sibling_name = f"{prefix}-{idx}.yaml"
            sibling_path = output_dir / sibling_name
            ordered = _ordered_transcript_dict(cr.transcript.model_dump())
            sibling_path.write_text(
                yaml.dump(
                    ordered,
                    Dumper=_BlockLiteralDumper,
                    sort_keys=False,
                    allow_unicode=True,
                    width=100,
                ),
                encoding="utf-8",
            )
            cr.transcript_path = sibling_name
            spilled += 1
    if spilled:
        logger.debug("spilled %d judge transcript(s) to %s", spilled, output_dir)
    return spilled


def load_judge_transcripts(result: EvaluationResult, task_dir: Path) -> int:
    """Read sibling judge transcript files and attach them to each result.

    For each criterion result in either result list that has a
    ``transcript_path`` set (and no inline ``transcript`` — already-loaded
    results are left alone), reads the sibling file relative to ``task_dir``
    and attaches the parsed dict on ``transcript`` so HTML / markdown renderers
    see the same shape they get during the original run.

    Missing sibling files are skipped silently and logged at debug level —
    runs predating this feature have no sibling files and render fine via
    their inline transcript (preserved through ``CriterionResult``'s
    ``extra="allow"`` round-trip).

    Returns the count of transcripts loaded.
    """
    loaded = 0
    criterion_results = result.success_criteria_results + result.post_failure_criteria_results
    for cr in criterion_results:
        path = getattr(cr, "transcript_path", None)
        if not path:
            continue
        # Already inline -- typical for the orchestrator's own first HTML render,
        # which runs against the in-memory result.
        if getattr(cr, "transcript", None):
            continue
        # SECURITY: transcript_path comes from task.json, which travels across trust
        # boundaries. The writer only ever emits a generated basename, so ALLOWLIST
        # that shape -- under BOTH POSIX and Windows semantics, since
        # ``subdir\judge-0.yaml`` passes a POSIX check and nests on Windows.
        # Rationale: .claude/notes/persistence.md § transcript_path is untrusted input
        if path in {".", ".."} or PurePosixPath(path).name != path or PureWindowsPath(path).name != path:
            logger.warning("Refusing to load judge transcript with non-basename path: %s", path)
            continue
        # Platform-INDEPENDENT, so a task.json minted on Linux carrying such a path
        # is rejected before it travels to Windows.
        stem_upper = path.split(".", 1)[0].upper()
        if stem_upper in _WINDOWS_RESERVED_BASENAMES:
            logger.warning("Refusing to load judge transcript with reserved Windows device name: %s", path)
            continue
        sibling = task_dir / path
        # Defense-in-depth: a symlink inside ``task_dir`` could still redirect
        # outside it, so verify containment after resolving.
        try:
            resolved_sibling = sibling.resolve()
            resolved_root = task_dir.resolve()
        except OSError as e:
            logger.warning("Failed to resolve judge transcript path %s: %s", sibling, e)
            continue
        if not resolved_sibling.is_relative_to(resolved_root):
            logger.warning(
                "Refusing to read judge transcript outside task dir: path=%s resolved=%s task_dir=%s",
                path,
                resolved_sibling,
                resolved_root,
            )
            continue
        if not resolved_sibling.is_file():
            logger.debug("judge transcript path %s set but %s missing — skipping", path, resolved_sibling)
            continue
        sibling = resolved_sibling
        try:
            text = sibling.read_text(encoding="utf-8")
            # JSON for legacy siblings; everything else through PyYAML, which
            # accepts JSON as a subset anyway.
            data = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
        except Exception as e:
            logger.warning("Failed to read judge transcript %s: %s", sibling, e)
            continue
        if not isinstance(data, dict):
            # A scalar / list / None payload would land on the result and crash the
            # renderer on its first ``.get()``.
            logger.warning(
                "Judge transcript %s is %s, expected mapping — skipping",
                sibling,
                type(data).__name__,
            )
            continue
        # Typed, so isinstance checks see the same shape as during the original
        # run; the raw-dict fallback keeps an older sibling rendering.
        attached: JudgeTranscript | dict[str, Any]
        try:
            attached = JudgeTranscript.model_validate(data)
        except ValidationError as e:
            logger.debug("Judge transcript %s did not match JudgeTranscript schema, attaching as dict: %s", sibling, e)
            attached = data
        # Bypasses pydantic's setter, which a loaded subclass's config might
        # validate or reject. The renderer accepts both shapes.
        try:
            object.__setattr__(cr, "transcript", attached)
        except Exception as e:
            logger.warning("Failed to attach judge transcript onto %s: %s", type(cr).__name__, e)
            continue
        loaded += 1
    if loaded:
        logger.debug("loaded %d judge transcript(s) from %s", loaded, task_dir)
    return loaded
