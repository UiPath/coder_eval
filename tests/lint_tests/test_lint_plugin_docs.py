"""Lint tests: The tutorials and user-facing guides that quote the plugin's own artifacts."""

from pathlib import Path

import pytest

from tests.lint_tests.plugin_base import PluginArtifactsBase
from tests.lint_tests.shared import (
    PLUGIN_ROOT,
    PLUGIN_SKILLS,
    SKILL_DISABLE_MODEL_INVOCATION,
    SKILL_DOC_SURFACES,
    _normalized,
    _wrong_skill_count_offenders,
)


@pytest.mark.lint
class TestTheTutorialsAndGuides(PluginArtifactsBase):
    """The tutorials and user-facing guides that quote the plugin's own artifacts.

    One of five classes carved out of `TestPluginArtifacts`; the shared class attributes and
    grader helpers live on :class:`PluginArtifactsBase`.
    """

    @pytest.mark.parametrize("skill", PLUGIN_SKILLS, ids=[p.parent.name for p in PLUGIN_SKILLS])
    def test_skill_docs_surfaces_list_every_skill(self, skill: Path):
        # What stops the next skill from shipping undocumented. CLAUDE.md was normalized to
        # the slash form when the sixth skill landed, so one form is accepted everywhere.
        name = f"/coder-eval:{skill.parent.name}"
        missing = [
            surface
            for surface in SKILL_DOC_SURFACES
            if name not in (self.REPO_ROOT / surface).read_text(encoding="utf-8")
        ]
        assert not missing, f"{name} is not documented in {missing} — a shipped skill nobody can discover"

    def test_tutorial_index_lists_every_tutorial(self):
        # `docs/tutorials/README.md` is the index a reader lands on, and it is hand-written
        # while the set of tutorials is a directory listing. mkdocs' nav is guarded by CE028,
        # but nothing read this table — so a tutorial added without a row here is invisible
        # to anyone who does not already know its filename.
        tut_dir = self.REPO_ROOT / "docs" / "tutorials"
        index = (tut_dir / "README.md").read_text(encoding="utf-8")
        pages = sorted(p.name for p in tut_dir.glob("*.md") if p.name != "README.md")
        missing = [name for name in pages if f"({name})" not in index]
        assert not missing, (
            f"docs/tutorials/README.md's table does not link {missing}. A tutorial nobody links "
            f"is a tutorial nobody finds."
        )

    def test_tutorial_09_excerpts_match_the_committed_suite(self):
        # Tutorial 09 quotes `tasks/skills/ci-outcome.yaml` and its rows file as excerpts.
        # An excerpt is a derived surface: when the suite gained a second `includes` slot
        # and an `expected_snippet_2` field, the page kept showing the old shape and a
        # reader copying it would build a suite that raises at expansion. Compared against
        # the real files rather than pinned, so the suite stays free to change.
        import json
        import re

        page = (self.REPO_ROOT / "docs" / "tutorials" / "09-optimizing-a-skill-body.md").read_text(encoding="utf-8")
        rows = [
            json.loads(ln)
            for ln in (self.REPO_ROOT / "tasks" / "skills" / "ci-outcome-rows.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        real_fields = set(rows[0])

        # 1. The illustrative JSONL row must carry exactly the fields a real row does.
        shown = [json.loads(m) for m in re.findall(r'^\{"id".*\}$', page, re.M)]
        assert shown, "tutorial 09 no longer shows a sample dataset row; this sensor reads it"
        for row in shown:
            assert set(row) == real_fields, (
                f"tutorial 09's sample row has fields {sorted(set(row))}, but a committed row has "
                f"{sorted(real_fields)}. A reader copies that shape — a missing ${{row.*}} field "
                f"raises at expansion."
            )

        # 2. Every ${row.*} the page's YAML excerpt references must exist on a real row.
        referenced = set(re.findall(r"\$\{row\.([A-Za-z_][A-Za-z0-9_]*)\}", page))
        missing = referenced - real_fields
        assert not missing, f"tutorial 09 references ${{row.{sorted(missing)}}}, which no committed row carries"

        # 3. The excerpt must not UNDER-state the gated criterion: if the suite grades two
        #    snippets, showing one teaches a shape that scores differently from the file.
        suite = (self.REPO_ROOT / "tasks" / "skills" / "ci-outcome.yaml").read_text(encoding="utf-8")
        for field in ("expected_snippet", "expected_snippet_2"):
            if f"${{row.{field}}}" in suite:
                assert f"${{row.{field}}}" in page, (
                    f"the committed suite grades ${{row.{field}}} but tutorial 09's excerpt omits it"
                )

    def test_docs_state_the_right_criterion_type_count(self):
        # Same drift class as the skill count below, one layer down: a hand-maintained
        # number describing a set the registry derives. It had already rotted — three sites
        # said 14 against a registry of 15 — and nothing noticed, because the only guarded
        # surface was CLAUDE.md's heading, which happened to be right.
        #
        # Derived from the registry, never from a literal here: adding a criterion must not
        # require editing this test, only the prose it points at.
        import re

        from coder_eval.criteria import CriterionRegistry, init_criteria

        init_criteria(validate=False)
        count = len(CriterionRegistry.list_types())
        # \b on the number, or "5 criterion types" matches inside a correct "15 criterion
        # types" and the sensor reports a failure that is not there.
        patterns = (r"\b(\d+) criterion types", r"\b(\d+) success criteria types", r"Success Criteria \((\d+) types\)")
        offenders = []
        for rel in (
            "docs/TASK_DEFINITION_GUIDE.md",
            "CLAUDE.md",
            "README.md",
            *sorted(str(p.relative_to(self.REPO_ROOT)) for p in (self.REPO_ROOT / "docs" / "tutorials").glob("*.md")),
        ):
            text = _normalized(self.REPO_ROOT / rel)
            for pat in patterns:
                offenders += [f"{rel}: {m.group(0)!r}" for m in re.finditer(pat, text) if int(m.group(1)) != count]
        assert not offenders, (
            f"the registry has {count} criterion types, but these surfaces state another count: "
            f"{offenders}. The count is derived from `CriterionRegistry.list_types()` — update the prose."
        )

    def test_skill_docs_surfaces_state_the_right_count(self):
        # The companion to the test above, which only checks that each NAME appears. These
        # surfaces also state the count in prose, and adding the sixth skill meant hand-editing
        # seven such sites across four files. Without this, a seventh ships with every count
        # silently wrong — the exact drift that repair was. Derived from disk: no count is
        # written down here, and the matcher itself lives in `_wrong_skill_count_offenders`
        # so the wrapped-phrase self-test below can exercise the real thing.
        count = len(PLUGIN_SKILLS)
        auto = count - sum(1 for v in SKILL_DISABLE_MODEL_INVOCATION.values() if v)
        offenders = _wrong_skill_count_offenders(
            {surface: self.REPO_ROOT / surface for surface in SKILL_DOC_SURFACES},
            count=count,
            auto=auto,
        )
        assert not offenders, (
            f"there are {count} shipped skills, but these surfaces still state another count: "
            f"{offenders}. Update the prose alongside the table."
        )

    def test_tutorial_08_row_table_matches_the_committed_jsonl(self):
        # The page's Step-1 table claimed 21 rows against a file holding 28, with the
        # `analyze` row count off by seven — added in Part 2 and never back-propagated. A
        # reader sizing their own suite from that table sizes it from a number that has not
        # been true for two revisions, and nothing in the build noticed.
        #
        # Derived, not duplicated: the JSONL is the source and the table is the surface, so
        # this compares the two rather than pinning either.
        import collections
        import json
        import re

        page = (self.REPO_ROOT / "docs" / "tutorials" / "08-optimizing-a-skill-description.md").read_text(
            encoding="utf-8"
        )
        rows_file = self.REPO_ROOT / "tasks" / "skills" / "lint-tasks-activation-rows.jsonl"
        rows = [json.loads(ln) for ln in rows_file.read_text(encoding="utf-8").splitlines() if ln.strip()]

        actual = collections.Counter(r["expected_skill"] for r in rows)
        by_split = collections.Counter((r["expected_skill"], r.get("split")) for r in rows)

        # | Kind | `expected_skill` | Total | train | test |
        table = re.findall(
            r"^\|\s*(?:Positive|Distractor|Sibling-owned)\s*\|\s*`([^`]*)`\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|",
            page,
            re.M,
        )
        assert table, "tutorial 08's Step-1 row table is gone or reshaped; this sensor reads it by column"

        stated_total = 0
        for raw_skill, total, train, test in table:
            skill = "" if raw_skill == '""' else raw_skill
            stated_total += int(total)
            assert (int(total), int(train), int(test)) == (
                actual[skill],
                by_split[(skill, "train")],
                by_split[(skill, "test")],
            ), (
                f"tutorial 08's table says {skill!r} has {total} rows ({train} train / {test} test); "
                f"{rows_file.name} has {actual[skill]} ({by_split[(skill, 'train')]}/{by_split[(skill, 'test')]}). "
                "Recompute the table from the file — a reader sizes their own suite from it."
            )
        assert stated_total == len(rows), (
            f"tutorial 08's table rows sum to {stated_total}; the committed JSONL has {len(rows)} rows"
        )
        assert f"**{len(rows)} rows**" in page, (
            f"tutorial 08 no longer states the committed row count as **{len(rows)} rows** in prose"
        )

    def test_tutorial_08_shows_stage_b_as_three_separate_invocations(self):
        # The single most dangerous edit in this whole area. Suite rollups are keyed on
        # (variant, suite), so `--repeats 3` pools all three replicates into ONE suite.json
        # with one confusion matrix — the per-replicate F1 the activation gate compares
        # would not exist, and the gate would silently compare a number against itself.
        # Now that the page shows real command lines, "simplifying" them is a one-line edit.
        page = self.REPO_ROOT / "docs" / "tutorials" / "08-optimizing-a-skill-description.md"
        lines = page.read_text(encoding="utf-8").splitlines()

        # Matched on heading TEXT at any level: pinning `###` broke the first time the page
        # was legitimately restructured, which is a sensor failing for a reason that has
        # nothing to do with what it guards.
        def _is_heading(ln: str) -> bool:
            return ln.lstrip("#") != ln and ln.lstrip("#").startswith(" ")

        start = next(i for i, ln in enumerate(lines) if _is_heading(ln) and "Stage B" in ln)
        end = next(i for i, ln in enumerate(lines[start + 1 :], start + 1) if _is_heading(ln))
        block = "\n".join(lines[start:end])

        assert block.count("--run-dir") >= 3, (
            "tutorial 08's Stage B no longer shows THREE separate invocations with distinct "
            f"--run-dir values (found {block.count('--run-dir')}). Three run directories are "
            "what produce three suite.json files; the gate has nothing to read without them"
        )
        # Scoped to the fenced COMMANDS, not the prose: the section's own warning says
        # "**not** `--repeats 3`", and a sensor that fired on the warning would be telling
        # the author to delete the very sentence it exists to protect.
        fenced, inside = [], False
        for line in lines[start:end]:
            if line.strip().startswith("```"):
                inside = not inside
                continue
            if inside:
                fenced.append(line)
        commands = "\n".join(fenced)
        assert "--repeats" not in commands, (
            "tutorial 08's activation Stage B now RUNS `--repeats` — rollups pool replicates "
            "into one suite.json, so the per-replicate F1 this gate compares would not exist. "
            "`--repeats` is correct at Stage C only, where paired_comparison averages per row "
            "BEFORE pairing"
        )
        assert "not** `--repeats 3`" in block, (
            "tutorial 08's Stage B no longer WARNS against --repeats 3. The commands are now "
            "shown, so the warning is what stops the next reader collapsing them"
        )

    def test_tutorial_09_keeps_the_claims_a_reader_most_needs(self):
        # Tutorial 09 reports a NULL result, and the facts that make it useful are the ones
        # an editor tightening a long page would trim first: the suite's shape, the setting
        # that decided the outcome, why the round stopped, and the denominator rule. Same
        # deletion-sensor shape as the optimize-skill guard above.
        text = _normalized(self.REPO_ROOT / "docs" / "tutorials" / "09-optimizing-a-skill-body.md")

        for token, why in (
            (
                "one dataset-backed task",
                "the outcome suite's shape — separate task files produce no rollup to rank and "
                "make --split test silently re-run the train rows",
            ),
            (
                "disallowed_tools",
                "denying sub-agent delegation is the setting that moved engaged rows from 0.333 "
                "to 1.000; an allowlist cannot express it",
            ),
            (
                "disable-model-invocation",
                "the round's actual root cause: the Skill tool REFUSES such a call, so the body "
                "never loads and every criterion scores the model's prior knowledge instead",
            ),
            (
                "read the call's *parameters* and never its *result*",
                "why it stayed hidden — the engagement criterion reported yes for a refused "
                "call, which is what made four arms tie exactly",
            ),
            (
                "completion_rate",
                "an errored row is excluded from the aggregate, so it never shows up as a low "
                "score — only as a denominator that shrank",
            ),
        ):
            assert token in text, f"tutorial 09 lost {token!r} — {why}"

    def test_user_guide_documents_every_row_selector_on_plan(self):
        """`plan`'s flag table must name every selector the model declares.

        Derived from `ROW_SELECTOR_FLAGS` rather than a hand-typed list, so a fourth selector is
        covered here automatically — the same reason CE043 reads the mapping instead of restating
        it. This is the cheap, targeted half of a general "every long flag has a USER_GUIDE
        section" rule; it deliberately checks the `plan` section only, because that is the surface
        this pairing exists to keep honest. The table already lacked `--split` before the
        selectors were added to `plan` at all, which is how the gap went unnoticed.
        """
        from coder_eval.models import ROW_SELECTOR_FLAGS

        # RAW text, not `_normalized`: that helper collapses newlines, and this assertion is
        # about table ROWS, which only exist while the line structure does.
        guide = (PLUGIN_ROOT.parent.parent / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
        marker = "### `coder-eval plan`"
        assert marker in guide, "the USER_GUIDE no longer has a `coder-eval plan` section"
        section = guide.split(marker, 1)[1].split("\n### ", 1)[0]
        # A table ROW, not merely the token: every selector is also NAMED in its siblings' prose
        # ("applied before --sample / --sample-per-stratum"), so a substring test stays green
        # after the row documenting the flag is deleted. Verified: it did.
        rows = [line for line in section.splitlines() if line.lstrip().startswith("| `")]
        documented = {line.split("`")[1].split()[0].rstrip(",") for line in rows if "`" in line}
        missing = sorted(flag for flag in ROW_SELECTOR_FLAGS.values() if flag not in documented)
        assert not missing, (
            f"docs/USER_GUIDE.md's `plan` flag table does not document {missing}. `plan` is the "
            "pre-spend preview of a run, so a selector it accepts but never documents is one a "
            "user pays to discover."
        )
