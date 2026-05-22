"""HTML report generation for coder_eval runs.

Produces self-contained HTML files (inline CSS/JS, no external fonts or
images) that visualize a single task's conversation trace and success
criteria, plus cross-variant experiment summaries.

Designed for offline viewing and for upload as CI artifacts.
"""

from __future__ import annotations

import html as _html
import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from coder_eval.models import (
        CommandTelemetry,
        CriterionResult,
        EvaluationResult,
        ExperimentDefinition,
        ExperimentResult,
        TurnRecord,
        VariantAggregate,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Styling — fully inline; dark theme with light override via `.light` class.
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg-page: #0f1419;
  --bg-card: #1a2129;
  --bg-card-2: #232d38;
  --bg-code: #0b0f14;
  --fg: #e6edf3;
  --fg-muted: #8b98a5;
  --fg-dim: #6e7681;
  --border: #30363d;
  --accent: #58a6ff;
  --score-high: #22c55e;
  --score-mid: #f59e0b;
  --score-low: #ef4444;
  --status-success: #22c55e;
  --status-failure: #f59e0b;
  --status-error: #ef4444;
  --status-neutral: #6e7681;
  --shadow: 0 1px 3px rgba(0,0,0,0.3);
}
.light {
  --bg-page: #ffffff;
  --bg-card: #f6f8fa;
  --bg-card-2: #eaeef2;
  --bg-code: #f6f8fa;
  --fg: #1f2328;
  --fg-muted: #636c76;
  --fg-dim: #8b949e;
  --border: #d0d7de;
  --accent: #0969da;
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial,
               sans-serif;
  font-size: 14px;
  line-height: 1.55;
  color: var(--fg);
  background: var(--bg-page);
}
.container { max-width: 1200px; margin: 0 auto; padding: 24px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
h1, h2, h3, h4 { margin: 0 0 12px 0; line-height: 1.25; }
h1 { font-size: 22px; font-weight: 600; }
h2 { font-size: 18px; font-weight: 600; margin-top: 28px;
     padding-bottom: 8px; border-bottom: 1px solid var(--border); }
h3 { font-size: 15px; font-weight: 600; color: var(--fg-muted); }
h4 { font-size: 13px; font-weight: 600; color: var(--fg-muted); }
p { margin: 0 0 8px 0; }
.muted { color: var(--fg-muted); }
.dim { color: var(--fg-dim); }
.mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
        monospace; font-size: 12.5px; }
code { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
       monospace; font-size: 12.5px; background: var(--bg-code);
       border: 1px solid var(--border); padding: 1px 5px; border-radius: 3px; }
pre { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
      monospace; font-size: 12.5px; background: var(--bg-code);
      border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px;
      margin: 6px 0; overflow-x: auto; white-space: pre-wrap;
      word-wrap: break-word; }
.header-bar { display: flex; align-items: center; justify-content: space-between;
              margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
.title-group { flex: 1; min-width: 0; }
.title-group .subtitle { color: var(--fg-muted); font-size: 13px;
                         margin-top: 2px; word-break: break-all; }
.badges { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.badge { display: inline-flex; align-items: center; gap: 6px;
         padding: 4px 10px; border-radius: 12px; font-size: 12px;
         font-weight: 600; background: var(--bg-card-2);
         border: 1px solid var(--border); color: var(--fg); }
.badge.success { background: color-mix(in srgb, var(--status-success) 20%, var(--bg-card-2));
                 border-color: var(--status-success); color: var(--status-success); }
.badge.failure { background: color-mix(in srgb, var(--status-failure) 20%, var(--bg-card-2));
                 border-color: var(--status-failure); color: var(--status-failure); }
.badge.error { background: color-mix(in srgb, var(--status-error) 20%, var(--bg-card-2));
               border-color: var(--status-error); color: var(--status-error); }
.badge.neutral { color: var(--fg-muted); }
.score-pill { display: inline-block; padding: 2px 8px; border-radius: 10px;
              font-size: 12px; font-weight: 700; min-width: 42px;
              text-align: center; color: #0b0f14; }
.score-high { background: var(--score-high); }
.score-mid { background: var(--score-mid); }
.score-low { background: var(--score-low); }
.card { background: var(--bg-card); border: 1px solid var(--border);
        border-radius: 8px; padding: 16px 20px; margin-bottom: 16px;
        box-shadow: var(--shadow); }
.card.highlight { border-left: 4px solid var(--accent); }
.grid { display: grid; gap: 12px;
        grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }
.stat { background: var(--bg-card-2); padding: 10px 12px; border-radius: 6px;
        border: 1px solid var(--border); }
.stat .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
               color: var(--fg-muted); margin-bottom: 4px; }
.stat .value { font-size: 16px; font-weight: 600; }
table { width: 100%; border-collapse: collapse; margin: 4px 0 12px 0; }
th, td { text-align: left; padding: 8px 10px;
         border-bottom: 1px solid var(--border); vertical-align: top;
         font-size: 13px; }
th { color: var(--fg-muted); font-weight: 600; font-size: 12px;
     text-transform: uppercase; letter-spacing: 0.3px; }
tr:last-child td { border-bottom: none; }
details { margin: 6px 0; border: 1px solid var(--border); border-radius: 6px;
          background: var(--bg-card-2); }
details > summary { cursor: pointer; padding: 8px 12px; user-select: none;
                    font-weight: 500; list-style: none; }
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: "▸ "; color: var(--fg-dim);
                            display: inline-block; width: 14px;
                            transition: transform 0.15s; }
details[open] > summary::before { content: "▾ "; }
details .details-body { padding: 0 12px 10px 12px; }
.tool-row { display: flex; align-items: center; gap: 10px;
            flex-wrap: wrap; font-size: 13px; }
.tool-name { font-weight: 600; color: var(--accent); font-family: ui-monospace,
             SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }
.tool-status-ok { color: var(--status-success); font-size: 11px;
                  font-weight: 600; }
.tool-status-err { color: var(--status-error); font-size: 11px;
                   font-weight: 600; }
.tool-duration { color: var(--fg-muted); font-size: 12px; }
.tool-seq { color: var(--fg-dim); font-size: 11px; font-weight: 600;
            min-width: 24px; text-align: right; font-family: ui-monospace,
            SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }
.turn-header { display: flex; justify-content: space-between;
               align-items: baseline; flex-wrap: wrap; gap: 10px;
               margin-bottom: 10px; }
.turn-meta { font-size: 12px; color: var(--fg-muted); }
.criterion-row td:first-child { font-family: ui-monospace, SFMono-Regular,
                                 "SF Mono", Menlo, Consolas, monospace;
                                 color: var(--fg-muted); font-size: 12px; }
.judge-section { display: flex; flex-direction: column; gap: 12px; }
.judge-card { padding: 14px 16px; }
.judge-card-head { display: flex; justify-content: space-between;
                    align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.judge-card-title { font-size: 14px; line-height: 1.4; flex: 1; min-width: 0; }
.judge-card-score { flex-shrink: 0; }
.judge-rationale { margin-top: 10px; padding: 10px 12px;
                   background: var(--bg-code); border-radius: 4px;
                   font-size: 13px; line-height: 1.5; }
.judge-findings { margin: 8px 0 0 18px; padding-left: 4px; font-size: 13px;
                   line-height: 1.6; }
.judge-findings li { margin: 2px 0; }
.nav-toggle { display: inline-block; padding: 5px 10px; font-size: 12px;
              background: var(--bg-card-2); border: 1px solid var(--border);
              border-radius: 5px; color: var(--fg); cursor: pointer; }
.nav-toggle:hover { background: var(--bg-card); }
.llm-next-steps { margin: 6px 0 0 20px; padding-left: 0; }
.llm-next-steps li { margin: 3px 0; }
.iteration-group { padding: 12px 16px 4px 16px; margin-bottom: 20px;
                   border-radius: 0 6px 6px 0; border-left: 4px solid var(--status-neutral); }
/* rgba fallback for browsers without color-mix (Chrome <111, Firefox <113, Safari <16.2). */
.iteration-group--recovered { border-left-color: var(--status-success);
                              background: rgba(34, 197, 94, 0.06);
                              background: color-mix(in srgb, var(--status-success) 6%, transparent); }
.iteration-group--terminal { border-left-color: var(--status-error);
                             background: rgba(239, 68, 68, 0.06);
                             background: color-mix(in srgb, var(--status-error) 6%, transparent); }
.iteration-group__banner { margin: 0 0 12px 0; font-size: 14px; font-weight: 600; }
.iteration-group--recovered .iteration-group__banner { color: var(--status-success); }
.iteration-group--terminal .iteration-group__banner { color: var(--status-error); }
.attempt-transition { display: flex; align-items: center; gap: 8px;
                      margin: -6px 0 12px 8px; color: var(--status-error); font-size: 12.5px; }
.attempt-transition__icon { font-weight: 700; }
footer { margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--border);
         color: var(--fg-dim); font-size: 12px; text-align: center; }
"""


_JS = """
function toggleTheme() {
  document.documentElement.classList.toggle('light');
  try {
    const mode = document.documentElement.classList.contains('light') ? 'light' : 'dark';
    localStorage.setItem('coder-eval-theme', mode);
  } catch (e) { /* ignore */ }
}
(function() {
  try {
    if (localStorage.getItem('coder-eval-theme') === 'light') {
      document.documentElement.classList.add('light');
    }
  } catch (e) { /* ignore */ }
})();
"""


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


_MAX_VALUE_LEN = 400
_MAX_RESULT_LEN = 1200


def _esc(value: Any) -> str:
    """HTML-escape a value after coercing to string."""
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Truncate text to `limit` chars. Returns (text, was_truncated)."""
    if text is None:
        return "", False
    s = str(text)
    if len(s) <= limit:
        return s, False
    return s[:limit], True


def _score_class(score: float | None) -> str:
    """Return CSS class for a 0..1 score."""
    if score is None:
        return "score-mid"
    if score >= 0.8:
        return "score-high"
    if score >= 0.5:
        return "score-mid"
    return "score-low"


def _score_pill(score: float | None, suffix: str = "") -> str:
    """Render a colored score pill."""
    if score is None:
        return '<span class="score-pill score-mid">—</span>'
    label = f"{score:.2f}{suffix}"
    return f'<span class="score-pill {_score_class(score)}">{_esc(label)}</span>'


def _status_badge(status: Any) -> str:
    """Render a colored status badge for FinalStatus."""
    status_str = getattr(status, "value", None) or str(status)
    cls = "neutral"
    su = status_str.upper()
    if su == "SUCCESS":
        cls = "success"
    elif su in ("FAILURE", "MAX_TURNS_EXHAUSTED", "TIMEOUT", "TOKEN_BUDGET_EXCEEDED", "COST_BUDGET_EXCEEDED"):
        cls = "failure"
    elif su == "ERROR":
        cls = "error"
    return f'<span class="badge {cls}">{_esc(status_str)}</span>'


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.0f}s"


def _format_ms(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"


def _format_params(params: dict[str, Any]) -> str:
    """Pretty-print tool parameters as JSON, truncating long values."""
    try:
        trimmed: dict[str, Any] = {}
        for key, val in params.items():
            if isinstance(val, str) and len(val) > _MAX_VALUE_LEN:
                trimmed[key] = val[:_MAX_VALUE_LEN] + f"… ({len(val) - _MAX_VALUE_LEN} more chars)"
            else:
                trimmed[key] = val
        return json.dumps(trimmed, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(params)


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_header(result: EvaluationResult) -> str:
    started = result.started_at.isoformat(timespec="seconds") if result.started_at else "—"
    duration = _format_duration(result.duration_seconds)
    score_badge = _score_pill(result.weighted_score) if result.weighted_score is not None else ""
    model = _esc(result.model_used or "—")
    agent_type = _esc(getattr(result.agent_type, "value", result.agent_type))
    subtitle = _esc(result.task_description.strip().splitlines()[0] if result.task_description else "")
    turns_count = result.total_assistant_turns or 0
    cost_badge = ""
    if result.total_token_usage and result.total_token_usage.total_cost_usd is not None:
        cost_badge = f'<span class="badge neutral">${result.total_token_usage.total_cost_usd:.4f}</span>'
    return f"""
<div class="header-bar">
  <div class="title-group">
    <h1>Task: {_esc(result.task_id)}</h1>
    <div class="subtitle">{subtitle}</div>
  </div>
  <div class="badges">
    {_status_badge(result.final_status)}
    {score_badge}
    <span class="badge neutral">{agent_type} · {model}</span>
    <span class="badge neutral">{_esc(duration)}</span>
    {cost_badge}
    <span class="nav-toggle" onclick="toggleTheme()">Toggle theme</span>
  </div>
</div>
<div class="card">
  <div class="grid">
    <div class="stat"><div class="label">Variant</div><div class="value">{_esc(result.variant_id)}</div></div>
    <div class="stat"><div class="label">Started</div><div class="value mono">{_esc(started)}</div></div>
    <div class="stat"><div class="label">Duration</div><div class="value">{_esc(duration)}</div></div>
    <div class="stat"><div class="label">Turns (SDK)</div><div class="value">{turns_count}</div></div>
    <div class="stat"><div class="label">Commands</div><div class="value">{result.actual_commands or 0}</div></div>
  </div>
</div>
"""


def _render_criteria_details(cr: CriterionResult) -> str:
    """Render the Details cell for a criterion.

    Short details (no newline) render inline to keep the table scannable.
    Multiline details (typical for `run_command` which includes command,
    exit, stdout, and stderr) render as a collapsible `<details>` block
    whose summary shows just the first line — the user expands to see the
    full captured output.

    Judge criteria render their findings + transcript in a dedicated
    "Judge Verdicts" section below the criteria table; this cell stays
    compact and just shows the score=...\\nrationale: ... summary.
    """
    err = _esc(cr.error) if cr.error else ""
    details_raw = (cr.details or "").strip()
    details_txt = _esc(details_raw)

    if not details_raw:
        return err or '<span class="dim">—</span>'

    lines = details_raw.splitlines()
    if len(lines) <= 1:
        body = err + ("<br/>" if err and details_txt else "") + details_txt
        return f'<div class="dim mono" style="white-space:pre-wrap">{body}</div>'

    summary = _esc(lines[0])
    full_pre = f'<pre style="margin:6px 0 0 0">{details_txt}</pre>'
    prefix = err + "<br/>" if err else ""
    return (
        f"{prefix}"
        f'<details><summary class="mono dim">{summary}</summary>'
        f'<div class="details-body">{full_pre}</div></details>'
    )


def _render_judge_section(criteria: list[CriterionResult]) -> str:
    """Render the dedicated 'Judge Verdicts' section containing per-judge cards.

    Each judge result that carries verdict evidence (findings, transcript) gets
    a card with: type + description, score badge, rationale extracted from the
    details string, an expanded-by-default Findings list, and a collapsed
    Judge transcript disclosure.

    Returns an empty string when there's no judge result with anything to show
    — non-judge tasks render no Judge Verdicts heading.
    """
    cards: list[str] = []
    for cr in criteria:
        # criterion_type identifies judges; details/findings/transcript may all
        # be present (typed JudgeCriterionResult) or in model_extra (round-tripped).
        if cr.criterion_type not in ("llm_judge", "agent_judge"):
            continue
        findings_raw = getattr(cr, "findings", []) or []
        findings = [str(f).strip() for f in findings_raw if str(f).strip()]
        transcript = getattr(cr, "transcript", None)
        if not findings and transcript in (None, {}, []):
            continue
        cards.append(_render_judge_card(cr, findings, transcript))

    if not cards:
        return ""

    return f'<h2>Judge Verdicts ({len(cards)})</h2><div class="judge-section">' + "".join(cards) + "</div>"


def _extract_rationale(details: str | None) -> str:
    """Pull the rationale line out of the standard format_details payload.

    ``format_details`` writes ``score=0.750\\nrationale: ...\\nnotes: ...``.
    Reach into the second line and strip the ``rationale: `` prefix.
    Returns an empty string when the details aren't in this shape.
    """
    if not details:
        return ""
    for line in details.splitlines():
        prefix = "rationale: "
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _render_judge_card(cr: CriterionResult, findings: list[str], transcript: Any) -> str:
    """Render one judge result as a card (type + score + rationale + findings + transcript)."""
    rationale = _extract_rationale(cr.details)
    rationale_html = f'<div class="judge-rationale">{_esc(rationale)}</div>' if rationale else ""

    findings_html = ""
    if findings:
        items = "".join(f"<li>{_esc(f)}</li>" for f in findings)
        # Findings open by default — they're the audit highlight reviewers came for.
        findings_html = (
            f'<details open style="margin-top:8px">'
            f'<summary class="mono dim">Findings ({len(findings)})</summary>'
            f'<ul class="judge-findings">{items}</ul>'
            f"</details>"
        )

    transcript_html = _render_judge_transcript(transcript)

    return (
        '<div class="card judge-card">'
        + '<div class="judge-card-head">'
        + f'<div class="judge-card-title"><span class="mono dim">{_esc(cr.criterion_type)}</span> '
        + f"&middot; {_esc(cr.description)}</div>"
        + f'<div class="judge-card-score">{_score_pill(cr.score)}</div>'
        + "</div>"
        + rationale_html
        + findings_html
        + transcript_html
        + "</div>"
    )


def _render_judge_transcript(transcript: Any) -> str:
    """Render a JudgeTranscript (or its dict round-trip form) as a disclosure block."""
    if transcript is None:
        return ""
    # Normalize to a plain dict so dict-form (round-tripped) and model-form work the same.
    if hasattr(transcript, "model_dump"):
        data = transcript.model_dump()
    elif isinstance(transcript, dict):
        data = transcript
    else:
        return ""

    tool_calls = data.get("tool_calls") or []
    raw_verdict = (data.get("raw_verdict") or "").strip()
    duration = data.get("duration_seconds") or 0.0
    truncated = bool(data.get("truncated"))
    token_usage = data.get("token_usage")

    if not tool_calls and not raw_verdict and not token_usage:
        return ""

    rows: list[str] = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            tool = _esc(str(tc.get("tool_name", "")))
            detail = _esc(str(tc.get("detail", "")))
            status = _esc(str(tc.get("status", "")))
            preview = _esc(str(tc.get("result_preview", "")))
        else:
            tool = _esc(getattr(tc, "tool_name", ""))
            detail = _esc(getattr(tc, "detail", ""))
            status = _esc(getattr(tc, "status", ""))
            preview = _esc(getattr(tc, "result_preview", ""))
        rows.append(
            f"<tr><td class='mono'>{tool}</td><td class='mono dim'>{status}</td>"
            + f"<td class='mono' style='white-space:pre-wrap'>{detail}</td>"
            + f"<td class='dim' style='white-space:pre-wrap'>{preview}</td></tr>"
        )
    table = ""
    if rows:
        table = (
            "<table style='margin-top:6px;font-size:0.9em'>"
            "<thead><tr><th>Tool</th><th>Status</th><th>Detail</th><th>Result</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    raw_block = ""
    if raw_verdict:
        raw_block = (
            f'<details style="margin-top:6px"><summary class="mono dim">Raw verdict</summary>'
            f'<pre style="margin:6px 0 0 0;white-space:pre-wrap">{_esc(raw_verdict)}</pre></details>'
        )

    meta_bits: list[str] = []
    if duration:
        meta_bits.append(f"duration: {float(duration):.1f}s")
    if isinstance(token_usage, dict):
        in_tok = token_usage.get("input_tokens")
        out_tok = token_usage.get("output_tokens")
        if in_tok is not None or out_tok is not None:
            meta_bits.append(f"tokens: in={in_tok or 0}, out={out_tok or 0}")
    if truncated:
        meta_bits.append("truncated")
    meta = f'<div class="dim mono" style="margin-top:4px">{_esc(" • ".join(meta_bits))}</div>' if meta_bits else ""

    summary_label = f"Judge transcript ({len(tool_calls)} tool calls)" if tool_calls else "Judge transcript"
    return (
        f'<details style="margin-top:4px"><summary class="mono dim">{_esc(summary_label)}</summary>'
        f'<div class="details-body">{meta}{table}{raw_block}</div></details>'
    )


def _render_criteria(results: list[CriterionResult]) -> str:
    if not results:
        return """
<div class="card">
  <h2 style="margin-top:0;border:none;padding:0">Success Criteria</h2>
  <p class="muted">No criteria were evaluated (task may have errored before
  the checker ran).</p>
</div>
"""
    rows: list[str] = []
    for cr in results:
        rows.append(
            f"""
<tr class="criterion-row">
  <td>{_esc(cr.criterion_type)}</td>
  <td>{_esc(cr.description)}</td>
  <td style="text-align:center">{_score_pill(cr.score)}</td>
  <td>{_render_criteria_details(cr)}</td>
</tr>
"""
        )
    passed = sum(1 for r in results if r.score >= r.pass_threshold)
    total = len(results)
    return f"""
<h2>Success Criteria <span class="muted" style="font-weight:400">({passed}/{total} passed)</span></h2>
<div class="card" style="padding:0">
<table>
  <thead>
    <tr>
      <th style="width:15%">Type</th>
      <th style="width:35%">Description</th>
      <th style="width:10%">Score</th>
      <th>Details</th>
    </tr>
  </thead>
  <tbody>
    {"".join(rows)}
  </tbody>
</table>
</div>
"""


def _render_command(cmd: CommandTelemetry) -> str:
    status_cls = "tool-status-ok" if cmd.result_status == "success" else "tool-status-err"
    status_label = (cmd.result_status or "unknown").upper()
    params_pretty = _format_params(cmd.parameters or {})
    params_pretty_trunc, _ = _truncate(params_pretty, 4000)
    result_text = cmd.result_summary or ""
    result_trunc, was_trunc = _truncate(result_text, _MAX_RESULT_LEN)
    if was_trunc:
        result_trunc += f"\n\n… (truncated, {len(result_text) - _MAX_RESULT_LEN} more chars)"
    err_block = ""
    if cmd.error_message:
        err_trunc, _ = _truncate(cmd.error_message, 2000)
        err_block = f"""<h4>Error</h4><pre>{_esc(err_trunc)}</pre>"""
    return f"""
<details>
  <summary>
    <span class="tool-row">
      <span class="tool-seq">#{cmd.sequence_number}</span>
      <span class="tool-name">{_esc(cmd.tool_name)}</span>
      <span class="{status_cls}">{_esc(status_label)}</span>
      <span class="tool-duration">{_esc(_format_ms(cmd.duration_ms))}</span>
    </span>
  </summary>
  <div class="details-body">
    <h4>Parameters</h4>
    <pre>{_esc(params_pretty_trunc)}</pre>
    <h4>Result</h4>
    <pre>{_esc(result_trunc) if result_trunc else '<span class="dim">(no summary)</span>'}</pre>
    {err_block}
  </div>
</details>
"""


def _group_turns_by_iteration(
    turns: list[TurnRecord],
) -> list[tuple[int, list[TurnRecord]]]:
    """Group consecutive TurnRecords by iteration as ``(iteration, group)`` tuples for the renderer."""
    from .reports import group_consecutive_by_iteration

    groups = group_consecutive_by_iteration(turns, lambda t: t.iteration)
    return [(group[0].iteration, group) for group in groups]


def _render_iteration_group(iteration: int, group: list[TurnRecord]) -> str:
    """Render an iteration's turns; multi-attempt groups get a coloured wrapper + banner."""
    if len(group) == 1:
        return _render_turn(group[0])

    n = len(group)
    crashed_count = sum(1 for t in group if t.crashed)
    recovered = any(not t.crashed for t in group)
    if recovered:
        plural = "s" if crashed_count != 1 else ""
        banner = f"Iteration {iteration} — recovered after {crashed_count} crashed attempt{plural}"
        modifier = "iteration-group--recovered"
    else:
        banner = f"Iteration {iteration} — terminal failure ({n} crashed attempts)"
        modifier = "iteration-group--terminal"

    parts: list[str] = []
    for i, t in enumerate(group):
        parts.append(_render_turn(t, attempt_index=i + 1, attempt_total=n, recovered=(not t.crashed and recovered)))
        # Marker between a crashed attempt and the next; suppressed on terminal (no following card).
        if t.crashed and i + 1 < len(group):
            reason = t.crash_reason or "Agent crashed"
            parts.append(_render_attempt_transition(reason))
    return f"""
<div class="iteration-group {modifier}">
  <h3 class="iteration-group__banner">{_esc(banner)}</h3>
  {"".join(parts)}
</div>
"""


def _render_attempt_transition(reason: str) -> str:
    """Render a small inline marker between two attempts of the same iteration."""
    return f"""
<div class="attempt-transition">
  <span class="attempt-transition__icon">⚠</span>
  <span><strong>{_esc(reason)}</strong> · resuming ↓</span>
</div>
"""


def _render_turn(
    turn: TurnRecord,
    *,
    attempt_index: int | None = None,
    attempt_total: int | None = None,
    recovered: bool = False,
) -> str:
    prompt_trunc, _ = _truncate(turn.user_input or "", 2000)
    response_trunc, _ = _truncate(turn.agent_output or "", 4000)
    cmds_html = "".join(_render_command(c) for c in (turn.commands or []))
    if not cmds_html:
        cmds_html = '<p class="muted">No tool calls recorded for this turn.</p>'
    tokens_label = ""
    if turn.token_usage:
        tu = turn.token_usage
        tokens_label = (
            f'<span class="badge neutral">'
            f"in {tu.input_tokens} · out {tu.output_tokens}"
            f" · cache {tu.cache_read_input_tokens}"
            f"</span>"
        )
    duration_label = f'<span class="badge neutral">{_esc(_format_duration(turn.duration_seconds))}</span>'
    exhausted = '<span class="badge failure">max_turns exhausted</span>' if turn.max_turns_exhausted else ""
    response_block = (
        f"""
  <details>
    <summary>Agent response</summary>
    <div class="details-body"><pre>{_esc(response_trunc)}</pre></div>
  </details>"""
        if turn.agent_output
        else ""
    )
    crashed = '<span class="badge error">crashed (partial)</span>' if turn.crashed else ""
    recovered_badge = '<span class="badge success">recovered</span>' if recovered and not turn.crashed else ""
    if attempt_total is not None and attempt_total > 1 and attempt_index is not None:
        # Iteration number lives on the group banner; card heading is just the attempt.
        heading = f"Attempt {attempt_index} of {attempt_total}"
    else:
        heading = f"Iteration {turn.iteration}"
    return f"""
<div class="card">
  <div class="turn-header">
    <h3>{_esc(heading)}</h3>
    <div class="badges">
      <span class="badge neutral">{turn.assistant_turn_count} assistant turns</span>
      {tokens_label}
      {duration_label}
      {exhausted}
      {crashed}
      {recovered_badge}
    </div>
  </div>
  <details>
    <summary>Prompt to agent</summary>
    <div class="details-body"><pre>{_esc(prompt_trunc)}</pre></div>
  </details>{response_block}
  <h4 style="margin-top:12px">Tool Calls ({len(turn.commands or [])})</h4>
  {cmds_html}
</div>
"""


def _render_command_stats(stats: Any | None) -> str:
    if stats is None:
        return ""
    rows: list[str] = []
    for tool, count in sorted((stats.commands_by_tool or {}).items(), key=lambda x: x[1], reverse=True):
        rows.append(f"<tr><td class='mono'>{_esc(tool)}</td><td>{count}</td></tr>")
    rows_html = "".join(rows) or "<tr><td colspan='2' class='muted'>No commands</td></tr>"
    from .reports import SLOW_PARAMS_PREVIEW_CHARS

    slow_rows_list: list[str] = []
    for c in stats.slowest_commands or []:
        params_full = str(c.parameters)
        params_preview = params_full[:SLOW_PARAMS_PREVIEW_CHARS]
        if len(params_full) > SLOW_PARAMS_PREVIEW_CHARS:
            params_preview += "..."
        slow_rows_list.append(
            f"<tr><td class='mono'>{_esc(c.tool)}</td><td>{_esc(_format_ms(c.duration_ms))}</td>"
            + f"<td class='mono dim'>{_esc(params_preview)}</td></tr>"
        )
    slow_rows = "".join(slow_rows_list) or "<tr><td colspan='3' class='muted'>—</td></tr>"
    success_pct = (stats.successful_commands / stats.total_commands * 100) if stats.total_commands else 0.0
    successful_str = f"{stats.successful_commands} ({success_pct:.0f}%)"
    avg_str = _esc(_format_ms(stats.avg_command_time_ms))

    extras: list[str] = []
    if stats.most_common_sequence:
        extras.append(f"<p><strong>Most Common Pattern:</strong> <code>{_esc(stats.most_common_sequence)}</code></p>")
    skill_count = (stats.commands_by_tool or {}).get("Skill", 0)
    if skill_count > 0:
        extras.append(f"<p><strong>Skill Tool Invoked:</strong> {skill_count} time(s)</p>")
    extras_html = "".join(extras)

    return f"""
<h2>Command Telemetry</h2>
<div class="card">
  <div class="grid">
    <div class="stat"><div class="label">Total</div><div class="value">{stats.total_commands}</div></div>
    <div class="stat"><div class="label">Successful</div><div class="value">{successful_str}</div></div>
    <div class="stat"><div class="label">Failed</div><div class="value">{stats.failed_commands}</div></div>
    <div class="stat"><div class="label">Avg / cmd</div><div class="value">{avg_str}</div></div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 2fr;gap:16px;margin-top:12px">
    <div>
      <h4>By Tool</h4>
      <table>
        <thead><tr><th>Tool</th><th>Count</th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    <div>
      <h4>Slowest</h4>
      <table>
        <thead><tr><th>Tool</th><th>Duration</th><th>Parameters</th></tr></thead>
        <tbody>{slow_rows}</tbody>
      </table>
    </div>
  </div>
  {extras_html}
</div>
"""


def _render_error_details(result: EvaluationResult) -> str:
    if not result.error_message and not result.error_details:
        return ""
    details = result.error_details or {}
    category = details.get("error_category", "unknown") if isinstance(details, dict) else ""
    component = details.get("component", "") if isinstance(details, dict) else ""
    retryable = details.get("is_retryable") if isinstance(details, dict) else None
    retry_badge = ""
    if retryable is not None:
        retry_badge = (
            '<span class="badge neutral">retryable</span>'
            if retryable
            else '<span class="badge neutral">non-retryable</span>'
        )
    # Prefer the in-result tail captured at run time (sanitised, bounded). Fall
    # back to the legacy stack_trace from error_details so reports regenerated
    # against archived runs (pre-error_log_tail) still surface diagnostics.
    log_text = result.error_log_tail or ""
    if not log_text and isinstance(details, dict):
        stack = details.get("stack_trace")
        if stack:
            log_text = str(stack)
    logs_html = ""
    if log_text:
        logs_body = f'<div class="details-body"><pre>{_esc(log_text)}</pre></div>'
        logs_html = f"<details><summary>Logs</summary>{logs_body}</details>"
    component_html = f'<span class="badge neutral">{_esc(component)}</span>' if component else ""
    return f"""
<h2>Error</h2>
<div class="card" style="border-left:4px solid var(--status-error)">
  <div class="badges" style="margin-bottom:10px">
    <span class="badge error">{_esc(category)}</span>
    {component_html}
    {retry_badge}
  </div>
  <h4>Message</h4>
  <pre>{_esc(result.error_message or "(no message)")}</pre>
  {logs_html}
</div>
"""


def _render_token_usage(result: EvaluationResult) -> str:
    """Render Token Usage section — totals + cost."""
    tu = result.total_token_usage
    if tu is None:
        return ""
    total = tu.total_tokens
    cost_str = f"${tu.total_cost_usd:.4f}" if tu.total_cost_usd is not None else "N/A"
    cache_write_fmt = f"{tu.cache_creation_input_tokens:,}"
    cache_read_fmt = f"{tu.cache_read_input_tokens:,}"
    return f"""
<h2>Token Usage</h2>
<div class="card">
  <div class="grid">
    <div class="stat"><div class="label">Input</div><div class="value">{tu.input_tokens:,}</div></div>
    <div class="stat"><div class="label">Output</div><div class="value">{tu.output_tokens:,}</div></div>
    <div class="stat"><div class="label">Cache Write</div><div class="value">{cache_write_fmt}</div></div>
    <div class="stat"><div class="label">Cache Read</div><div class="value">{cache_read_fmt}</div></div>
    <div class="stat"><div class="label">Total</div><div class="value">{total:,}</div></div>
    <div class="stat"><div class="label">Cost</div><div class="value">{_esc(cost_str)}</div></div>
  </div>
</div>
"""


def _render_generation_metrics(result: EvaluationResult) -> str:
    """Render Generation Metrics — latency, turns."""
    from .reports import count_partials_by_outcome, group_consecutive_by_iteration

    turns = result.iterations or []
    num_turns = len(turns)
    asst_turns = result.total_assistant_turns or 0
    avg_turn = (sum(t.duration_seconds for t in turns) / num_turns) if num_turns else 0.0
    total_latency = _esc(_format_duration(result.duration_seconds))
    avg_latency = _esc(_format_duration(avg_turn))
    crashed_stat = ""
    groups = group_consecutive_by_iteration(turns, lambda t: t.iteration)
    total_partials, recovered_partials, terminal_partials = count_partials_by_outcome(groups, lambda t: t.crashed)
    if total_partials:
        breakdown = f"{total_partials} ({recovered_partials} recovered, {terminal_partials} terminal)"
        crashed_stat = (
            f'<div class="stat"><div class="label">Crashed Partials</div>'
            f'<div class="value">{_esc(breakdown)}</div></div>'
        )
    return f"""
<h2>Generation Metrics</h2>
<div class="card">
  <div class="grid">
    <div class="stat"><div class="label">Total Latency</div><div class="value">{total_latency}</div></div>
    <div class="stat"><div class="label">Turns</div><div class="value">{num_turns}</div></div>
    <div class="stat"><div class="label">Assistant Turns</div><div class="value">{asst_turns}</div></div>
    <div class="stat"><div class="label">Avg Turn Latency</div><div class="value">{avg_latency}</div></div>
    {crashed_stat}
  </div>
</div>
"""


def _render_commands_efficiency(result: EvaluationResult) -> str:
    """Render Commands Efficiency card. Empty when data missing."""
    if result.commands_efficiency is None or result.expected_commands is None or result.actual_commands is None:
        return ""
    pct = result.commands_efficiency * 100
    ratio = f"{result.expected_commands}/{result.actual_commands}"
    return f"""
<h2>Commands Efficiency</h2>
<div class="card">
  <div class="grid">
    <div class="stat"><div class="label">Efficiency</div><div class="value">{pct:.1f}%</div></div>
    <div class="stat"><div class="label">Expected / Actual</div><div class="value">{ratio}</div></div>
  </div>
</div>
"""


def _render_agent_settings(result: EvaluationResult) -> str:
    """Render Agent Settings section. Prefers sdk_options, falls back to agent_config."""
    from .reports import collect_agent_settings_rows

    if result.sdk_options:
        settings: dict[str, Any] = result.sdk_options
        is_sdk = True
    elif result.agent_config is not None:
        settings = result.agent_config.model_dump(warnings=False)
        is_sdk = False
    else:
        return ""

    rows = collect_agent_settings_rows(settings, is_sdk)
    body = "".join(f"<tr><td class='mono dim'>{_esc(label)}</td><td>{_esc(value)}</td></tr>" for label, value in rows)
    return f"""
<h2>Agent Settings</h2>
<div class="card" style="padding:0">
  <table>
    <tbody>{body}</tbody>
  </table>
</div>
"""


def _render_installed_tools(result: EvaluationResult) -> str:
    """Render Installed Tools section. Empty when absent."""
    tools = result.environment_info.get("installed_tools") if result.environment_info else None
    if not tools or not isinstance(tools, dict):
        return ""
    rows = "".join(
        f"<tr><td class='mono'>{_esc(name)}</td><td class='mono'>{_esc(ver)}</td></tr>"
        for name, ver in sorted(tools.items())
    )
    return f"""
<h2>Installed Tools</h2>
<div class="card" style="padding:0">
  <table>
    <thead><tr><th>Tool</th><th>Version</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
"""


_SIMULATION_STOP_REASON_LABELS = {
    "criteria_passed": ("success", "criteria passed"),
    "stop_token": ("neutral", "simulator ended dialog"),
    "max_turns": ("failure", "turn cap reached"),
    "budget": ("failure", "token budget exhausted"),
    "error": ("failure", "simulator error"),
}


def _render_simulation(result: EvaluationResult) -> str:
    """Render the Simulation section (only when simulation telemetry is present)."""
    sim = result.simulation
    if sim is None:
        return ""
    badge_class, label = _SIMULATION_STOP_REASON_LABELS.get(sim.stop_reason, ("neutral", sim.stop_reason))
    trial_line = (
        f"<tr><td class='mono dim'>Trial</td><td>{sim.replicate_index + 1} of {sim.n_trials}</td></tr>"
        if sim.n_trials > 1
        else ""
    )
    failure_line = (
        f"<tr><td class='mono dim'>Simulator failures</td><td>{sim.simulator_failures}</td></tr>"
        if sim.simulator_failures > 0
        else ""
    )
    return f"""
<h2>Simulation</h2>
<div class="card" style="padding:0">
  <table>
    <tbody>
      <tr><td class='mono dim'>Stop reason</td>
          <td><span class="badge {badge_class}">{_esc(label)}</span>
              <span class="mono dim"> ({_esc(sim.stop_reason)})</span></td></tr>
      <tr><td class='mono dim'>Total turns</td><td>{sim.total_turns}</td></tr>
      {trial_line}
      <tr><td class='mono dim'>Simulator tokens</td>
          <td>in {sim.simulator_input_tokens} · out {sim.simulator_output_tokens}</td></tr>
      {failure_line}
    </tbody>
  </table>
</div>
"""


def _render_environment(result: EvaluationResult) -> str:
    """Render Environment section (excluding installed_tools, which has its own)."""
    env = {k: v for k, v in (result.environment_info or {}).items() if k != "installed_tools"}
    if not env:
        return ""
    rows = "".join(f"<tr><td class='mono dim'>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in env.items())
    return f"""
<h2>Environment</h2>
<div class="card" style="padding:0">
  <table>
    <tbody>{rows}</tbody>
  </table>
</div>
"""


def _wrap_document(title: str, body: str) -> str:
    """Wrap body HTML in a complete standalone document."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
{body}
<footer>Generated by coder_eval · {_esc(datetime.now().isoformat(timespec="seconds"))}</footer>
</div>
<script>{_JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Variant / Experiment helpers
# ---------------------------------------------------------------------------


def _variant_stddev_lines(variant_id: str, result: ExperimentResult | None) -> str:
    """Render Score/Duration stddev stats inside the summary card.

    Returns an empty string when ``result`` is not provided or data is insufficient.
    """
    if result is None:
        return ""
    from .reports_stats import stddev

    vrs = [vr for ts in result.task_summaries for vr in ts.variant_results if vr.variant_id == variant_id]
    scores = [vr.weighted_score for vr in vrs]
    durations = [vr.duration_seconds for vr in vrs]
    extras: list[str] = []
    if len(scores) >= 2:
        extras.append(f"<li><strong>Score Stddev:</strong> {stddev(scores):.3f}</li>")
    if len(durations) >= 2:
        extras.append(f"<li><strong>Duration Stddev:</strong> {stddev(durations):.1f}s</li>")
    if not extras:
        return ""
    return f'<ul style="margin-top:10px">{"".join(extras)}</ul>'


def _variant_rich_sections(variant_id: str, result: ExperimentResult | None, run_dir: Path | None) -> str:
    """Render Generation Metrics, Token Usage, Command Telemetry, Agent Settings,
    Installed Tools, and Environment sections by loading per-task JSON from disk.

    Aggregates across all tasks in the variant (matching the markdown reporter).
    Returns "" when either ``result`` or ``run_dir`` is missing or no task data
    loads successfully.
    """
    if result is None or run_dir is None:
        return ""

    from .analysis import calculate_command_statistics
    from .reports_stats import load_variant_eval_results

    eval_results = load_variant_eval_results(run_dir, variant_id, result.task_summaries)
    if not eval_results:
        return ""

    sections: list[str] = []
    sections.append(_render_variant_generation_metrics(eval_results))
    sections.append(_render_variant_token_usage(eval_results))

    # Command Telemetry — aggregate turns across all variant tasks
    all_turns = [t for r in eval_results for t in r.iterations]
    if all_turns:
        stats = calculate_command_statistics(all_turns)
        if stats.total_commands > 0:
            sections.append(_render_command_stats(stats))

    # Agent Settings and Environment are per-task snapshots — first task with
    # data wins, matching the markdown reporter's `first task` fallback.
    settings_result = next((r for r in eval_results if r.sdk_options or r.agent_config is not None), None)
    if settings_result is not None:
        sections.append(_render_agent_settings(settings_result))

    sections.append(_render_variant_installed_tools(eval_results))

    env_result = next((r for r in eval_results if r.environment_info), None)
    if env_result is not None:
        sections.append(_render_environment(env_result))
    return "".join(sections)


def _render_variant_generation_metrics(eval_results: list[EvaluationResult]) -> str:
    """Aggregate generation metrics across all tasks in a variant."""
    if not eval_results:
        return ""
    total_tasks = len(eval_results)
    total_duration = sum(r.duration_seconds for r in eval_results)
    total_turns = sum(len(r.iterations) for r in eval_results)
    total_asst = sum(r.total_assistant_turns or 0 for r in eval_results)
    per_turn_latencies = [t.duration_seconds for r in eval_results for t in r.iterations]
    avg_turn = (sum(per_turn_latencies) / len(per_turn_latencies)) if per_turn_latencies else 0.0
    total_latency_fmt = _esc(_format_duration(total_duration))
    avg_turn_fmt = _esc(_format_duration(avg_turn))
    return f"""
<h2>Generation Metrics</h2>
<div class="card">
  <div class="grid">
    <div class="stat"><div class="label">Tasks</div><div class="value">{total_tasks}</div></div>
    <div class="stat"><div class="label">Total Latency</div><div class="value">{total_latency_fmt}</div></div>
    <div class="stat"><div class="label">Turns</div><div class="value">{total_turns}</div></div>
    <div class="stat"><div class="label">Assistant Turns</div><div class="value">{total_asst}</div></div>
    <div class="stat"><div class="label">Avg Turn Latency</div><div class="value">{avg_turn_fmt}</div></div>
  </div>
</div>
"""


def _render_variant_token_usage(eval_results: list[EvaluationResult]) -> str:
    """Aggregate token usage across all tasks in a variant."""
    usages = [r.total_token_usage for r in eval_results if r.total_token_usage is not None]
    if not usages:
        return ""
    input_tok = sum(u.input_tokens for u in usages)
    output_tok = sum(u.output_tokens for u in usages)
    cache_write = sum(u.cache_creation_input_tokens for u in usages)
    cache_read = sum(u.cache_read_input_tokens for u in usages)
    total = input_tok + output_tok + cache_write + cache_read
    costs = [u.total_cost_usd for u in usages if u.total_cost_usd is not None]
    cost_str = f"${sum(costs):.4f}" if costs else "N/A"
    return f"""
<h2>Token Usage</h2>
<div class="card">
  <div class="grid">
    <div class="stat"><div class="label">Input</div><div class="value">{input_tok:,}</div></div>
    <div class="stat"><div class="label">Output</div><div class="value">{output_tok:,}</div></div>
    <div class="stat"><div class="label">Cache Write</div><div class="value">{cache_write:,}</div></div>
    <div class="stat"><div class="label">Cache Read</div><div class="value">{cache_read:,}</div></div>
    <div class="stat"><div class="label">Total</div><div class="value">{total:,}</div></div>
    <div class="stat"><div class="label">Cost</div><div class="value">{_esc(cost_str)}</div></div>
  </div>
</div>
"""


def _render_variant_installed_tools(eval_results: list[EvaluationResult]) -> str:
    """Render Installed Tools rows aggregated from every task in the variant."""
    rows: list[str] = []
    for r in eval_results:
        tools = r.environment_info.get("installed_tools") if r.environment_info else None
        if not tools or not isinstance(tools, dict):
            continue
        for name, ver in sorted(tools.items()):
            rows.append(
                f"<tr><td class='mono'>{_esc(r.task_id)}</td>"
                + f"<td class='mono'>{_esc(name)}</td>"
                + f"<td class='mono'>{_esc(ver)}</td></tr>"
            )
    if not rows:
        return ""
    return f"""
<h2>Installed Tools</h2>
<div class="card" style="padding:0">
  <table>
    <thead><tr><th>Task</th><th>Tool</th><th>Version</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>
"""


def _experiment_prompt_config(experiment: ExperimentDefinition | None, variant_ids: list[str]) -> str:
    """Render the Prompt Configuration section when the experiment definition
    actually specifies any mutations or overrides."""
    if experiment is None:
        return ""
    from .reports_stats import describe_prompt_config

    has_config = bool(experiment.defaults and experiment.defaults.prompt_mutations) or any(
        v.prompt_mutations or v.initial_prompt or v.initial_prompt_file for v in experiment.variants
    )
    if not has_config:
        return ""
    variant_map = {v.variant_id: v for v in experiment.variants}
    items = []
    for vid in variant_ids:
        v = variant_map.get(vid)
        desc = describe_prompt_config(v) if v else "(unknown)"
        items.append(f"<li><strong>{_esc(vid)}</strong>: {_esc(desc)}</li>")
    return f"""
<h2>Prompt Configuration</h2>
<div class="card">
  <ul>{"".join(items)}</ul>
</div>
"""


def _collect_variant_series(result: ExperimentResult) -> dict[str, dict[str, list[float]]]:
    """Group per-variant numeric series (scores, durations, etc.)."""
    series: dict[str, dict[str, list[float]]] = {
        vid: {"scores": [], "durations": [], "asst_turns": [], "tokens": []} for vid in result.variant_ids
    }
    for ts in result.task_summaries:
        for vr in ts.variant_results:
            s = series.get(vr.variant_id)
            if s is None:
                continue
            s["scores"].append(vr.weighted_score)
            s["durations"].append(vr.duration_seconds)
            if vr.total_tokens is not None:
                s["tokens"].append(float(vr.total_tokens))
            if vr.total_assistant_turns is not None:
                s["asst_turns"].append(float(vr.total_assistant_turns))
    return series


def _experiment_aggregate_metrics(result: ExperimentResult) -> str:
    """Render the Aggregate Metrics table (with p-values when exactly 2 variants)."""
    from .reports_stats import fmt_mean_sd, fmt_p, welch_t_test

    show_p = len(result.variant_ids) == 2
    vid_a, vid_b = (result.variant_ids[0], result.variant_ids[1]) if show_p else ("", "")

    header_cells = ["<th>Metric</th>"] + [f"<th>{_esc(vid)}</th>" for vid in result.variant_ids]
    if show_p:
        header_cells.append("<th>p-value</th>")
    header_html = "<tr>" + "".join(header_cells) + "</tr>"

    series = _collect_variant_series(result)

    def _row(label: str, values: list[str], p: str | None) -> str:
        cells = [f"<td>{_esc(label)}</td>"] + [f"<td>{_esc(v)}</td>" for v in values]
        if show_p:
            cells.append(f"<td>{_esc(p) if p is not None else '—'}</td>")
        return "<tr>" + "".join(cells) + "</tr>"

    rows: list[str] = []
    rows.append(_row("Tasks Run", [str(result.variant_aggregates[vid].tasks_run) for vid in result.variant_ids], None))
    rows.append(
        _row(
            "Succeeded",
            [str(result.variant_aggregates[vid].tasks_succeeded) for vid in result.variant_ids],
            None,
        )
    )
    rows.append(_row("Failed", [str(result.variant_aggregates[vid].tasks_failed) for vid in result.variant_ids], None))
    rows.append(_row("Errors", [str(result.variant_aggregates[vid].tasks_error) for vid in result.variant_ids], None))

    def _success_rate(vid: str) -> str:
        agg = result.variant_aggregates[vid]
        evaluable = agg.tasks_run - agg.tasks_error
        rate = (agg.tasks_succeeded / evaluable * 100) if evaluable > 0 else 0.0
        return f"{rate:.1f}%"

    rows.append(_row("Success Rate", [_success_rate(vid) for vid in result.variant_ids], None))

    rows.append(
        _row(
            "Score",
            [fmt_mean_sd(series[vid]["scores"]) for vid in result.variant_ids],
            fmt_p(welch_t_test(series[vid_a]["scores"], series[vid_b]["scores"])) if show_p else None,
        )
    )
    rows.append(
        _row(
            "Duration (s)",
            [fmt_mean_sd(series[vid]["durations"], ".1f") for vid in result.variant_ids],
            fmt_p(welch_t_test(series[vid_a]["durations"], series[vid_b]["durations"])) if show_p else None,
        )
    )
    if any(series[vid]["asst_turns"] for vid in result.variant_ids):
        rows.append(
            _row(
                "Assistant Turns",
                [fmt_mean_sd(series[vid]["asst_turns"], ".1f") for vid in result.variant_ids],
                fmt_p(welch_t_test(series[vid_a]["asst_turns"], series[vid_b]["asst_turns"])) if show_p else None,
            )
        )
    if any(series[vid]["tokens"] for vid in result.variant_ids):
        rows.append(
            _row(
                "Tokens",
                [fmt_mean_sd(series[vid]["tokens"], ",.0f") for vid in result.variant_ids],
                fmt_p(welch_t_test(series[vid_a]["tokens"], series[vid_b]["tokens"])) if show_p else None,
            )
        )

    return f"""
<h2>Aggregate Metrics</h2>
<div class="card" style="padding:0">
  <table>
    <thead>{header_html}</thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>
"""


def _experiment_win_rates(result: ExperimentResult) -> str:
    """Render the Win Rates bullet list."""
    if not result.task_summaries:
        return ""
    total = len(result.task_summaries)
    win_counts: dict[str, int] = {vid: 0 for vid in result.variant_ids}
    tie_count = 0
    for ts in result.task_summaries:
        if ts.is_tie:
            tie_count += 1
        else:
            win_counts[ts.best_variant] = win_counts.get(ts.best_variant, 0) + 1
    items = []
    for vid in result.variant_ids:
        wins = win_counts.get(vid, 0)
        pct = wins / total * 100 if total else 0.0
        items.append(f"<li><strong>{_esc(vid)}</strong>: {wins}/{total} tasks ({pct:.0f}%)</li>")
    if tie_count > 0:
        pct = tie_count / total * 100
        items.append(f"<li><strong>Ties</strong>: {tie_count}/{total} tasks ({pct:.0f}%)</li>")
    return f"""
<h2>Win Rates</h2>
<div class="card">
  <ul>{"".join(items)}</ul>
</div>
"""


def _experiment_per_task_comparison(result: ExperimentResult) -> str:
    """Render the Per-Task Comparison table."""
    if not result.task_summaries:
        return ""
    header_cells = ["<th>Task</th>"] + [f"<th>{_esc(vid)}</th>" for vid in result.variant_ids]
    header_cells += ["<th>Best</th>", "<th>Spread</th>"]
    header = "<tr>" + "".join(header_cells) + "</tr>"
    rows: list[str] = []
    for ts in result.task_summaries:
        scores_by_variant = {vr.variant_id: vr for vr in ts.variant_results}
        cells = [f"<td class='mono'>{_esc(ts.task_id)}</td>"]
        for vid in result.variant_ids:
            vr = scores_by_variant.get(vid)
            if vr is None:
                cells.append("<td>N/A</td>")
            else:
                cells.append(f"<td>{vr.weighted_score:.3f} ({_esc(vr.final_status.icon)})</td>")
        best = "TIE" if ts.is_tie else ts.best_variant
        cells.append(f"<td>{_esc(best)}</td>")
        cells.append(f"<td>{ts.score_spread:.3f}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"""
<h2>Per-Task Comparison</h2>
<div class="card" style="padding:0">
  <table>
    <thead>{header}</thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>
"""


def _experiment_most_divergent(result: ExperimentResult) -> str:
    """Render the Most Divergent Tasks section — omitted when all spreads are 0."""
    if not result.task_summaries:
        return ""
    sorted_tasks = sorted(result.task_summaries, key=lambda t: t.score_spread, reverse=True)
    if not sorted_tasks or sorted_tasks[0].score_spread == 0:
        return ""
    items = [
        f"<li><strong>{_esc(ts.task_id)}</strong>: spread={ts.score_spread:.3f}, best={_esc(ts.best_variant)}</li>"
        for ts in sorted_tasks[:5]
    ]
    return f"""
<h2>Most Divergent Tasks</h2>
<div class="card">
  <ul>{"".join(items)}</ul>
</div>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class HTMLReportGenerator:
    """Generates self-contained HTML reports from coder_eval run data."""

    @staticmethod
    def generate_task_html(result: EvaluationResult) -> str:
        """Render a single task's EvaluationResult as a standalone HTML page.

        The page shows the run header, success-criteria table, per-turn
        conversation trace with tool calls, command telemetry stats, and
        error details (if any). When ``result.error_log_tail`` is set, the
        captured tail is embedded under a "Logs" disclosure in the error
        section; otherwise the renderer falls back to the legacy
        ``error_details["stack_trace"]`` so archived runs predating the tail
        capture still surface diagnostics.
        """
        groups = _group_turns_by_iteration(result.iterations or [])
        turns_html = "".join(_render_iteration_group(it, group) for it, group in groups)
        if not turns_html:
            no_turn_msg = "No turn data (agent communication failed before producing any turns)."
            turns_html = f'<div class="card"><p class="muted">{no_turn_msg}</p></div>'

        n_iterations = len(groups)
        n_attempts = len(result.iterations or [])
        has_partials = any(t.crashed for t in (result.iterations or []))
        iter_label = f"{n_iterations} iteration" + ("s" if n_iterations != 1 else "")
        if has_partials:
            attempts_label = f", {n_attempts} attempt" + ("s" if n_attempts != 1 else "")
            trace_count = f"{iter_label}{attempts_label}"
        else:
            trace_count = iter_label

        body = (
            _render_header(result)
            + _render_simulation(result)
            + _render_criteria(result.success_criteria_results or [])
            + _render_judge_section(result.success_criteria_results or [])
            + _render_error_details(result)
            + f"<h2>Conversation Trace ({trace_count})</h2>"
            + turns_html
            + _render_command_stats(result.command_stats)
            + _render_generation_metrics(result)
            + _render_token_usage(result)
            + _render_commands_efficiency(result)
            + _render_agent_settings(result)
            + _render_installed_tools(result)
            + _render_environment(result)
        )
        title = f"coder_eval · {result.task_id} · {result.variant_id}"
        return _wrap_document(title, body)

    @staticmethod
    def generate_variant_html(
        variant_id: str,
        agg: VariantAggregate,
        task_links: list[tuple[str, str, float | None, str]],
        result: ExperimentResult | None = None,
        run_dir: Path | None = None,
    ) -> str:
        """Render a variant-level summary page linking to per-task HTMLs.

        Args:
            variant_id: The variant identifier.
            agg: VariantAggregate with summary metrics.
            task_links: List of (task_id, relative_html_path, score, status).
            result: Optional full ExperimentResult — enables stddev rows.
            run_dir: Optional top-level run dir — enables Generation Metrics,
                Token Usage, Command Telemetry, Agent Settings, Installed
                Tools, and Environment sections loaded from per-task JSON.
        """
        rows = "".join(
            f"""
<tr>
  <td><a href="{_esc(link)}">{_esc(tid)}</a></td>
  <td style="text-align:center">{_score_pill(score) if score is not None else "—"}</td>
  <td>{_status_badge(status)}</td>
</tr>
"""
            for tid, link, score, status in task_links
        )
        stddev_lines = _variant_stddev_lines(variant_id, result)
        rich_sections = _variant_rich_sections(variant_id, result, run_dir)
        budget_stats = ""
        if agg.tasks_token_budget_exceeded > 0:
            budget_stats += (
                f'<div class="stat"><div class="label">Token Budget</div>'
                f'<div class="value">{agg.tasks_token_budget_exceeded}</div></div>'
            )
        if agg.tasks_cost_budget_exceeded > 0:
            budget_stats += (
                f'<div class="stat"><div class="label">Cost Budget</div>'
                f'<div class="value">{agg.tasks_cost_budget_exceeded}</div></div>'
            )
        body = f"""
<div class="header-bar">
  <div class="title-group">
    <h1>Variant: {_esc(variant_id)}</h1>
    <div class="subtitle">Aggregate summary for variant <code>{_esc(variant_id)}</code></div>
  </div>
  <div class="badges">
    {_score_pill(agg.average_score, suffix=" avg")}
    <span class="nav-toggle" onclick="toggleTheme()">Toggle theme</span>
  </div>
</div>
<div class="card">
  <div class="grid">
    <div class="stat"><div class="label">Tasks Run</div><div class="value">{agg.tasks_run}</div></div>
    <div class="stat"><div class="label">Succeeded</div><div class="value">{agg.tasks_succeeded}</div></div>
    <div class="stat"><div class="label">Failed</div><div class="value">{agg.tasks_failed}</div></div>
    <div class="stat"><div class="label">Errors</div><div class="value">{agg.tasks_error}</div></div>
    {budget_stats}
  </div>
  {stddev_lines}
</div>
<h2>Tasks</h2>
<div class="card" style="padding:0">
<table>
  <thead><tr><th>Task</th><th style="width:80px">Score</th><th style="width:140px">Status</th></tr></thead>
  <tbody>{rows or '<tr><td colspan="3" class="muted">No tasks</td></tr>'}</tbody>
</table>
</div>
{rich_sections}
"""
        return _wrap_document(f"Variant {variant_id}", body)

    @staticmethod
    def generate_experiment_html(
        result: ExperimentResult,
        experiment: ExperimentDefinition | None = None,
        variant_links: list[tuple[str, str]] | None = None,
    ) -> str:
        """Render an experiment-level index linking to each variant's page.

        Matches ``ExperimentReportGenerator.generate_experiment_report`` in
        content: header, optional Prompt Configuration, Aggregate Metrics,
        Win Rates, Per-Task Comparison, Most Divergent Tasks.
        """
        exp_name = experiment.experiment_id if experiment else result.experiment_id
        exp_desc = experiment.description if experiment else ""

        rows: list[str] = []
        for vid in result.variant_ids:
            agg = result.variant_aggregates.get(vid)
            link = next((ln for v, ln in (variant_links or []) if v == vid), f"{vid}/variant.html")
            if agg is None:
                continue
            rows.append(
                f"""
<tr>
  <td><a href="{_esc(link)}">{_esc(vid)}</a></td>
  <td style="text-align:center">{_score_pill(agg.average_score)}</td>
  <td>{agg.tasks_succeeded}/{agg.tasks_run}</td>
</tr>
"""
            )
        body = (
            f"""
<div class="header-bar">
  <div class="title-group">
    <h1>Experiment: {_esc(exp_name)}</h1>
    <div class="subtitle">{_esc(exp_desc)}</div>
  </div>
  <div class="badges">
    <span class="badge neutral">{len(result.variant_ids)} variants</span>
    <span class="badge neutral">{_esc(_format_duration(result.total_duration_seconds))}</span>
    <span class="nav-toggle" onclick="toggleTheme()">Toggle theme</span>
  </div>
</div>
<h2>Variants</h2>
<div class="card" style="padding:0">
<table>
  <thead><tr><th>Variant</th><th style="width:100px">Avg Score</th><th style="width:120px">Pass Ratio</th></tr></thead>
  <tbody>{"".join(rows) or '<tr><td colspan="3" class="muted">No variants</td></tr>'}</tbody>
</table>
</div>
"""
            + _experiment_prompt_config(experiment, result.variant_ids)
            + _experiment_aggregate_metrics(result)
            + _experiment_win_rates(result)
            + _experiment_per_task_comparison(result)
            + _experiment_most_divergent(result)
        )
        return _wrap_document(f"Experiment {exp_name}", body)


def safe_write(generate: Callable[[], str], output_path: Path, *, label: str) -> Path | None:
    """Render and write an HTML report. Log and return None on failure.

    Used by the orchestrator and experiment writers so a render bug in one
    report never masks the underlying run outcome. The CLI regen path
    surfaces the ``None`` return as a per-task failure.
    """
    try:
        html_text = generate()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html_text, encoding="utf-8")
        return output_path
    except Exception:
        logger.exception("Failed to generate %s", label)
        return None


def write_task_html(result: EvaluationResult, output_path: Path) -> Path | None:
    """Render a per-task HTML report via ``safe_write``.

    The renderer is a pure function of ``result``; the captured
    ``error_log_tail`` (if any) drives the error section's "Logs" disclosure.
    """
    return safe_write(
        lambda: HTMLReportGenerator.generate_task_html(result),
        output_path,
        label=f"task.html for {result.task_id}",
    )


def write_variant_html(
    variant_id: str,
    agg: VariantAggregate,
    task_links: list[tuple[str, str, float | None, str]],
    output_path: Path,
    *,
    result: ExperimentResult | None = None,
    run_dir: Path | None = None,
) -> Path | None:
    """Render a per-variant HTML report via ``safe_write``.

    When ``result`` and ``run_dir`` are provided, the page also includes
    per-variant Generation Metrics, Token Usage, Command Telemetry, Agent
    Settings, Installed Tools, and Environment sections.
    """
    return safe_write(
        lambda: HTMLReportGenerator.generate_variant_html(variant_id, agg, task_links, result, run_dir),
        output_path,
        label=f"variant.html for {variant_id}",
    )


def write_experiment_html(
    result: ExperimentResult,
    experiment: ExperimentDefinition | None,
    variant_links: list[tuple[str, str]] | None,
    output_path: Path,
) -> Path | None:
    """Render a per-experiment HTML report via ``safe_write``."""
    return safe_write(
        lambda: HTMLReportGenerator.generate_experiment_html(result, experiment, variant_links),
        output_path,
        label=f"experiment.html for {result.experiment_id}",
    )
