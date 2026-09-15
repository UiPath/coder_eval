"""CE058: an unknown timing value may not become a numeric literal.

In ``src/coder_eval/``, ``None`` means *never timed*; ``0.0`` means *timed,
instant*. Timing fields match ``_TIMING_NAME`` (``duration_ms``, command-time
pair, ``*_duration_ms``, ``*_startup_ms``/``*_teardown_ms``, ``*_union_ms``);
constructors, ``_TIMING_CONSTRUCTORS``. Six forms:

1. a zero keyword on a timing constructor;
2. ``duration_ms or 0``;
3. ``x if x is not None else 0.0`` (and the mirror);
4. ``if x.duration_ms is None: x.duration_ms = <number>``;
5. ``model_copy(update={"duration_ms": 0.0})``;
6. ``cmd.duration_ms = 0.0`` with no ``is None`` guard naming it.

Forms 2-4 flag any number (each replaces an unmeasured value); forms 1, 5, 6 only
zero, since those shapes also write a real ``1234.0``. Form 6 skips form 4's hits:
``visit_If`` records them before ``generic_visit`` descends.

BLIND SPOTS: forms 1 and 6 key on spelling; renaming the
``AssistantMessageTelemetry`` alias, a rebound local, or ``setattr`` escapes.

``# noqa: CE058`` where missing genuinely means zero.

Rationale: .claude/notes/lint-rules.md § CE058
"""

import ast
import re

from tests.lint.rules.base import BaseRule


# Trailing-segment match, so `cmd.duration_ms` and `generation_duration_ms`
# fire while `duration_ms_limit` does not. The `_startup_ms` / `_teardown_ms`
# arms need a leading segment for the same reason the `_duration_ms` arm does:
# the shipped fields are `harness_*`, and a bare `startup_ms` is more likely a
# budget than a measurement.
#
# THREE field families, not two. `tool_union_ms` is the turn's third wall-clock
# bucket, on the same model and under the same None-vs-0.0 contract as the
# `harness_*` pair — and it matched NO arm above, so `TurnRecord(tool_union_ms=0.0)`
# would have been invisible even though `TurnRecord` is already in
# `_TIMING_CONSTRUCTORS`. Naming the field `tool_union_duration_ms` to inherit
# the generic `_duration_ms` arm for free was considered and rejected: the two
# fields beside it needed their own arm for exactly this reason, and one
# spelling across the four buckets is worth two lines of regex.
_TIMING_NAME = re.compile(
    r"^(duration_ms|generation_duration_ms|total_command_time_ms|avg_command_time_ms"
    r"|[a-z_]*_duration_ms|[a-z_]*_(?:startup|teardown)_ms|[a-z_]*_union_ms)$"
)

# The constructors that carry a timing field. Keying on the callee name is what
# makes the alias hazard above real; it is also the only thing an AST rule can
# see without type inference.
_TIMING_CONSTRUCTORS = frozenset(
    {"AssistantMessage", "AssistantMessageTelemetry", "CommandTelemetry", "SlowestCommandInfo", "TurnRecord"}
)

_SRC_ROOT = re.compile(r"(?:^|[/\\])src[/\\]coder_eval[/\\]")


def _timing_name(node: ast.expr) -> str | None:
    """The name of a timing-looking expression, or None."""
    if isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.Name):
        name = node.id
    else:
        return None
    return name if _TIMING_NAME.match(name) else None


def _numeric_literal(node: ast.expr) -> bool:
    """Any numeric constant — the fallback in forms 2-4, where ANY invented
    number for an unmeasured value is the defect, not just zero."""
    return isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool)


def _zero_literal(node: ast.expr) -> bool:
    """Exactly `0` / `0.0`. Form 1 is narrower than the others on purpose: a
    constructor keyword is also how a legitimately measured value is passed
    (`generation_duration_ms=1234.0` in a test factory or a replay), so only
    the placeholder zero every real producer actually wrote is flagged."""
    return isinstance(node, ast.Constant) and _numeric_literal(node) and node.value == 0


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _none_test(test: ast.expr) -> tuple[ast.expr, bool] | None:
    """(<operand>, is_none) for an `x is None` / `x is not None` comparison."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return None
    comparator = test.comparators[0]
    if not (isinstance(comparator, ast.Constant) and comparator.value is None):
        return None
    if isinstance(test.ops[0], ast.Is):
        return test.left, True
    if isinstance(test.ops[0], ast.IsNot):
        return test.left, False
    return None


def _same_target(a: ast.expr, b: ast.expr) -> bool:
    """True when two expressions name the same attribute or local.

    Compares the unparsed source rather than ``ast.dump``: the guard reads the
    name (``Load``) and the assignment writes it (``Store``), so the dumps
    differ by ``ctx`` on the very pair the rule exists to match.
    """
    return ast.unparse(a) == ast.unparse(b)


_MESSAGE = (
    "an unknown timing value is written as a numeric literal. `None` means it was never "
    "measured; a literal means it was measured and took that long — and writing the literal "
    "publishes the second while meaning the first, which is how Antigravity reported 0ms of "
    "generation on every message of every run and Codex divided avg_command_time_ms by a "
    "command count a third of which had never been timed. Keep it None, or branch on "
    "`is None`. Use `# noqa: CE058` where a missing value genuinely IS a zero, with a "
    "comment saying so."
)


class NoTimingLiteral(BaseRule):
    id = "CE058"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_scope = bool(_SRC_ROOT.search(filepath))
        #: Assignments form 4 has already flagged, so form 6 does not report
        #: the same statement a second time. Populated in `visit_If` before
        #: `generic_visit` reaches the body — see the module docstring.
        self._flagged_assigns: set[ast.Assign] = set()

    def visit_Call(self, node: ast.Call) -> None:
        # Form 1: `AssistantMessage(generation_duration_ms=0.0, ...)`
        if self._in_scope:
            if _callee_name(node) in _TIMING_CONSTRUCTORS:
                for kw in node.keywords:
                    if kw.arg is not None and _TIMING_NAME.match(kw.arg) and _zero_literal(kw.value):
                        self.violation(node, _MESSAGE)
            # Form 5. Scoped to an `update=` dict rather than any dict literal:
            # that is the `model_copy(update={...})` idiom the agents use to
            # write timing, and scoping it there keeps an unrelated
            # `{"duration_ms": 0}` fixture from firing.
            for kw in node.keywords:
                if kw.arg == "update" and isinstance(kw.value, ast.Dict):
                    self._check_update_dict(kw.value)
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # Form 2: `duration_ms or 0`
        if (
            self._in_scope
            and isinstance(node.op, ast.Or)
            and len(node.values) == 2
            and _timing_name(node.values[0]) is not None
            and _numeric_literal(node.values[1])
        ):
            self.violation(node, _MESSAGE)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        # Form 3: `x if x is not None else 0.0` (and the `is None` mirror).
        parsed = _none_test(node.test)
        if self._in_scope and parsed is not None:
            operand, is_none = parsed
            unknown_branch = node.body if is_none else node.orelse
            if _timing_name(operand) is not None and _numeric_literal(unknown_branch):
                self.violation(node, _MESSAGE)
        self.generic_visit(node)

    def _check_update_dict(self, node: ast.Dict) -> None:
        """Form 5: a timing key set to a zero literal inside an ``update=`` dict."""
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and _TIMING_NAME.match(key.value)
                and _zero_literal(value)
            ):
                self.violation(node, _MESSAGE)

    def visit_If(self, node: ast.If) -> None:
        # Form 4: `if x.duration_ms is None: x.duration_ms = 0.0`
        parsed = _none_test(node.test)
        if self._in_scope and parsed is not None:
            operand, is_none = parsed
            if is_none and _timing_name(operand) is not None:
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.Assign)
                        and len(stmt.targets) == 1
                        and _numeric_literal(stmt.value)
                        and _same_target(stmt.targets[0], operand)
                    ):
                        self._flagged_assigns.add(stmt)
                        self.violation(stmt, _MESSAGE)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Form 6: `cmd.duration_ms = 0.0` with no `is None` guard on the value
        # itself — see the module docstring for why this is narrower than
        # form 4 and why the dedupe below is ordering-safe rather than lucky.
        if (
            self._in_scope
            and node not in self._flagged_assigns
            and len(node.targets) == 1
            and _timing_name(node.targets[0]) is not None
            and _zero_literal(node.value)
        ):
            self.violation(node, _MESSAGE)
        self.generic_visit(node)
