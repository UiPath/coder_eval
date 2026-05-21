"""CE013: Don't regex-parse agent transcripts inside ``evaluation/`` or ``criteria/``.

Replacing text-with-JSON verdict parsing with a typed tool channel was the
whole point of the 2026-05-20 judge refactor. The legacy parser used regex
against structural-tag patterns (``[ASSISTANT]``, ``[RESULT - …]``) and
JSON-shape literals (``\\{``, ``"score"``, ``"rationale"``) — both highly
coupled to ``ClaudeCodeAgent._format_messages`` rendering choices that
have no business being a correctness contract.

This rule prevents that pattern from coming back. Inside ``evaluation/``
or ``criteria/``, any call to ``re.compile`` / ``re.search`` / ``re.match`` /
``re.findall`` / ``re.finditer`` / ``re.fullmatch`` whose first positional
argument is a string literal containing one of:

  * ``[ASSISTANT]``, ``[RESULT``, ``[TOOL USE]`` (structural transcript tags)
  * ``"score"``, ``"rationale"`` (JSON-shape verdict fields)
  * ``\\{`` followed by a verdict-shape hint (loose: literal ``\\{`` with
    one of the above markers anywhere in the pattern)

…is flagged. Use the verdict tool channel instead.

Add ``# noqa: CE013 -- <reason>`` for legitimate non-correctness-critical
sites (e.g. log scrubbing, telemetry-only utterance extraction).
"""

import ast

from tests.lint.rules.base import BaseRule


_RE_FUNCS = frozenset({"compile", "search", "match", "findall", "finditer", "fullmatch"})

_TRANSCRIPT_MARKERS = (
    "[ASSISTANT]",
    "[RESULT",
    "[TOOL USE]",
    '"score"',
    "'score'",
    '"rationale"',
    "'rationale'",
)


def _is_re_call(node: ast.Call) -> bool:
    """Match ``re.compile(...)`` / ``re.search(...)`` etc."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _RE_FUNCS:
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "re"


def _first_arg_is_transcript_pattern(node: ast.Call) -> bool:
    """True if ``node.args[0]`` is a string literal containing a transcript marker."""
    if not node.args:
        return False
    first = node.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return False
    pattern = first.value
    return any(marker in pattern for marker in _TRANSCRIPT_MARKERS)


def _file_is_in_scope(filepath: str) -> bool:
    """Limit the rule to ``src/coder_eval/evaluation/`` and ``src/coder_eval/criteria/``."""
    # Normalize Windows backslashes so substring checks work cross-platform.
    norm = filepath.replace("\\", "/")
    return "/coder_eval/evaluation/" in norm or "/coder_eval/criteria/" in norm


class NoTranscriptRegexInEval(BaseRule):
    id = "CE013"

    def visit_Call(self, node: ast.Call) -> None:
        if _file_is_in_scope(self.filepath) and _is_re_call(node) and _first_arg_is_transcript_pattern(node):
            self.violation(
                node,
                "regex-parsing agent transcripts is forbidden in evaluation/ and criteria/ — "
                "use the submit_verdict tool channel instead (see verdict_tool.py)",
            )
        self.generic_visit(node)
