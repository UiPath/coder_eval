"""Structured argv matching — the one engine both CLI surfaces share.

Two places ask the same question about one invocation. The ``cli_called``
criterion reads a recorded ``argv`` back afterwards and asks *did this happen*;
a ``record_cli`` response rule asks it live, inside the sandbox, to choose which
canned response to serve. An author who writes ``verb: "ixp projects get"`` in a
rule and again in the criterion that grades it must get one semantic, not two
that drift.

Everything here takes PLAIN DICTS rather than pydantic models, and imports
nothing beyond the standard library: :func:`coder_eval.invocation_log.render_recorder`
embeds this module's SOURCE into every generated shim, and that shim runs inside
the sandbox, where ``coder_eval`` is not installed. Lint rule CE047 keeps the
imports stdlib-only.

The spec dict is what ``CliMatch.match_spec`` emits::

    {"verb_spellings": [["ixp", "projects", "get"]],
     "positional": ["proj-1"],
     "flags": {"model": {"equals": "pro", "aliases": [], "flags": 0, ...}},
     "value_flags": ["output"],
     "ignore_flags": []}

``verb_spellings`` is empty when there is no verb constraint; ``positional`` and
``flags`` are absent or None when unconstrained.
"""

import re
from typing import Any


def split_flags(
    argv: list[str],
    ignore: frozenset[str],
    value_flags: frozenset[str],
    known_names: frozenset[str] = frozenset(),
) -> tuple[list[str], dict[str, list[str]]]:
    """Split ``argv`` into non-flag arguments and a flag map.

    Only flags in ``value_flags`` consume a following token; everything else is a
    switch. Guessing instead (``--yes proj-1`` binding ``yes=proj-1``) let a
    ``max_count: 0`` guard pass on the delete it forbade, so ambiguity resolves
    toward keeping the token positional.

    ``--flag=value`` binds directly, being unambiguous. Repeated flags accumulate.
    ``ignore`` names are dropped with their values. ``--`` ends flag parsing and is
    itself dropped; a lone ``-`` is positional.

    ``known_names`` are the flag names the spec mentions at all (including
    presence predicates and aliases). A declared name is always taken whole, so a
    genuine multi-char short flag still matches; undeclared ones are split.
    """
    positional: list[str] = []
    flags: dict[str, list[str]] = {}

    def record(name: str, value: str) -> None:
        if name not in ignore:
            flags.setdefault(name, []).append(value)

    index = 0
    end_of_flags = False
    while index < len(argv):
        token = argv[index]
        index += 1

        if end_of_flags or not token.startswith("-") or token == "-":
            positional.append(token)
            continue
        if token == "--":
            end_of_flags = True
            continue

        # Equals form: unambiguous, bind it and move on.
        if "=" in token:
            name, _, value = token.partition("=")
            record(name.lstrip("-"), value)
            continue

        name = token.lstrip("-")
        known = name in value_flags or name in known_names

        # A bare negative number is a value, not a flag. Reading `-1` as a flag
        # named `1` drops it from the positionals -- the same silent-disappearance
        # that let `--yes proj-1` slip a delete past a guard.
        if not known and is_number(name):
            positional.append(token)
            continue

        # Clustered short flags: `-rf` is `-r -f`. Declared names win, so a real
        # multi-char short flag still matches, and `-fvalue` binds when `f` takes
        # a value; otherwise each character is its own switch, which is what stops
        # `-yf` escaping an `aliases: [y]` predicate.
        if not known and not token.startswith("--") and len(name) > 1:
            head, rest = name[0], name[1:]
            if head in value_flags:
                record(head, rest)
            else:
                for char in name:
                    record(char, "")
            continue

        if name in value_flags and index < len(argv):
            record(name, argv[index])
            index += 1
        else:
            # Switch: empty value, and the next token is left for the positionals.
            record(name, "")

    return positional, flags


def is_number(text: str) -> bool:
    """Whether ``text`` parses as a number, so ``-1`` reads as a value not a flag."""
    try:
        float(text)
    except ValueError:
        return False
    return True


def predicate_needs_value(predicate: dict[str, Any]) -> bool:
    """Whether evaluating this flag predicate requires the flag's VALUE.

    Presence predicates (``present`` / ``absent``) do not, so they must not make
    a flag value-bearing: asserting a boolean switch would otherwise make it
    consume the following token, dropping that token from the positionals.
    Mirrors ``FlagMatch.needs_value``; both sides of the spec boundary must agree
    or a rule and the criterion grading it would parse the same argv differently.
    """
    return not (predicate.get("present") or predicate.get("absent"))


def flag_matches(predicate: dict[str, Any], values: list[str] | None) -> bool:
    """Whether a recorded flag satisfies one flag predicate.

    ``values`` is None when the flag was not passed at all. Every non-``absent``
    predicate is satisfied by ANY of a repeated flag's values.
    """
    if predicate.get("absent"):
        return values is None
    if predicate.get("present"):
        return values is not None
    if values is None:
        return False
    if predicate.get("equals") is not None:
        return any(value == predicate["equals"] for value in values)
    if predicate.get("contains") is not None:
        return any(predicate["contains"] in value for value in values)
    if predicate.get("any_of") is not None:
        allowed = set(predicate["any_of"])
        return any(value in allowed for value in values)
    if predicate.get("matches_regex") is not None:
        regex = re.compile(predicate["matches_regex"], predicate.get("flags", 0))
        return any(regex.search(value) is not None for value in values)
    # Unreachable: the model guarantees exactly one predicate. Raise rather than
    # return False so a predicate added without a matcher arm here fails loudly.
    raise AssertionError(f"flag predicate has no matcher arm: {predicate!r}")


def argv_matches(spec: dict[str, Any], argv: list[str]) -> bool:
    """Whether ``argv`` satisfies every configured facet of one match spec."""
    flag_specs: dict[str, Any] = spec.get("flags") or {}

    def names_of(flag: str, predicate: dict[str, Any]) -> tuple[str, ...]:
        return (flag, *(predicate.get("aliases") or ()))

    # Declarations only. Folding `ignore_flags` into value_flags made ignored
    # SWITCHES value-bearing, which swallowed the next positional and reopened a
    # guard false-PASS; an ignored flag that takes a value declares it in
    # value_flags.
    ignore = frozenset(spec.get("ignore_flags") or ())
    value_flags = frozenset(
        name
        for flag, predicate in flag_specs.items()
        if predicate_needs_value(predicate)
        for name in names_of(flag, predicate)
    ) | frozenset(spec.get("value_flags") or ())
    known_names = (
        frozenset(name for flag, predicate in flag_specs.items() for name in names_of(flag, predicate)) | ignore
    )

    positional, flags = split_flags(argv, ignore, value_flags, known_names)

    offset = 0
    spellings = spec.get("verb_spellings") or []
    if spellings:
        # Token-wise, not a subset and not a string startswith: `labellings confirm`
        # must never be satisfied by `labellings unconfirm`. Taking the first match is
        # safe because validation rejects one spelling prefixing another, so no argv
        # can match two.
        matched = next((tokens for tokens in spellings if positional[: len(tokens)] == list(tokens)), None)
        if matched is None:
            return False
        # Measured from the spelling that matched, since spellings can differ in length.
        offset = len(matched)

    expected = spec.get("positional")
    if expected is not None and positional[offset : offset + len(expected)] != list(expected):
        return False

    for flag, predicate in flag_specs.items():
        # [] means absent under every spelling, which flag_matches distinguishes
        # from a switch's "present with empty value" ([""]).
        collected = [value for name in names_of(flag, predicate) for value in flags.get(name, [])]
        if not flag_matches(predicate, collected or None):
            return False

    return True


def select_rule(rules: list[dict[str, Any]], argv: list[str]) -> tuple[int, dict[str, Any]] | None:
    """``(index, rule)`` of the first rule whose ``when`` spec matches ``argv``, or None.

    First match wins, so ordering is the author's disambiguation tool: the
    specific rule goes above the general one. Stateless by design -- the same
    argv gets the same answer every time, which keeps the shim free of on-disk
    counters that two concurrent agent commands would race on.

    The index travels with the rule because the shim records it: "no rule
    matched" and "a rule matched and looks like the default" are otherwise the
    same line in the log.
    """
    for index, rule in enumerate(rules):
        if argv_matches(rule.get("when") or {}, argv):
            return index, rule
    return None
