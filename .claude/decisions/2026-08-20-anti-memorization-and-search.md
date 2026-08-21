# The anti-memorization preflight, and what `search_compare` is not

Subject: `optimize/search.py::candidate_leaks`, `skill_text`, `search_compare`,
`leak_detection.py`.

## Why the leak preflight must be handed a DIRECTORY, not a file

`candidate_leaks` compares TEXT; `skill_text(skill_dir)` is the reader, and the CALLER owns which
of the two it passes. That split is why the obligation has to be stated rather than assumed: a skill
is a directory, and handed `SKILL.md` alone the preflight comes back CLEAN for a candidate that
bundled train-row content into `scripts/` or a reference file — byte-identical to a genuinely clean
result, which is the worst shape a preflight can have.

## Why it shares its primitive with CE061 rather than reimplementing it

`LEAK_LOCATOR_FIELDS`, `LEAK_MIN_CHARS`, `string_leaves` and `graded_strings` are ONE declaration
with TWO consumers pointing in opposite directions: CE061 asks whether a dataset row's PROMPT
contains a value a criterion grades it on; `candidate_leaks` asks whether a candidate SKILL.md newly
contains train-row content. A second copy would agree on ordinary input and diverge exactly where
either was written for.

They differ in one behaviour, and it is a parameter rather than a fork: `graded_strings(drop_type=)`.
CE061 keeps the discriminator — a PROMPT saying "skill_triggered" is worth flagging — while
`candidate_leaks` drops it, because a skill BODY discussing eval criteria names types legitimately.

The primitive lives in its own module rather than on `optimize.gate` because a task-lint rule
importing from the optimize gate inverts the dependency, the same separation `pricing.py` and
`path_utils.py` already have.

## `search_compare` is emphatically not a gate

It is an accept/revert decision inside a search, over one arm at a time, and it does not correct for
multiplicity. Calling it a gate invites a reader to promote on it, which is the one thing it cannot
support: the gates are `activation_gate` and `execution_gate`, and both go through Holm.

## A hole is absent, never zero

Every front and every comparison here treats a missing measurement as missing. Folding it to 0.0
makes an arm that failed to produce a number look worse than one that produced a bad one, which
inverts the ranking rather than merely biasing it.
