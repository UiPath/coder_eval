"""Shared JSON-verdict parser for judge-style criteria.

Walks all top-level ``{...}`` spans in the model's response and returns the last
one that validates as a ``JudgeVerdict``. Defeats two model pathologies at once:
markdown/prose preamble before the verdict, and trailing acknowledgment dicts
(e.g. ``{"ack": true}``) that lack a ``score`` key.
"""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import ValidationError

from coder_eval.models import JudgeVerdict


def _iter_top_level_object_spans(text: str) -> list[str]:
    """Return every top-level ``{...}`` substring where brace depth returns to 0.

    Tracks string-literal state with proper escape handling so braces inside
    quoted strings don't affect depth, and escaped quotes (``\\"``) don't
    prematurely close the string.
    """
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : i + 1])
                start = -1
    return spans


def parse_judge_verdict(content: str) -> tuple[JudgeVerdict | None, str | None]:
    """Locate the LAST span in ``content`` that validates as a ``JudgeVerdict``.

    Returns ``(verdict, None)`` on success or ``(None, error_message)`` on failure.

    When no span validates, surfaces a score-specific error message (missing,
    non-numeric, non-finite) from the last verdict-shaped span so callers see
    the intent-revealing diagnostic rather than a generic "no valid verdict".

    LAST-span contract — DO NOT flip to pick-first without considering
    prompt-injection. The judge system prompts instruct the model that "the
    final message is the verdict, nothing else"; picking the last span aligns
    with that contract and tolerates models that prepend a markdown preamble
    or include an echoed JSON literal in their reasoning. Three mitigations
    keep the LAST-span rule from being weaponised by an adversarial agent
    output that the judge has been asked to grade:

    1. The judge system prompt explicitly tells the model that its FINAL
       message is the verdict (see ``_SYSTEM_PROMPT`` in both
       ``criteria/agent_judge.py`` and ``criteria/llm_judge.py``).
    2. Directory-form references are mounted on the filesystem, NOT inlined
       into the prompt — the agent under review can't smuggle a fake "later"
       verdict by writing JSON into the reference solution.
    3. ``scrub_reference`` blanks any whole-string match of the reference
       inside the model's response BEFORE parsing, so a verbatim copy of a
       reference-embedded fake verdict is destroyed before it gets here.

    A future maintainer who flips to first-valid would defeat (1) without
    addressing the prompt-injection vector that the LAST rule sidesteps:
    a leading ``{"score": 1.0}`` smuggled into agent output that the judge
    is asked to grade would suddenly become the verdict instead of the
    judge's own final message.
    """
    stripped = content.strip()
    if not stripped:
        return None, "Failed to parse JSON verdict: empty response"

    last_verdict: JudgeVerdict | None = None
    last_verdict_shaped_error: str | None = None
    for span in _iter_top_level_object_spans(stripped):
        # Parse with stdlib json first so legacy-permissive literals (NaN,
        # Infinity, -Infinity) survive as floats and hit JudgeVerdict's
        # non-finite check — pydantic's strict JSON parser would reject them
        # upstream with a generic "json_invalid" error instead.
        try:
            data = json.loads(span)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            last_verdict = JudgeVerdict.model_validate(data)
        except ValidationError:
            # Only surface errors for verdict-shaped dicts (i.e. ones that actually
            # have a score key — rejects trailing acks like {"ack": true} silently).
            # Tracks the LAST such error: on multi-span input we want the diagnostic
            # from the most recent verdict attempt, mirroring the pick-last-valid rule.
            err = _verdict_shaped_error(data)
            if err is not None:
                last_verdict_shaped_error = err

    if last_verdict is not None:
        return last_verdict, None
    if last_verdict_shaped_error is not None:
        return None, last_verdict_shaped_error
    return None, "Failed to parse JSON verdict: no valid verdict in response"


def _verdict_shaped_error(data: dict[str, Any]) -> str | None:
    """Classify a failed validation on ``data`` into a user-facing diagnostic.

    Input-driven (not pydantic-internal) so the legacy error vocabulary is
    preserved without string-matching pydantic's ``msg`` field:
      * score missing     -> "score field missing in judge verdict"
      * score not numeric -> "score field is not a number: <repr>"
      * score not finite  -> "score field is not a finite number: <repr>"
      * rationale bad     -> "rationale field must be a string, got <type>"

    Returns ``None`` when ``data`` doesn't look like a verdict attempt at all
    (no score key AND no rationale key) — caller skips silently so trailing
    acknowledgment dicts don't pollute the error surface.
    """
    if "score" not in data:
        if "rationale" in data:
            return "score field missing in judge verdict"
        return None
    raw = data["score"]
    if isinstance(raw, bool):
        return f"score field is not a number: {raw!r}"
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return f"score field is not a number: {raw!r}"
    if not math.isfinite(f):
        return f"score field is not a finite number: {raw!r}"
    # Score is valid — then rationale must be the problem.
    rationale = data.get("rationale")
    if rationale is None:
        # None coerces to "" in the validator; should have succeeded.
        return "Failed to parse JSON verdict: unknown validation failure"
    if isinstance(rationale, str):
        # The validator rejects whitespace-only strings (collapses to "" then raises)
        # so a single-line ``rationale: `` invariant isn't broken downstream.
        if not " ".join(rationale.split()):
            return "rationale field is empty after whitespace collapse"
        # Shouldn't happen: a non-empty string passes the validator.
        return "Failed to parse JSON verdict: unknown validation failure"
    return f"rationale field must be a string, got {type(rationale).__name__}"
