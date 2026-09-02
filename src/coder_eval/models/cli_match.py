"""Argv-matching models shared by ``cli_called`` and ``record_cli`` response rules.

A cycle-free leaf, like :mod:`coder_eval.models.judge_defaults`: both
:mod:`coder_eval.models.criteria` and :mod:`coder_eval.models.sandbox` import it,
and sandbox.py could not import from criteria.py in any case (criteria.py already
takes ``RECORD_CLI_LOG`` from sandbox.py).

The matching *semantics* live in :mod:`coder_eval.argv_match`, which is
stdlib-only because its source is embedded into generated shims. This module
holds the authoring surface — the pydantic models and the validators that reject
a pattern which cannot mean what it looks like — and lowers it to the plain spec
dict that engine consumes.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coder_eval.argv_match import FlagPredicate, MatchSpec, is_number


class FlagMatch(BaseModel):
    """Predicate for ONE flag value.

    Exactly one predicate field may be set. In YAML a bare scalar is accepted as
    shorthand for ``equals`` (``model: gemini_2_5_pro`` == ``model: {equals:
    gemini_2_5_pro}``), which keeps the common case unnested.

    ``absent: true`` asserts the flag was NOT passed — distinct from "passed with
    a different value", and the reason this is a predicate rather than a bare
    ``dict[str, str]`` on the criterion.

    The one-predicate rule means a conjunction on a single flag ("contains BOTH
    A and B") is not expressible here. Either declare two ``cli_called`` criteria
    over the same log, or use one ``matches_regex`` that spans both — the latter
    is what a heredoc-built JSON payload usually wants, together with
    ``flags: 16`` (``re.DOTALL``) so ``.`` crosses the payload's newlines.
    """

    model_config = ConfigDict(extra="forbid")

    equals: str | None = Field(default=None, description="Flag value must equal this string exactly")
    contains: str | None = Field(default=None, description="Flag value must contain this substring")
    matches_regex: str | None = Field(
        default=None,
        description="Flag value must match this regex. Scoped to ONE value, unlike a whole-line pattern",
    )
    any_of: list[str] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Flag value must equal one of these strings. Non-empty: an empty list would match "
            "nothing, so a max_count: 0 guard built on it would pass vacuously"
        ),
    )
    absent: bool = Field(default=False, description="Flag must NOT be present in the invocation")
    present: bool = Field(
        default=False,
        description=(
            "Flag must be present, whatever its value -- the predicate for a boolean switch. Unlike "
            '`equals: ""` it survives a CLI that spells the switch `--force true`, and it never makes '
            "the flag value-bearing, so asserting a switch cannot swallow the next positional"
        ),
    )
    aliases: list[str] = Field(
        default_factory=list,
        description=(
            "Other names for the SAME flag, e.g. aliases: [y] on a `yes` predicate so `-y` and "
            "`--yes` are one flag. Values are gathered across every name: `present` holds if any "
            "appeared, `absent` only if none did, a value predicate matches if any value under any "
            "name satisfies it"
        ),
    )
    flags: int = Field(
        default=0,
        description=(
            "Regex flags for matches_regex (re.IGNORECASE=2, re.MULTILINE=8, re.DOTALL=16), "
            "mirroring FileMatchesRegexCriterion.flags. DOTALL is the usual need, since a "
            "heredoc-built flag value spans lines"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_scalar_shorthand(cls, value: Any) -> Any:
        """Accept ``model: gemini_2_5_pro`` as ``model: {equals: ...}``."""
        if isinstance(value, str):
            return {"equals": value}
        return value

    @model_validator(mode="after")
    def _exactly_one_predicate(self) -> FlagMatch:
        set_predicates = [
            name for name in ("equals", "contains", "matches_regex", "any_of") if getattr(self, name) is not None
        ]
        if self.absent:
            set_predicates.append("absent")
        if self.present:
            set_predicates.append("present")
        if len(set_predicates) != 1:
            msg = (
                "FlagMatch requires exactly one of equals / contains / matches_regex / any_of / absent / present, "
                f"got {sorted(set_predicates) or 'none'}"
            )
            raise ValueError(msg)
        # `flags` only reaches re.compile via matches_regex; setting it beside any
        # other predicate is a silent no-op, so reject it rather than mislead.
        if self.flags and self.matches_regex is None:
            msg = f"FlagMatch.flags applies only to matches_regex, but the predicate is {set_predicates[0]!r}"
            raise ValueError(msg)
        # Compile HERE, not in a checker: this model now feeds two consumers, and
        # only one of them can report. A `record_cli` response rule evaluates the
        # pattern inside the sandbox, where a PatternError is swallowed and the
        # tool serves its fallback -- a log line indistinguishable from a
        # legitimate no-match, so the task scores differently for identical agent
        # behaviour with nothing on any report surface. At load, both surfaces
        # refuse the pattern instead.
        if self.matches_regex is not None:
            try:
                re.compile(self.matches_regex, self.flags)
            except (re.error, ValueError) as exc:
                msg = f"FlagMatch.matches_regex is not a valid regex with flags={self.flags}: {exc}"
                raise ValueError(msg) from exc
        return self


# The argv facets every matching surface must offer. `cli_called` declares these
# fields itself (with grading-specific guidance in each description) rather than
# inheriting them, so a facet added to one surface and forgotten on the other is
# caught by the parity test in tests/test_cli_match_parity.py instead of shipping
# as a rule the criterion cannot express.
MATCH_FACET_FIELDS: tuple[str, ...] = ("verb", "verb_any_of", "positional", "flags", "value_flags", "ignore_flags")


def verb_spellings_of(verb: str | None, verb_any_of: list[str] | None) -> list[list[str]]:
    """Each accepted verb as its token list; empty when there is no verb constraint.

    The only place either verb field is split, so the validators, the matcher and
    the failure detail cannot disagree.
    """
    if verb is not None:
        return [verb.split()]
    if verb_any_of is not None:
        return [spelling.split() for spelling in verb_any_of]
    return []


def validate_verbs(verb: str | None, verb_any_of: list[str] | None, spellings: list[list[str]], label: str) -> None:
    """Reject verb declarations that cannot mean what they look like.

    ``label`` names the surface (``cli_called``, ``record_cli response when``) so
    the message points at the block the author actually wrote.
    """
    if verb is not None and verb_any_of is not None:
        msg = f"{label} accepts verb or verb_any_of, not both"
        raise ValueError(msg)
    # Falsy, so an at-least-one-facet check would read it as "no verb".
    if verb_any_of is not None and not verb_any_of:
        msg = f"{label} verb_any_of must not be empty: drop the field to match any verb"
        raise ValueError(msg)
    # A character count would pass "   ", whose split() is an empty prefix.
    if any(not tokens for tokens in spellings):
        msg = f"{label} verb must not be blank: a blank verb is an empty prefix and matches every invocation"
        raise ValueError(msg)
    # A verb is compared against the NON-FLAG arguments, so a flag written into it
    # can never match anything -- and the failure is silent: the criterion scores 0
    # against a log that holds the very call it describes, and a response rule falls
    # through to the tool's default. Inviting, too, since a whole verb reads like a
    # command line. `is_number` mirrors the splitter's own rule so this check cannot
    # forbid a token (`-1`) that the matcher would in fact have seen.
    for tokens in spellings:
        for token in tokens:
            if token.startswith("-") and token != "-" and not is_number(token.lstrip("-")):
                msg = (
                    f"{label} verb token {token!r} looks like a flag. A verb matches only the "
                    "non-flag arguments, so a flag inside it can never match. Put it in `flags:` "
                    f"instead, e.g. flags: {{{token.lstrip('-').split('=')[0]}: <value>}}."
                )
                raise ValueError(msg)
    for first, second in itertools.combinations(spellings, 2):
        if first == second:
            msg = f"{label} verb_any_of lists {' '.join(first)!r} twice"
            raise ValueError(msg)
        # Sorting by length is total here: two DISTINCT entries of equal length
        # cannot prefix each other, since an equal-length prefix is the same list.
        shorter, longer = sorted((first, second), key=len)
        if longer[: len(shorter)] == shorter:
            msg = (
                f"{label} verb_any_of entry {' '.join(shorter)!r} is a prefix of "
                f"{' '.join(longer)!r}; the shorter one already accepts every invocation the "
                "longer one does, so drop the longer entry or list only the verbs you mean."
            )
            raise ValueError(msg)


def validate_positional(positional: list[str] | None, label: str) -> None:
    """Reject an empty positional list, which slices to itself and asserts nothing."""
    if positional is not None and not positional:
        msg = (
            f"{label} positional must not be empty: an empty list asserts nothing. List the "
            "arguments you expect, or drop the field."
        )
        raise ValueError(msg)


def validate_flag_ownership(flags: dict[str, FlagMatch] | None, ignore_flags: list[str], label: str) -> None:
    """Reject flag predicates that collide with each other or with ``ignore_flags``.

    An alias that is also a key, or shared between two predicates, would make
    which predicate owns a recorded flag depend on dict order. A predicate on an
    ignored flag can never be evaluated: ignore_flags drops the flag before any
    predicate runs, so ``absent`` would pass vacuously and ``equals`` could never
    match.
    """
    seen: dict[str, str] = {}
    for key, predicate in (flags or {}).items():
        for name in (key, *predicate.aliases):
            if name in seen and seen[name] != key:
                msg = (
                    f"{label} flag name {name!r} is claimed by both {seen[name]!r} and {key!r} "
                    "(via aliases); a flag can belong to only one predicate"
                )
                raise ValueError(msg)
            seen[name] = key
        if key in predicate.aliases:
            msg = f"{label} flag {key!r} lists itself in aliases"
            raise ValueError(msg)

    shadowed = sorted(set(seen) & set(ignore_flags))
    if shadowed:
        names = ", ".join(repr(n) for n in shadowed)
        msg = (
            f"{label} flag predicate(s) {names} are also listed in ignore_flags (directly or as "
            "an alias), which drops them before matching. Remove them from ignore_flags, or drop "
            "the predicate."
        )
        raise ValueError(msg)


def build_match_spec(
    *,
    verb_spellings: list[list[str]],
    positional: list[str] | None,
    flags: dict[str, FlagMatch] | None,
    value_flags: list[str],
    ignore_flags: list[str],
) -> MatchSpec:
    """Lower an authored match surface to what :mod:`coder_eval.argv_match` reads.

    JSON-serializable on purpose: the same dict is embedded verbatim into a
    generated shim, so a spec the criterion evaluates in-process and a spec the
    shim evaluates in the sandbox are the same bytes.

    The cast is honest because ``FlagMatch``'s field set IS ``FlagPredicate``'s key
    set -- asserted in tests/test_cli_match_parity.py, so adding a field to one and
    not the other fails rather than silently dropping out of the lowered spec.
    """
    return {
        "verb_spellings": verb_spellings,
        "positional": positional,
        "flags": (
            {name: cast("FlagPredicate", predicate.model_dump()) for name, predicate in flags.items()}
            if flags
            else None
        ),
        "value_flags": list(value_flags),
        "ignore_flags": list(ignore_flags),
    }


class CliMatch(BaseModel):
    """A pattern over ONE invocation's arguments, used to dispatch a canned response.

    The ``when:`` block of a ``record_cli`` response rule. Facets are ANDed, and
    an unmentioned facet is unconstrained — an extra ``--output json`` never
    stops a rule from matching. Matching semantics are identical to the
    ``cli_called`` criterion of the same shape, so the pattern that selects a
    stub response is the pattern that grades it.

    Always a mapping, never a bare string: a pattern has six possible facets, so
    a lone ``"ixp dummy1"`` would leave the reader to infer which one it sets, and
    a quoted verb reads enough like a command line to invite the flags a verb
    cannot hold. :class:`FlagMatch` one level down keeps its scalar shorthand
    (``flags: {output: json}``) — a single-valued predicate has only one facet a
    scalar could mean, so nothing is left to infer there.
    """

    model_config = ConfigDict(extra="forbid")

    verb: str | None = Field(
        default=None,
        description=(
            "Whitespace-separated subcommand chain that must be an ORDERED PREFIX of the "
            "invocation's non-flag arguments, compared token by token (so 'projects list' never "
            "matches 'projects lists', and 'labellings confirm' never matches 'labellings "
            "unconfirm'). Tokens after it are unconstrained, so a short verb claims every "
            "invocation under it: 'projects' answers 'projects delete' as readily as 'projects get'"
        ),
    )
    verb_any_of: list[str] | None = Field(
        default=None,
        description=(
            "Alternative whole verbs; matches if ANY of them does, e.g. ['projects list', "
            "'projects get'] to serve one response for both spellings. Each entry is a complete "
            "verb in the same form `verb` takes, NOT one token of a chain. Mutually exclusive with "
            "`verb`"
        ),
    )
    positional: list[str] | None = Field(
        default=None,
        description=(
            "Non-flag arguments that must follow the verb, in order. A PREFIX of what followed, so "
            "anything past them is unconstrained. Use it to answer differently per project/id, e.g. "
            "positional: ['proj-1']. Depends on value_flags being complete — an undeclared flag's "
            "value stays non-flag and shifts these slots"
        ),
    )
    flags: dict[str, FlagMatch] | None = Field(
        default=None,
        description=(
            "Flag name (without leading dashes) to predicate. A bare scalar means 'equals', e.g. "
            "flags: {output: json} to serve JSON only when the agent asked for it. Flags not listed "
            "are ignored, so an unrelated flag never stops the rule matching"
        ),
    )
    value_flags: list[str] = Field(
        default_factory=lambda: ["output"],
        description=(
            "Flag names (no leading dashes) that consume a following token as their value. Keys of "
            "`flags` are value-bearing already; everything else is a switch whose following token "
            "stays positional. Declare a flag here when its value would otherwise be read as a "
            "positional, e.g. [folder] for `--folder F proj-1`. Defaults to [output]"
        ),
    )
    ignore_flags: list[str] = Field(
        default_factory=list,
        description=(
            "Flag names dropped before matching. Empty by default, unlike the cli_called criterion: "
            "a response rule dispatches rather than grades, so nothing is outcome-invisible here and "
            "a rule may key on any flag it declares"
        ),
    )

    @property
    def verb_spellings(self) -> list[list[str]]:
        """Each accepted verb as its token list; empty when there is no verb constraint."""
        return verb_spellings_of(self.verb, self.verb_any_of)

    @property
    def match_spec(self) -> MatchSpec:
        """This pattern as what :func:`coder_eval.argv_match.argv_matches` reads."""
        return build_match_spec(
            verb_spellings=self.verb_spellings,
            positional=self.positional,
            flags=self.flags,
            value_flags=self.value_flags,
            ignore_flags=self.ignore_flags,
        )

    @model_validator(mode="before")
    @classmethod
    def _reject_scalar_shorthand(cls, value: Any) -> Any:
        """Name the fix, rather than let pydantic report a bare type error.

        ``when: "ixp dummy1"`` is the obvious thing to try, and the generic
        "Input should be a valid dictionary" says nothing about which key was
        meant.
        """
        if isinstance(value, str):
            msg = f'record_cli response `when` must be a mapping, not a bare string: use {{verb: "{value}"}}'
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _validate_match(self) -> CliMatch:
        label = "record_cli response `when`"
        validate_verbs(self.verb, self.verb_any_of, self.verb_spellings, label)
        validate_positional(self.positional, label)
        validate_flag_ownership(self.flags, self.ignore_flags, label)
        # Falsiness, not `is None`: `verb: ""` would otherwise match every
        # invocation and shadow every rule below it.
        if not self.verb and not self.verb_any_of and not self.positional and not self.flags:
            msg = (
                "record_cli response `when` requires at least one of verb / verb_any_of / positional "
                "/ flags. A rule that matches everything is the tool's default response: set the "
                "entry's own exit_code / stdout / stderr instead."
            )
            raise ValueError(msg)
        return self
