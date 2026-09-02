"""Structured argv matching: the one engine both CLI surfaces share.

ASCII-only by rule, not by accident: this module's source is spliced into
generated shims, and `test_rendered_shim_is_pure_ascii` covers the spliced
shape, so an em-dash here fails that test rather than a sandbox somewhere.

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

:class:`MatchSpec` is what ``CliMatch.match_spec`` emits. It is a ``TypedDict``
rather than a bare dict on purpose: it is the seam where every guarantee the
pydantic models establish would otherwise be erased, and the fallback for a key
the reader failed to find is always "unconstrained" -- the direction that makes a
rule match everything, or a criterion score 1.0 against any log. TypedDict is
closed, so a key renamed on either side is a pyright error on both.
"""

import re
from typing import TypedDict


class FlagPredicate(TypedDict):
    """One ``FlagMatch``, lowered. Every key present: the producer dumps the model."""

    equals: str | None
    contains: str | None
    matches_regex: str | None
    any_of: list[str] | None
    absent: bool
    present: bool
    aliases: list[str]
    flags: int


class MatchSpec(TypedDict):
    """One authored pattern, lowered. ``verb_spellings`` is empty for no constraint.

    ``positional`` / ``flags`` are None when unconstrained -- None rather than
    absent, so a reader indexes required keys directly and a missing one is a
    KeyError rather than a silently wider match.
    """

    verb_spellings: list[list[str]]
    positional: list[str] | None
    flags: dict[str, FlagPredicate] | None
    value_flags: list[str]
    ignore_flags: list[str]


class ResponseRule(TypedDict):
    """One ``CliResponse``, lowered -- what the shim carries and dispatches on."""

    when: MatchSpec
    exit: int
    stdout: str
    stderr: str


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


def predicate_needs_value(predicate: FlagPredicate) -> bool:
    """Whether evaluating this flag predicate requires the flag's VALUE.

    Presence predicates (``present`` / ``absent``) do not, so they must not make
    a flag value-bearing: asserting a boolean switch would otherwise make it
    consume the following token, dropping that token from the positionals. That
    is how `flags: {yes: {present: true}}` on a guard over `delete --yes proj-1`
    once bound ``yes=proj-1``, dropped the project name, and handed the guard a
    false PASS.

    The ONLY implementation of the rule. ``FlagMatch`` deliberately does not
    carry a pydantic-side twin: two spellings of one predicate rule is how a rule
    and the criterion grading it come to parse the same argv differently.
    """
    return not (predicate["present"] or predicate["absent"])


def flag_matches(predicate: FlagPredicate, values: list[str] | None) -> bool:
    """Whether a recorded flag satisfies one flag predicate.

    ``values`` is None when the flag was not passed at all. Every non-``absent``
    predicate is satisfied by ANY of a repeated flag's values.
    """
    if predicate["absent"]:
        return values is None
    if predicate["present"]:
        return values is not None
    if values is None:
        return False
    if (equals := predicate["equals"]) is not None:
        return any(value == equals for value in values)
    if (contains := predicate["contains"]) is not None:
        return any(contains in value for value in values)
    if (any_of := predicate["any_of"]) is not None:
        allowed = set(any_of)
        return any(value in allowed for value in values)
    if (pattern := predicate["matches_regex"]) is not None:
        # Compiled at load by FlagMatch, so this cannot raise on a spec the models
        # produced -- and re caches, so recompiling per invocation is not a cost.
        regex = re.compile(pattern, predicate["flags"])
        return any(regex.search(value) is not None for value in values)
    # Unreachable: the model guarantees exactly one predicate. Raise rather than
    # return False so a predicate added without a matcher arm here fails loudly.
    raise AssertionError(f"flag predicate has no matcher arm: {predicate!r}")


def argv_matches(spec: MatchSpec, argv: list[str]) -> bool:
    """Whether ``argv`` satisfies every configured facet of one match spec."""
    flag_specs = spec["flags"] or {}

    def names_of(flag: str, predicate: FlagPredicate) -> tuple[str, ...]:
        return (flag, *predicate["aliases"])

    # Declarations only. Folding `ignore_flags` into value_flags made ignored
    # SWITCHES value-bearing, which swallowed the next positional and reopened a
    # guard false-PASS; an ignored flag that takes a value declares it in
    # value_flags.
    ignore = frozenset(spec["ignore_flags"])
    value_flags = frozenset(
        name
        for flag, predicate in flag_specs.items()
        if predicate_needs_value(predicate)
        for name in names_of(flag, predicate)
    ) | frozenset(spec["value_flags"])
    known_names = (
        frozenset(name for flag, predicate in flag_specs.items() for name in names_of(flag, predicate)) | ignore
    )

    positional, flags = split_flags(argv, ignore, value_flags, known_names)

    offset = 0
    spellings = spec["verb_spellings"]
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

    expected = spec["positional"]
    if expected is not None and positional[offset : offset + len(expected)] != list(expected):
        return False

    for flag, predicate in flag_specs.items():
        # [] means absent under every spelling, which flag_matches distinguishes
        # from a switch's "present with empty value" ([""]).
        collected = [value for name in names_of(flag, predicate) for value in flags.get(name, [])]
        if not flag_matches(predicate, collected or None):
            return False

    return True


def select_rule(rules: list[ResponseRule], argv: list[str]) -> tuple[int, ResponseRule] | None:
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
        if argv_matches(rule["when"], argv):
            return index, rule
    return None
