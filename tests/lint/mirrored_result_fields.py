"""CE058's reader — a mirrored ``CriterionResult`` field must be assigned at the checker's seam.

``CriterionResult`` mirrors fields from the criterion that produced it: ``pass_threshold``,
``gating`` and now ``weight``. All three are stamped in ``evaluation/checker.py`` — by attribute
assignment in ``SuccessChecker._finalize_result`` and as keyword arguments in
``_missing_checker_result`` / ``_error_result``. A fourth mirrored field added to the model and not
stamped there is **silent**: every result carries the field's default, nothing raises, no type
checker complains, and every consumer believes the value is real. ``weight`` is the field that
makes this a recurring class rather than a one-off, and it is read by the execution gate's
dead-weight computation, so a default of ``None`` there reads as "not recorded" on a run that
recorded everything else.

**Class-wired, not a ``BaseRule``, and that is structural.** ``BaseRule.__init__`` takes a single
``filepath`` and the class is an ``ast.NodeVisitor`` over that one file, while this predicate spans
**two**: a field description in ``models/results.py`` and an assignment in
``evaluation/checker.py``. ``CLAUDE.md`` states the rule for that case, and CE057 is the precedent
followed here exactly — the detection body is this shared reader beside ``skip_guards.py`` and
``outcome_prompt_leak.py``, the rule itself is a ``@pytest.mark.lint`` class in
``tests/test_custom_lint.py``, nothing is added under ``tests/lint/rules/`` (that directory holds
``BaseRule`` modules) and ``runner.py`` is untouched, since its id-uniqueness assert covers
``ALL_RULES`` alone.

The model side is read by **runtime introspection** of ``CriterionResult.model_fields`` — the CE063
precedent, because the question is about resolved field metadata rather than source shape — and the
checker side by an AST scan.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof:**

- It matches the field's **description text**. A mirrored field whose description does not contain
  :data:`MIRROR_MARKER` is invisible to it, which is why the rule's own test asserts the detected
  set is non-empty and names all three of today's fields.
- It checks that an assignment **exists**, never that the assigned value is right. Stamping
  ``result.weight = criterion.pass_threshold`` passes.
- It is scoped to ``CriterionResult`` **itself, not its subclasses**, and the honest version of
  that is worth stating: no subclass field says ``"mirrors "`` today, so an unscoped rule would
  detect nothing extra *right now* — the scoping is a decision about the future, not a live
  suppression. A subclass field is produced by the criterion that computed it
  (``observed_label``, ``findings``, ``transcript``), and no seam could stamp one, so a subclass
  field that ever did claim to mirror something would need its own answer rather than this rule's.
- The three functions in :data:`STAMPING_FUNCTIONS` are required **each**, not any-of. A field
  assigned only in ``_finalize_result`` is exactly the defect this exists to catch: it defaults
  silently on the two error paths, which build a result for a criterion no checker ran.
- Within one of those functions, a keyword of the right NAME on any call counts — so
  ``logger.log(msg, weight=3)`` would satisfy it — and ``ast.walk`` is depth-blind, so a
  same-named function nested inside an unrelated one, or on a second class, also counts. Both are
  false negatives accepted for the same reason the value is not checked: the rule asks whether the
  author thought about the field, not whether they got it right.
- A DRY ``setattr(result, name, ...)`` loop over the mirrored fields is a false POSITIVE — the
  names are not literals there. That shape would need a ``# noqa``, which is the rule working: it
  forces the author to say the loop covers all three paths.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

from coder_eval.models import CriterionResult


# The phrase a mirrored field's description carries. All three of today's say "mirrors
# BaseSuccessCriterion.<name>"; keying on the verb rather than the class name keeps a field
# mirrored from somewhere else in scope.
MIRROR_MARKER = "mirrors "

# The functions that may satisfy the requirement: the stamping seam plus the two construction
# sites that cannot route through it, because they build a result for a criterion no checker ran.
STAMPING_FUNCTIONS = ("_finalize_result", "_missing_checker_result", "_error_result")


class MirrorGap(NamedTuple):
    """One mirrored field, and the stamping functions that do not assign it."""

    field: str
    missing_from: tuple[str, ...]


def mirrored_fields() -> list[str]:
    """Every ``CriterionResult`` field whose description says it mirrors something, in model order.

    Read off ``model_fields`` rather than parsed out of the source: the question is what the model
    resolves to, and a description assembled from a constant or a shared prefix is still the
    description a reader gets.
    """
    return [
        name
        for name, field in CriterionResult.model_fields.items()
        if field.description and MIRROR_MARKER in field.description
    ]


def stamped_fields(checker_source: Path) -> dict[str, set[str]]:
    """Field names assigned, PER stamping function, by either accepted shape.

    Two shapes, because the sites have two: ``result.<field> = ...`` inside ``_finalize_result``, and
    ``<field>=...`` as a keyword to the ``CriterionResult(...)`` calls in the two error paths. A
    keyword is counted wherever it appears in those functions rather than only on a
    ``CriterionResult`` call, so a helper renamed or wrapped does not silently stop counting.

    **Per function, not unioned**, and that is the correctness of the rule rather than a detail: a
    union is satisfied by a stamp in any ONE of the three, which is precisely the shape that leaves a
    mirrored field defaulting on both error paths with nothing raised.

    A function named in :data:`STAMPING_FUNCTIONS` but absent from the file maps to no key, so the
    caller reports it as missing everything rather than passing over it.
    """
    tree = ast.parse(checker_source.read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name not in STAMPING_FUNCTIONS:
            continue
        names = found.setdefault(node.name, set())
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign):
                names |= {t.attr for t in inner.targets if isinstance(t, ast.Attribute)}
            elif isinstance(inner, ast.Call):
                names |= {kw.arg for kw in inner.keywords if kw.arg is not None}
    return found


def gaps(checker_source: Path) -> tuple[list[MirrorGap], list[str]]:
    """``(mirrored fields some stamping function never assigns, every mirrored field seen)``.

    The second element is what a caller asserts non-empty: a renamed description convention makes
    the first empty for the wrong reason, and a rule that reports "no violations" because it can no
    longer see its subject is the vacuous pass CE044 and CE045 were written after.
    """
    fields = mirrored_fields()
    stamped = stamped_fields(checker_source)
    found: list[MirrorGap] = []
    for name in fields:
        missing = tuple(fn for fn in STAMPING_FUNCTIONS if name not in stamped.get(fn, set()))
        if missing:
            found.append(MirrorGap(field=name, missing_from=missing))
    return found, fields
