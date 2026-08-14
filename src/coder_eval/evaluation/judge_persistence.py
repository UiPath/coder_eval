"""Sibling-file persistence for ``JudgeCriterionResult.transcript``.

The full judge transcript (tool calls, raw verdict, rendered prompt and
system prompt) can run 10-100 KB. Inlining it into every ``task.json``
inflates the row record for consumers (suite rollups, report renderers)
that don't need it. Spilling each transcript to a sibling YAML file next to
``task.json`` keeps the row record lean and lets reviewers grep transcripts
independently.

YAML (over JSON) for the sibling: the transcript carries multi-line text
(``judge_prompt``, ``judge_system_prompt``, ``raw_verdict``) which YAML's
literal block scalar (``|``) renders as readable paragraphs instead of
single-line strings with ``\\n`` escapes. Sibling consumers are humans;
YAML wins.

Two functions:

- ``spill_judge_transcripts``: called by the orchestrator just before it
  writes ``task.json``. For each judge result with an inline ``transcript``,
  writes a sibling file and sets ``transcript_path`` on the result. The
  inline ``transcript`` is left in place so HTML rendering against the
  in-memory ``EvaluationResult`` still sees it; ``model_dump_json``
  callers strip it via ``exclude={...}``.

- ``load_judge_transcripts``: called by re-render paths
  (``coder-eval report``, stats loaders) after
  ``EvaluationResult.model_validate_json``. For each result whose
  ``transcript_path`` points at an existing sibling file, reads it back
  and attaches it as a dict on ``transcript`` so renderers see the same
  shape they get during the original run. Accepts both ``.yaml`` (the
  current format) and ``.json`` (the previous format) so previously-spilled
  runs keep rendering.

Backward compatibility: old ``task.json`` files with inline ``transcript``
keep working — the loader treats ``transcript_path is None`` as a no-op.
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


# Windows reserved device basenames. The Win32 API maps these to character
# devices regardless of the directory they sit in — opening ``CON`` or ``NUL.yaml``
# inside ``task_dir`` resolves to the console or the null device, not a file.
# Case-insensitive; the trailing extension (if any) is ignored by Win32 too,
# so ``con``, ``CON``, ``CON.yaml``, ``nul.txt`` all map to devices.
# Only COM1-9 / LPT1-9 are device names; COM10 and beyond are regular files.
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)


# Field order in the YAML output: lead with the human-readable summary fields
# (durations, token counts, prompts), then the long body fields. Verdict-shape
# (raw_verdict) goes last because it's the bulkiest.
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
    # ORDER IS LOAD-BEARING. Each filename is keyed off the criterion's
    # position in its result list; ``load_judge_transcripts`` reads the stored
    # path, so each list must retain its order through persistence.
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
        # Skip results that already have an inline transcript — typical for
        # the orchestrator's own first HTML render, which runs against the
        # in-memory result before it gets dumped + reloaded.
        if getattr(cr, "transcript", None):
            continue
        # SECURITY: transcript_path comes from task.json, which may travel across
        # trust boundaries (CI artifacts, shared eval bundles). spill_judge_transcripts
        # only ever writes generated basename-only paths, with no separators or
        # ``..``. Allowlist that shape directly so a tampered
        # ``transcript_path: '/etc/passwd'`` or ``../../secrets`` is refused at the
        # door rather than relying on ``is_relative_to`` to catch it after a join.
        # Check BOTH PurePosixPath (forward-slash separator) AND PureWindowsPath
        # (forward and back-slash separators, drive-letter prefixes): a path like
        # ``subdir\judge-0.yaml`` passes the POSIX check on Linux (backslash is a
        # regular char) but resolves to a nested file on Windows. Rejecting under
        # either interpretation enforces the basename-only policy regardless of
        # which platform the task.json travels to next.
        if PurePosixPath(path).name != path or PureWindowsPath(path).name != path:
            logger.warning("Refusing to load judge transcript with non-basename path: %s", path)
            continue
        # Reject Windows reserved device basenames. On Windows, ``CON.yaml`` /
        # ``NUL`` / ``COM1`` open the console / null device / serial port
        # regardless of where they sit in the directory tree. The check is
        # platform-independent so a task.json minted on Linux that ships such a
        # transcript_path is rejected before it travels to Windows.
        stem_upper = path.split(".", 1)[0].upper()
        if stem_upper in _WINDOWS_RESERVED_BASENAMES:
            logger.warning("Refusing to load judge transcript with reserved Windows device name: %s", path)
            continue
        sibling = task_dir / path
        # Defense-in-depth: even with the basename guard above, resolve and verify
        # containment before reading — symlinks inside ``task_dir`` could redirect
        # outside it (a malicious bundle could ship one).
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
            # Parse as JSON for legacy ``.json`` siblings; anything else (today's
            # ``.yaml`` and any future format yaml.safe_load handles) goes through
            # PyYAML, which also accepts JSON as a subset.
            data = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
        except Exception as e:
            logger.warning("Failed to read judge transcript %s: %s", sibling, e)
            continue
        if not isinstance(data, dict):
            # A scalar / list / None payload would silently land on the result and
            # crash the HTML renderer (which assumes dict-or-typed) with an
            # AttributeError on the first ``.get()``. Reject early.
            logger.warning(
                "Judge transcript %s is %s, expected mapping — skipping",
                sibling,
                type(data).__name__,
            )
            continue
        # Prefer typed JudgeTranscript so renderer / aggregator code that does
        # isinstance checks sees the same shape it gets during the original run.
        # Fall back to the raw dict on ValidationError — older spilled siblings
        # (pre-schema-change) or forward-compat keys shouldn't break re-render.
        attached: JudgeTranscript | dict[str, Any]
        try:
            attached = JudgeTranscript.model_validate(data)
        except ValidationError as e:
            logger.debug("Judge transcript %s did not match JudgeTranscript schema, attaching as dict: %s", sibling, e)
            attached = data
        # Use object.__setattr__ so we don't go through pydantic's setter,
        # which (depending on model_config of the loaded subclass) might
        # validate or reject. The HTML renderer accepts both typed
        # JudgeTranscript and dict-shape so either shape works downstream.
        # NOTE: With the ``CriterionResultUnion`` discriminator on both
        # ``EvaluationResult`` criterion-result lists, ``cr`` is now a
        # properly-typed ``JudgeCriterionResult`` after reload (not a base
        # ``CriterionResult`` with the field in ``__pydantic_extra__``), so
        # the assignment lands on the declared field directly.
        try:
            object.__setattr__(cr, "transcript", attached)
        except Exception as e:
            logger.warning("Failed to attach judge transcript onto %s: %s", type(cr).__name__, e)
            continue
        loaded += 1
    if loaded:
        logger.debug("loaded %d judge transcript(s) from %s", loaded, task_dir)
    return loaded
