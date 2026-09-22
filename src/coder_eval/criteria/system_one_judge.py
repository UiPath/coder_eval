"""System-One-as-a-judge success criterion checker."""

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.evaluation.judge_context import (
    JudgeContext,
    JudgeContextBuilder,
    build_judge_transcript,
    format_details,
    scrub_reference,
)
from coder_eval.evaluation.judge_system_one import invoke_system_one_async
from coder_eval.evaluation.judge_usage import token_usage_from_anthropic_dict
from coder_eval.evaluation.system_one_scoring import build_questions_payload, reduce_answers
from coder_eval.models import (
    CriterionResult,
    JudgeCriterionResult,
    SystemOneJudgeCriterion,
)


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class SystemOneJudgeChecker(BaseCriterion[SystemOneJudgeCriterion]):
    """Checker for SystemOneJudgeCriterion — grades the task via a typed rubric."""

    criterion_type = "system_one_judge"

    async def _check_impl_async(
        self,
        criterion: SystemOneJudgeCriterion,
        sandbox: "Sandbox",
        *,
        turn_records: "list[TurnRecord] | None" = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        ctx = context or CheckContext()

        # Master enablement gate. A skipped criterion makes no API call and scores
        # 1.0; to exclude it from the weighted score, remove it or use a variant.
        if not criterion.enabled:
            return JudgeCriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=1.0,
                details="(skipped: enabled=false)",
            )

        # .build() does synchronous file I/O -- offload it so it does not stall the
        # event loop this native-async checker otherwise never blocks.
        judge_ctx = await asyncio.to_thread(
            JudgeContextBuilder(
                files=criterion.files,
                include_reference=criterion.include_reference,
                include_agent_output=criterion.include_agent_output,
                include_tool_calls=criterion.include_tool_calls,
                include_dialog=criterion.include_dialog,
                max_dialog_chars=criterion.max_dialog_chars,
                max_file_chars=criterion.max_file_chars,
            ).build,
            sandbox,
            ctx.reference_dir,
            turn_records,
        )

        # HAZARD: per-FILE contents, and taken from the CONTEXT rather than
        # recomputed from `include_reference` -- gating on the flag left a
        # `$REFERENCE_DIR/...` entry unscrubbed in the archived transcript.
        # Rationale: .claude/notes/contracts.md § What counts as reference-derived
        scrub_key = judge_ctx.reference_secrets or None

        state = _build_state(criterion.prompt, judge_ctx, criterion.max_state_chars)
        response = await invoke_system_one_async(
            base_url=criterion.base_url,
            api_key=os.environ.get(criterion.api_key_env, ""),
            model=criterion.model,
            state=state,
            questions=build_questions_payload(criterion.questions),
            timeout_seconds=criterion.timeout_seconds,
        )

        answers = response.get("answers")
        verdict = reduce_answers(
            criterion.questions, answers if isinstance(answers, dict) else {}, mode=criterion.scoring
        )
        judge_usage = token_usage_from_anthropic_dict(response, model=criterion.model)

        transcript = None
        if criterion.capture_transcript:
            transcript = build_judge_transcript(
                raw_verdict=json.dumps(answers, indent=2, default=str),
                max_chars=criterion.max_transcript_chars,
                judge_system_prompt=_rubric_digest(criterion),
                judge_prompt=json.dumps(state, indent=2, default=str),
                token_usage=judge_usage,
                scrub_key=scrub_key,
            )

        details = format_details(verdict.score, verdict.rationale, judge_ctx.missing_files, judge_ctx.degraded_notes)
        return JudgeCriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=verdict.score,
            details=scrub_reference(details, scrub_key),
            findings=[scrub_reference(f, scrub_key) for f in verdict.findings],
            transcript=transcript,
            token_usage=judge_usage,
        )


def _rubric_digest(criterion: SystemOneJudgeCriterion) -> str:
    """The rubric as sent, for the transcript's system-prompt slot.

    A System One judge has no system prompt — the questions ARE the instruction,
    so this is what a reviewer needs in that slot to replay the grade.
    """
    return json.dumps(build_questions_payload(criterion.questions), indent=2)


def _build_state(prompt: str, context: JudgeContext, max_section_chars: int) -> dict[str, Any]:
    """Render the judge context as the typed state object the model reads.

    A structured state, not a prose envelope: a System One model reads fields, and
    the questions carry the instruction, so there is no header wording to get right
    and no 'ignore instructions below' framing to hold. Each section is capped
    independently so one huge artifact cannot crowd the others out of the context.
    """
    state: dict[str, Any] = {}
    if prompt:
        state["context"] = prompt[:max_section_chars]

    state["files"] = {
        f.path: (f.content if f.content is not None else "<file not found>")[:max_section_chars] for f in context.files
    }
    if context.reference is not None:
        state["reference_solution"] = context.reference[:max_section_chars]
    if context.agent_output is not None:
        state["agent_output"] = context.agent_output[:max_section_chars]
    if context.tool_calls_summary is not None:
        state["agent_tool_calls"] = context.tool_calls_summary[:max_section_chars]
    if context.dialog:
        state["dialog"] = [
            {"turn": i, "user": user[:max_section_chars], "agent": agent[:max_section_chars]}
            for i, (user, agent) in enumerate(context.dialog, 1)
        ]
    return state
