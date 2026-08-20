"""`CLAUDE.md` is loaded into every session's context, so its size is a real cost.

Not a style preference: the file is prepended to the context of every request in every session, so a
paragraph nobody reads is paid for on every call, forever. It reached 89,090 characters, of which ONE
line was 33,247 — 37% of the file — restating 35 rule docstrings that already existed in
`tests/lint/rules/`. Duplicated prose is worse than absent prose here, because the copy drifts and
the reader cannot tell which one is current.

One number, in one place, so the next essay is a visible decision rather than a drift. A CEILING, not
an equality — shrinking further is always allowed.

**What it does NOT check** is that the content is right. The derived sentences are guarded where they
are derived: CE036's exemption list by `test_ce036_exemption_list_matches_claude_md`, every skill's
presence by `test_skill_docs_surfaces_list_every_skill`, the stated skill count by
`test_skill_docs_surfaces_state_the_right_count`, and the docs-index tables by CE028. This only
bounds the size.
"""

from __future__ import annotations

import pytest

from tests.lint_tests.shared import REPO_ROOT


pytestmark = pytest.mark.lint

# 89,090 before the prose registers were split out; 44,908 after. The ceiling leaves room for the
# tree to grow a module or two without a negotiation, and not for another essay.
MAX_CHARS = 46_000


def test_claude_md_stays_within_its_context_budget() -> None:
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert len(text) <= MAX_CHARS, (
        f"CLAUDE.md is {len(text):,} characters, over the agreed {MAX_CHARS:,}. It is loaded into "
        "every session's context, so this is a per-request cost. Move the reasoning to the module "
        "docstring or `.claude/decisions/` and leave a pointer — or raise this number deliberately, "
        "in this one place."
    )


def test_no_single_line_dominates_the_file() -> None:
    """The shape the ceiling alone would miss.

    One line held 37% of the file, and a size ceiling can be satisfied while that stays true. A long
    line is also the shape that HIDES a derived sentence: nobody diffs a 33,000-character line, which
    is how CE036's exemption list drifted from the code inside one.
    """
    lines = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    worst = max(lines, key=len)
    assert len(worst) <= 6_000, (
        f"one line is {len(worst):,} characters — {len(worst) * 100 // sum(len(x) + 1 for x in lines)}% "
        f"of the file. Split it into bullets or move the detail out:\n  {worst[:160]}…"
    )
