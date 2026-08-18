#!/usr/bin/env python3
"""Score one outcome-suite row's artifact against that row's expectations. STDLIB ONLY.

    python3 /abs/path/to/outcome-grader/verify.py <row_id>

This is the execution track's measuring instrument: a CONTINUOUS grader, wired into
`outcome.yaml`'s `run_command` slot with `score_from_stdout: true`. Read the whole contract before
editing it — a grader that is subtly wrong biases every arm of an A/B, so no cross-arm comparison
can reveal it, and every number computed afterwards is decoration. (`outcome.yaml`'s grader slot
carries the argument for why the slot is continuous rather than pass/fail.)

THE OUTPUT PROTOCOL
    Line 1 is the score, a float in [0.0, 1.0]. Every later line is detail, captured verbatim into
    the criterion's `details`. ALWAYS EXIT 0, including on every failure below: coder-eval checks
    the exit code BEFORE it parses the score line, so a grader that computed 0.75 and then exited
    non-zero reports 0.0 — the work is done and thrown away.

    The LAST line is `RULES ` followed by compact JSON: rule id -> `"pass"`, `"fail"` or `"na"`.
    It is emitted on EVERY exit path, including the ones that run no check at all — a consumer
    must be able to tell "this row attributed nothing" (`RULES {}`) from "this grader predates the
    contract" (no line). Emitted inside `_report`, so no future exit path can forget it, and last
    rather than first because line 1 belongs to the score and a consumer scans from the end.

    Nothing else may write to stdout, or it lands on line 1 and the row scores 0.0 with a parse
    error. A check's `print()` cannot do this — the dispatch loop captures stdout while checks run
    and folds anything they printed into the detail lines — but a module-level `print` at import
    time is outside that guard.

RULE ATTRIBUTION
    OPTIONAL. A top-level `rules` map in the expectations file — a SIBLING of `checks`, never a key
    inside one, because a check's params are forwarded verbatim to the check function where an
    unknown key would arrive as a silent extra argument:

        "rules": {"mentions#core": "R1", "json_field": "R7"}

    Keys are check keys as written under `checks`; a bare check NAME matches every labelled
    instance of it, so `{"mentions": "R1"}` attributes `mentions#core` and `mentions#detail` alike.
    An exact key wins over the bare name.

    A rule with several checks on one row is `fail` if ANY of its checks failed, and `na` only if
    ALL of them were N/A. The direction is what makes the verdict safe rather than merely
    convenient: any-fail counts the MOST rows as failing a rule, so a headroom estimate built on it
    is an UPPER BOUND — a rule that still cannot clear the noise floor under the most generous
    attribution is definitively unpromotable, which is the only claim `/coder-eval:optimize-skill`
    Step 7's ceiling table makes. A check whose name is not in CHECKS produced no verdict at all
    and contributes to no rule.

THE SCORE
    `passed / applicable`, where `applicable` counts only the checks that returned a verdict.

    Continuity comes from the NUMBER OF CHECKS on a row: each one is pass or fail, so a row with
    one check can only score 0.0 or 1.0, which is the zero-variance shape the whole file exists to
    avoid. Write several independent checks per row. Two checks may share a name by suffixing a
    label — `mentions#headings` and `mentions#findings` are two separate `mentions` checks, and
    without the suffix the second silently replaces the first, because JSON object keys are unique.

WRITING A CHECK
    A check is `fn(doc, params) -> tuple[bool | None, str]`, registered in `CHECKS`. `doc` is the
    parsed artifact, `params` is that check's dict from the expectations file, and the string is
    one line of detail. Return `True` or `False` — any other truthy value is an error, not a
    partial credit, because the score counts `int(verdict)` while the detail line reads truthiness.

    `None` means NOT APPLICABLE: the check leaves the numerator AND the denominator.

    ***THE N/A TRIGGER MUST BE A PROPERTY OF THE ROW, NEVER OF THE ARTIFACT.*** (The plugin's
    `reference/task-rubric.md` § "Grader fairness" declares this rule for reviewers. This file
    states it in full anyway, rather than pointing at it, because it is COPIED out of the plugin:
    a rule an author cannot read from where the script now lives is a rule they will not follow.)
    This is the single
    rule most easily got wrong, and getting it wrong inverts A/B verdicts rather than merely
    biasing them. A check that returns N/A because the artifact is the wrong shape makes the
    DENOMINATOR a function of the arm's own output: an arm that ignores the requirement entirely
    drops the check and scores 1/1, while an arm that complied and got one field wrong scores 1/2.
    The worse artifact wins. So N/A means "this row declared nothing for this check to look at" —
    which is a fact about the expectations file — and an artifact that cannot answer a question the
    row DID ask is a FAIL.

    A check that RAISES is a FAILING check with the exception in its detail — never a crashed
    grader, and never a zeroed row. An unknown check name is SKIPPED and excluded from the
    denominator, so a typo cannot silently inflate a score.

    VALIDATE YOUR PARAMS. `check_mentions` below raises on an `all_of` that is a string rather than
    a list, because Python would otherwise iterate it CHARACTER BY CHARACTER and report "all
    present" against any artifact of moderate length — a silent 1.0 on every row of every arm.

    LOCATE BY CONTENT, NEVER BY POSITION. Find a section by its heading text, a value by its key.
    A check keyed on "the third line" or "column D" fails an artifact that is correct but laid out
    differently — which penalises a legitimate alternative implementation, the exact grader defect
    the discrimination gate in `/coder-eval:task` step 6.5 exists to catch.

WHERE THE FILES LIVE
    The expectations directory is resolved relative to THIS SCRIPT, and the artifact relative to
    the CURRENT WORKING DIRECTORY — which `run_command` sets to the sandbox. That asymmetry is the
    point: the grader and its expectations live OUTSIDE the fixture, and `outcome.yaml`'s `sandbox:`
    comment states why and what it measured when they did not.

    Nothing stops a `path` from pointing outside the sandbox (`../`, or an absolute path). Such a
    check reads the same file for every arm, so it contributes no signal and only dilution.

    Running it by hand (which is how the discrimination gate is performed) resolves the artifact
    against YOUR shell's cwd, so `cd` to the directory the paths in your expectations are relative
    TO — the sandbox root, not the directory the artifact itself sits in.

BEYOND TEXT AND JSON
    The sandbox venv installs only what `sandbox.python.env_packages` names, and that list is sized
    for the AGENT's needs, not the grader's — so this scaffold parses text and JSON with the
    standard library alone. A format-specific grader (spreadsheet, PDF, XML) must add its parser to
    `env_packages` AND re-run the discrimination gate afterwards: a new parser is a new way to
    misread an artifact.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any


# Separates a check's NAME from an author's label: `mentions#headings`. Without it an expectations
# file cannot declare the same check twice — JSON keeps the last of two identical keys, so the
# first vanishes with no message and the denominator silently shrinks.
LABEL_SEPARATOR = "#"


class Artifact:
    """The parsed artifact, in the two shapes a check can ask about.

    `text` is always present. `data` is the parsed JSON only when the artifact is a JSON OBJECT or
    ARRAY; a bare scalar (`7`, `"x"`, `true`, and `null` most confusingly of all) leaves it None,
    because a scalar can answer no question a structured check asks and treating it as data would
    have `null` and "not JSON at all" mean different things while looking identical.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.data: Any | None = None
        try:
            parsed = json.loads(text)
        except ValueError:
            return
        if isinstance(parsed, dict | list):
            self.data = parsed


def check_mentions(doc: Artifact, params: dict[str, Any]) -> tuple[bool | None, str]:
    """Every string in `params["all_of"]` appears in the artifact, case-insensitively.

    The worked example of a content check: it locates by CONTENT, so an artifact that says the
    right things in a different order or under different headings still passes.

    N/A when the row declares no needles — a fact about the expectations file, not about the
    artifact, per the rule in the module docstring.
    """
    wanted = params.get("all_of", [])
    if isinstance(wanted, str):
        raise TypeError(f"`all_of` must be a LIST of strings, got the string {wanted!r} — wrap it in []")
    if not isinstance(wanted, list):
        raise TypeError(f"`all_of` must be a list of strings, got {type(wanted).__name__}")
    if not wanted:
        return None, "this row declares nothing to look for"
    haystack = doc.text.casefold()
    missing = [needle for needle in wanted if needle.casefold() not in haystack]
    return not missing, "all present" if not missing else f"missing {missing}"


def check_json_field(doc: Artifact, params: dict[str, Any]) -> tuple[bool | None, str]:
    """`params["field"]` is present in the JSON artifact, and equals `params["equals"]` if given.

    N/A only when the row declares no `field`. An artifact that is not JSON FAILS rather than
    dropping out — the row asked a question about structure, and "I wrote prose instead" is an
    answer, not an inapplicable question. See the module docstring for why that direction matters.

    **It answers from the SHALLOWEST occurrences of the key only**, which is the rule that keeps it
    from marking down a correct artifact, and the three cases it balances:

    * *Relocation* — a body that nests its report one level deeper (`{"result": {"status": "ok"}}`)
      still answers, because the search is by key rather than by a fixed path.
    * *Repetition* — several occurrences at that same level ALL have to match, or an artifact
      reporting two failed jobs and one succeeded one would pass `status == ok` on the third.
    * *Unrelated detail* — a deeper `meta.cache.status` is not an answer to a question about the
      report's status, and counting it would mark down the arm whose body produced the RICHER
      artifact. That is failure mode one in the rubric's grader-fairness section, in the check that
      ships beside it.
    """
    field = params.get("field")
    if field is None:
        return None, "this row declares no field"
    if not isinstance(field, str):
        raise TypeError(f"`field` must be a string key, got {type(field).__name__}: {field!r}")
    if doc.data is None:
        return False, f"{field!r} unanswerable — the artifact is not a JSON object or array"
    found = _shallowest_values_for_key(doc.data, field)
    if not found:
        return False, f"{field!r} is absent"
    if "equals" not in params:
        return True, f"{field!r} present"
    ok = all(value == params["equals"] for value in found)
    return ok, f"every {field!r} {'==' if ok else '!='} {params['equals']!r} (found {found})"


def _shallowest_values_for_key(root: Any, key: str) -> list[Any]:
    """Every value stored under `key` at the shallowest depth it occurs, or `[]`.

    Breadth-first, and it does NOT descend past a level that answered: once the key is found, a
    deeper key of the same name belongs to some other part of the document.
    """
    level = [root]
    while level:
        found = [node[key] for node in level if isinstance(node, dict) and key in node]
        if found:
            return found
        deeper: list[Any] = []
        for node in level:
            if isinstance(node, dict):
                deeper.extend(node.values())
            elif isinstance(node, list):
                deeper.extend(node)
        level = deeper
    return []


# REPLACE / EXTEND: the checks your rows actually need. The two above are worked examples, not a
# fixed vocabulary — every name here is one an expectations file may use under `checks`, optionally
# suffixed with `#<label>` to declare it more than once.
CHECKS = {
    "mentions": check_mentions,
    "json_field": check_json_field,
}


def _report(score: float, *details: str, rules: dict[str, str] | None = None) -> int:
    """Print the protocol — score on line 1, details, then `RULES` — and return the exit code.

    The `RULES` line is emitted HERE rather than at the call sites, so that every early exit —
    wrong argv, a missing or malformed expectations file, a missing or unreadable artifact, the
    top-level `except` — carries it too. `RULES {}` from a grader that ran nothing is a fact a
    consumer can act on; a MISSING line means an older grader, and the two must not look alike.
    """
    print(f"{score:.4f}")
    for line in details:
        print(line)
    # Sorted keys and compact separators: the line is machine-read, and a stable spelling is what
    # lets a test assert the exact string rather than re-parsing to compare.
    print("RULES " + json.dumps(rules or {}, sort_keys=True, separators=(",", ":")))
    return 0


def _aggregate_rules(declared: Any, verdicts: dict[str, bool | None]) -> tuple[dict[str, str], list[str]]:
    """Fold each rule's check verdicts into one, returning (rule id -> verdict, complaints).

    ANY-FAIL, ALL-NA. See the module docstring for why the direction is load-bearing: it makes the
    result an upper bound on the rows failing a rule, and an upper bound is what a "this rule
    cannot clear the floor" verdict has to rest on.

    A malformed `rules` block is reported as a detail line and otherwise ignored — never a raise.
    Attribution is an optional annotation, and a typo in it must not cost the row its score.
    """
    if not declared:
        return {}, []
    if not isinstance(declared, dict):
        return {}, [f"SKIP `rules` must be an object of check -> rule id, got {type(declared).__name__}"]

    complaints: list[str] = []
    grouped: dict[str, list[bool | None]] = {}
    for key, verdict in verdicts.items():
        name = key.split(LABEL_SEPARATOR, 1)[0]
        # Exact key first, then the bare name, so one entry can attribute every labelled instance.
        rule = declared.get(key, declared.get(name))
        if rule is None:
            continue
        if not isinstance(rule, str) or not rule:
            complaints.append(f"SKIP `rules[{key!r}]` must be a non-empty string, got {rule!r}")
            continue
        grouped.setdefault(rule, []).append(verdict)

    unused = sorted(set(declared) - {k for k in verdicts} - {k.split(LABEL_SEPARATOR, 1)[0] for k in verdicts})
    if unused:
        # Named rather than dropped: an entry matching no check is a renamed or mistyped check key,
        # and the rule it names then reads as untouched by this row.
        complaints.append(f"SKIP `rules` entries matching no declared check: {unused}")

    resolved = {
        rule: "fail" if any(v is False for v in seen) else "na" if all(v is None for v in seen) else "pass"
        for rule, seen in grouped.items()
    }
    return resolved, complaints


def _run_checks(doc: Artifact, checks: dict[str, Any]) -> tuple[int, int, list[str], dict[str, bool | None]]:
    """Dispatch every declared check, returning (passed, applicable, details, verdicts).

    Checks run with stdout REDIRECTED: a `print()` inside one would otherwise land on line 1, where
    coder-eval expects the score, and the row would report a parse error instead of a result.

    `verdicts` maps each check key that produced one to `True` / `False` / `None` (N/A), for
    :func:`_aggregate_rules`. A SKIPPED check — a name with no entry in CHECKS — is absent from it,
    because it produced no verdict about anything.
    """
    passed = 0
    applicable = 0
    details: list[str] = []
    verdicts: dict[str, bool | None] = {}
    for key, params in checks.items():
        name = key.split(LABEL_SEPARATOR, 1)[0]
        fn = CHECKS.get(name)
        if fn is None:
            details.append(f"SKIP unknown check {key!r}")
            continue
        chatter = io.StringIO()
        try:
            if not isinstance(params, dict):
                raise TypeError(f"expected an object of params, got {type(params).__name__}")
            with contextlib.redirect_stdout(chatter):
                verdict, detail = fn(doc, params)
            if verdict is not None and verdict is not True and verdict is not False:
                raise TypeError(f"returned {verdict!r}; a check must return True, False or None")
        except Exception as exc:  # a raising check is a FAILING check, never a crashed grader
            verdict, detail = False, f"raised {exc!r}"
        # ONE CHECK, ONE LINE. A detail carrying a newline — a check's own multi-line message, or
        # artifact text quoted into one — would otherwise split into extra lines that can read as
        # further PASS/FAIL results. The score line is unaffected, but the detail stream is what a
        # human reads to decide whether the grader is fair, and it must not be forgeable.
        detail = " ".join(str(detail).splitlines())
        for line in chatter.getvalue().splitlines():
            details.append(f"     (stdout from {key}: {line})")
        verdicts[key] = verdict
        if verdict is None:
            details.append(f"N/A  {key}: {detail}")
            continue
        applicable += 1
        passed += int(verdict)
        details.append(("PASS " if verdict else "FAIL ") + f"{key}: {detail}")
    return passed, applicable, details, verdicts


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return _report(0.0, f"usage: {Path(__file__).name} <row_id>")
    row_id = argv[1]

    # Relative to the SCRIPT, never the cwd: the cwd is the sandbox, which is exactly where the
    # expectations must not be.
    spec_file = Path(__file__).resolve().parent / "expectations" / f"{row_id}.json"
    if not spec_file.is_file():
        return _report(0.0, f"no expectations file for row {row_id!r} at {spec_file}")
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8-sig", errors="replace"))
    except ValueError as exc:
        return _report(0.0, f"expectations file {spec_file} is not valid JSON: {exc}")
    if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
        return _report(0.0, f"expectations file {spec_file} must be an object with a string `path`")
    # `spec.get("checks", {})`, never `... or {}`: a falsy non-dict (`[]`, `""`, `0`) would fold
    # into an empty dict and reach the author as "0/0 applicable" rather than as the typo it is.
    checks = spec.get("checks", {})
    if not isinstance(checks, dict):
        return _report(0.0, f"expectations file {spec_file}: `checks` must be an object, got {type(checks).__name__}")

    # Relative to the CWD, which `run_command` sets to the sandbox root.
    artifact_path = Path(spec["path"])
    if not artifact_path.is_file():
        why = "is a directory" if artifact_path.is_dir() else "not found"
        return _report(0.0, f"artifact {why}: {artifact_path} (cwd {Path.cwd()})")
    try:
        # `utf-8-sig` strips a BOM if there is one and is a no-op otherwise. Without it a
        # BOM-prefixed JSON artifact fails `json.loads` and every structural check scores it as
        # prose — a WRONG score for a correct artifact, which is worse than a crash.
        text = artifact_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return _report(0.0, f"artifact {artifact_path} could not be read: {exc}")

    passed, applicable, details, verdicts = _run_checks(Artifact(text), checks)
    # `spec.get("rules")` is passed through unvalidated on purpose: `_aggregate_rules` owns the
    # validation, so a malformed block costs a detail line rather than the row's score. Attribution
    # is an annotation, and an annotation must never be able to zero a measurement.
    rules, complaints = _aggregate_rules(spec.get("rules"), verdicts)
    details += complaints
    if not applicable:
        # 0/0 is not a perfect row and not a failed one — it is a row that measured nothing, which
        # `/coder-eval:task`'s discrimination gate catches by grading a known-GOOD artifact and
        # seeing 0.0 here.
        return _report(0.0, f"0/0 applicable — no check applied to {artifact_path}", *details, rules=rules)
    return _report(passed / applicable, f"{passed}/{applicable} applicable checks passed", *details, rules=rules)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:
        # The last guard on the protocol. Any unanticipated failure still reports a score and a
        # reason and still exits 0 — a traceback on stderr with a non-zero exit would be read as
        # 0.0 with the reason nowhere in the report.
        sys.exit(_report(0.0, f"grader failed: {exc!r}"))
