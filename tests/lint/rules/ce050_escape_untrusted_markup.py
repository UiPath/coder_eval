"""CE050: escape interpolated values in a Rich-markup ``console.print`` under ``src/coder_eval/cli/``.

Rich reads ``[...]`` in the string it is handed as MARKUP, not as text. So a value carrying square
brackets — a task id, an exception message, a ``run.json`` fragment, an agent's own output — does
not render as itself. ``[bold]`` disappears; an unclosed ``[`` swallows the rest of the line; a
stray ``[/red]`` closes a tag the program opened. The failure is a corrupted or *missing*
diagnostic, and it lands precisely when something has already gone wrong and the message matters
most.

``cli/aggregate_command.py`` shipped two of these on the degrade path, interpolating raw
``run.json`` values straight into a ``[yellow]…[/yellow]`` span, and ``cli/plan_command.py``
another dozen over task ids, variant ids, model names and exception text.

This is not an injection guard. Task YAML is author-controlled and the values are not attacker
supplied in any deployment this project has; the severity is a mangled message, not RCE. It is a
correctness rule about output.

**What it detects, precisely.** An ``ast.JoinedStr`` (an f-string) passed as an argument to ANY
call, in a file whose normalized path contains ``src/coder_eval/cli/``, where the f-string contains
BOTH a literal Rich markup tag (``[name]`` or ``[/name]`` in one of its constant parts) AND at
least one interpolation that is not escaped.

**Through ``+`` chains, and any call — not just ``console.print``.** The SINK is an
implementation detail, and keying on it fails open the moment output is buffered. Measured: the
same change that added this rule also moved ``plan``'s per-file output behind an ``emit`` sink and
a ``detail.append`` list, so five
markup-bearing f-strings in ``plan_command.py`` left the rule's view on the very file it was
written for. A Rich markup tag in a ``cli/`` f-string is markup because it reaches a console
eventually; where it is handed off on the way does not change that.

Concatenated operands are flattened before the check, because ``+``-joined multi-line strings are
this codebase's dominant style (pyright forbids implicit adjacent-string concatenation here) and
Rich parses the JOINED result as one markup string. Measured: two live unescaped sites —
``run_command.py``'s ``--resume`` config-drift warning and ``aggregate_command.py``'s summary line
— sat unflagged because the markup tag and the raw interpolation were in different operands.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof.**

* It flattens ``+`` chains but resolves nothing else: ``.format()``, ``%`` and ``*args`` are not
  matched.
* It matches f-strings **at the call site**. ``msg = f"[red]{e}[/red]"`` followed by
  ``console.print(msg)`` is invisible to it, because the rule never resolves a name to its value.
  It does follow ONE hop in the other direction: a local assigned directly from ``escape(...)``
  within the same function counts as escaped, so hoisting an escape out of a long line — which is
  ordinary formatting, not a trust decision — does not cost a suppression.
* It does not decide whether a given value is actually untrusted — it cannot, without knowing
  where the value came from. A genuinely trusted interpolation (a literal from this module, a
  formatted count) needs ``# noqa: CE050`` with a reason. That is the rule working: it forces the
  author to answer *could this value contain a bracket?* rather than never asking.
* It only fires when a markup tag is present in the same f-string. An f-string with no tags is
  passed through by Rich unchanged, so there is nothing to escape.
* It checks that the interpolation is *wrapped in a call named* ``escape``; it does not verify
  that the callee is ``rich.markup.escape``.
* Scope is ``src/coder_eval/cli/`` only, mirroring CE047's single-directory scoping and for the
  same reason: everything else in ``src/`` reports through ``logging``, where markup is never
  interpreted, so the rule would be noise there.

The fix: ``from rich.markup import escape`` and wrap the interpolated value —
``f"[yellow]dropped {escape(str(entry))}[/yellow]"``. Escape the VALUE only; the program's own
``[yellow]`` tags are markup on purpose and must not be escaped.
"""

import ast
import re

from tests.lint.rules.base import BaseRule
from tests.lint.violation import Violation


_CLI_PACKAGE = "src/coder_eval/cli/"

# A Rich markup tag, matching what Rich ACTUALLY parses — verified against the installed rich
# rather than inferred. An OPENING tag's first character must be lowercase, `#` or `@`, so
# `[Errno 66]` and `[0]` print literally and are not this rule's shape. A CLOSING tag is `[/`
# followed by anything, and an unmatched one RAISES `MarkupError` rather than rendering wrong —
# so an unescaped value carrying `[/whatever]` does not corrupt the diagnostic, it crashes the
# command that was trying to print it.
_MARKUP_TAG = re.compile(r"\[/[^\[\]]*\]|\[[a-z#@][^\[\]]*\]")

_FIX = (
    "this f-string carries Rich markup AND an unescaped interpolation. Rich reads `[...]` in the "
    "VALUE as markup too, so a task id, exception message or run.json fragment containing a "
    "bracket renders wrong or vanishes. Wrap the value: `from rich.markup import escape` then "
    "`{escape(str(value))}`. Escape the value only — the literal [tags] are the program's own. "
    "`# noqa: CE050` with a reason if the value genuinely cannot contain a bracket."
)


def _string_operands(node: ast.expr) -> list[ast.JoinedStr | ast.Constant]:
    """Every f-string / literal operand of a ``+`` chain, flattened; the node itself otherwise.

    Rich parses the JOINED result as one markup string, so a tag in one operand governs an
    interpolation in another. Checking each operand alone is what let two live sites through.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_operands(node.left) + _string_operands(node.right)
    return [node] if isinstance(node, ast.JoinedStr | ast.Constant) else []


def _has_markup(parts: list[ast.JoinedStr | ast.Constant]) -> bool:
    constants = [
        value
        for part in parts
        for value in (
            [part.value]
            if isinstance(part, ast.Constant)
            else [piece.value for piece in part.values if isinstance(piece, ast.Constant)]
        )
    ]
    return any(isinstance(value, str) and _MARKUP_TAG.search(value) for value in constants)


def _is_escape_call(value: ast.expr) -> bool:
    """True when the expression is a call to something named ``escape``.

    Reads the callee NAME, so `escape(x)` and `markup.escape(x)` both count. It does not resolve
    the import, which the module docstring states as a boundary.
    """
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Name):
        return func.id == "escape"
    return isinstance(func, ast.Attribute) and func.attr == "escape"


def _escaped_locals(tree: ast.AST) -> set[str]:
    """Names assigned directly from ``escape(...)``, per enclosing scope, flattened.

    Hoisting `escape(str(x))` into a local to keep a line under the length limit is formatting,
    not a trust decision, and charging a `# noqa` for it teaches the reader that the suppression
    is bookkeeping. Flattened rather than scope-accurate on purpose: the cost of the imprecision
    is a missed violation where two functions in one file reuse a name for different things, which
    is far cheaper than a false one — and `cli/` does not do that.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_escape_call(node.value):
            names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_escape_call(node.value)
            and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)
    return names


# Format specs that only a number accepts. `str.format` raises on a non-numeric value for every
# one of these, so an interpolation carrying one cannot render a bracket.
_NUMERIC_SPEC = re.compile(r"[bcdeEfFgGnoxX%]$|,")


def _is_numeric(part: ast.FormattedValue) -> bool:
    """True when this interpolation provably cannot contain a bracket.

    Three shapes, each decidable from the AST alone and each covering a large share of the CLI's
    interpolations — counts, durations, costs, percentages:

    * a numeric format spec (``{n:.2f}``, ``{n:,}``, ``{n:d}``);
    * a call to ``len(...)``, ``round(...)``, ``sum(...)``, ``int(...)``, ``float(...)``;
    * a numeric literal;
    * arithmetic over any of the above — ``{len(a) - len(b)}`` is a count, and charging a
      suppression for it would teach the reader that ``noqa`` is bookkeeping.

    Everything else is treated as possibly-bracketed, including a bare name holding an int. That
    asymmetry is deliberate: the rule cannot infer types, and the cost of being wrong in this
    direction is one ``escape()`` on a number (harmless) rather than a corrupted diagnostic.
    """
    spec = part.format_spec
    if isinstance(spec, ast.JoinedStr):
        text = "".join(p.value for p in spec.values if isinstance(p, ast.Constant) and isinstance(p.value, str))
        if text and _NUMERIC_SPEC.search(text):
            return True
    return _is_numeric_expr(part.value)


_NUMERIC_CALLS = frozenset({"len", "round", "sum", "int", "float", "abs", "min", "max"})


def _is_numeric_expr(value: ast.expr) -> bool:
    if isinstance(value, ast.BinOp):
        return _is_numeric_expr(value.left) and _is_numeric_expr(value.right)
    if isinstance(value, ast.UnaryOp):
        return _is_numeric_expr(value.operand)
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return value.func.id in _NUMERIC_CALLS
    return isinstance(value, ast.Constant) and isinstance(value.value, int | float)


class EscapeUntrustedMarkup(BaseRule):
    id = "CE050"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        # Normalized so the match works on Windows runners, where pathlib hands the rule
        # native-separator strings (the convention CE047 and CE009 already follow).
        self._active = _CLI_PACKAGE in filepath.replace("\\", "/")
        self._escaped: set[str] = set()

    def check(self, tree: ast.AST) -> list[Violation]:
        # The escaped-locals set is a whole-file fact, so it has to be collected before the walk
        # rather than during it — an assignment can follow the call that uses it.
        if self._active:
            self._escaped = _escaped_locals(tree)
        return super().check(tree)

    def _is_escaped(self, value: ast.expr) -> bool:
        return _is_escape_call(value) or (isinstance(value, ast.Name) and value.id in self._escaped)

    def visit_Call(self, node: ast.Call) -> None:
        if self._active:
            for arg in node.args:
                operands = _string_operands(arg)
                if not _has_markup(operands):
                    continue
                for operand in operands:
                    if not isinstance(operand, ast.JoinedStr):
                        continue
                    for part in operand.values:
                        if isinstance(part, ast.FormattedValue) and not (
                            self._is_escaped(part.value) or _is_numeric(part)
                        ):
                            self.violation(part, _FIX)
        self.generic_visit(node)
