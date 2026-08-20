"""Lint tests: Every other shipped surface: the skill set itself, its frontmatter, and what each skill reads."""

import re
from pathlib import Path

import pytest
import yaml

from tests.lint_tests.plugin_base import PluginArtifactsBase
from tests.lint_tests.shared import (
    EVAL_ROOT_DEFAULT_PHRASINGS,
    PLUGIN_ROOT,
    PLUGIN_SKILLS,
    PLUGIN_TEXT_FILES,
    REPO_PATH_TOKENS,
    REPO_ROOT,
    RUBRIC_READERS,
    SKILL_DISABLE_MODEL_INVOCATION,
    SKILL_LISTING_BUDGET_CHARS,
    SKILL_NEEDS_EVAL_ROOT_DISCOVERY,
    SKILLS_REQUIRING_THE_CLI,
    _normalized,
    _skill_frontmatter,
)


@pytest.mark.lint
class TestTheShippedSkills(PluginArtifactsBase):
    """Every other shipped surface: the skill set itself, its frontmatter, and what each skill reads.

    One of five classes carved out of `TestPluginArtifacts`; the shared class attributes and
    grader helpers live on :class:`PluginArtifactsBase`.
    """

    def test_task_skill_documents_row_roles_and_check_floor(self):
        # The two authoring-time rules an author cannot infer from a file that validates. A suite
        # can satisfy every schema in the tree and still be incapable of resolving anything.
        text = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        for token, why in (
            (
                "discriminator",
                "a row the incumbent already passes cannot show improvement, only regression — "
                "conflating the two roles is what produces a suite of mostly dead weight",
            ),
            ("guard", "the other half of the role split, and the reason a 1.000 row is not an error"),
            (
                "temptation test",
                "a discriminator whose lazy implementation already satisfies the rule sits at the "
                "ceiling and measures nothing, however many arms are run through it",
            ),
            (
                "Depth over breadth",
                "one row per rule maximises the denominator while minimising per-rule headroom — "
                "the worst possible suite shape, and the one an author writes by default",
            ),
        ):
            assert token in text, f"task/SKILL.md no longer states {token!r} — {why}"

        assert "min / mean / max" in text and "minimum of **4**" in text, (
            "task/SKILL.md's outcome-mode validation lost the applicable-check floor. Below four "
            "checks a row behaves like a binary grader, which manufactures the execution gate's "
            "zero-variance refusal out of a suite that looks fine"
        )

    def test_bundled_plugin_root_references_resolve(self):
        # Skills point at their own bundled files with `${CLAUDE_PLUGIN_ROOT}/...`, which is
        # resolved by Claude Code at runtime and by nothing at authoring time. A pointer to a
        # file that does not exist is therefore invisible until a user follows it — and the
        # skills' whole value is handing over an artifact rather than describing one.
        # Caught for real: optimize-skill shipped a pointer at reference/templates/outcome.yaml
        # one commit before that file existed, past 344 green lint tests.
        import re

        broken: list[str] = []
        for doc in (p for p in PLUGIN_TEXT_FILES if p.suffix == ".md"):
            text = doc.read_text(encoding="utf-8")
            for match in re.finditer(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)", text):
                target = match.group(1).rstrip(".")
                if not (PLUGIN_ROOT / target).exists():
                    broken.append(f"{doc.relative_to(PLUGIN_ROOT)} -> {target}")
        assert not broken, (
            f"these bundled files point at ${{CLAUDE_PLUGIN_ROOT}} paths that do not exist: "
            f"{sorted(set(broken))}. An installed plugin is copied without its parent directories, "
            "so the reference resolves to nothing and the skill hands the user a dead path."
        )

    def test_reachability_guidance_names_the_plugin_root_layout(self):
        # A local plugin path must be a PLUGIN ROOT — a directory holding `skills/`, so the
        # skill sits at `<path>/skills/<name>/SKILL.md`. A manifest is optional (the
        # namespace then defaults to the directory name). Pointing at a bare directory of
        # skill directories loads NOTHING, silently, and every positive row scores 0 —
        # indistinguishable from a skill that never triggers.
        #
        # The shipped guidance said the opposite (".claude/skills" for
        # ".claude/skills/my-skill/SKILL.md"), so every generated suite reported recall 0.0.
        # Caught only by running one for real. This pins the corrected form in all three
        # places that state it.
        surfaces = {
            "activation template": self.TEMPLATES / "activation.yaml",
            "check-skill": PLUGIN_ROOT / "skills" / "check-skill" / "SKILL.md",
            "optimize-skill": PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md",
            "ci": PLUGIN_ROOT / "skills" / "ci" / "SKILL.md",
            "docs/PLUGIN.md": self.REPO_ROOT / "docs" / "PLUGIN.md",
            "tutorial 07": self.REPO_ROOT / "docs" / "tutorials" / "07-plugin-in-claude-code.md",
            "tutorial 08": self.REPO_ROOT / "docs" / "tutorials" / "08-optimizing-a-skill-description.md",
            "tutorial 09": self.REPO_ROOT / "docs" / "tutorials" / "09-optimizing-a-skill-body.md",
        }
        for name, path in surfaces.items():
            text = path.read_text(encoding="utf-8")
            assert "plugin root" in text.lower(), (
                f"{name} no longer says the plugin `path` must be a PLUGIN ROOT — a bare "
                "directory of skill directories loads nothing and scores recall 0.0"
            )
            # The required layout, spelled out. `skills/` alone would NOT do: the pre-fix
            # (wrong) text contained it too, via `.claude/skills/my-skill/SKILL.md`, so an
            # assertion on the bare token passes on exactly the guidance it must exclude.
            assert "skills/<name>/SKILL.md" in text or "skills/<skill-name>/SKILL.md" in text, (
                f"{name} no longer shows the required `<path>/skills/<name>/SKILL.md` layout"
            )

        # No surface may hand out a SKILL_SOURCE_PATH ending in /skills, nor revive the
        # "directory containing the skill's directory" rule. Both are the pre-fix form, and
        # the ci skill's copy of it shipped straight into users' CI workflows.
        for name, path in surfaces.items():
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if "SKILL_SOURCE_PATH=" in line or "SKILL_SOURCE_PATH=" in line.replace('"', ""):
                    value = line.split("SKILL_SOURCE_PATH=", 1)[1].strip().strip('"').rstrip("`")
                    assert not value.endswith("/skills"), (
                        f"{name}:{line_no} assigns SKILL_SOURCE_PATH a path ending in /skills "
                        f"({value!r}) — that is one level too deep and loads NOTHING; the "
                        "plugin root is its parent"
                    )
            assert "directory *containing* the skill" not in text, (
                f"{name} revived the pre-fix rule 'the directory containing the skill's "
                "directory' — a bare directory of skill directories loads nothing"
            )

        # The user-facing surfaces must name the trap by example, since the wrong form is
        # the intuitive one.
        for name in ("activation template", "check-skill", "ci", "docs/PLUGIN.md", "tutorial 07", "tutorial 08"):
            text = surfaces[name].read_text(encoding="utf-8")
            assert "`.claude/skills`" in text and ".claude" in text, (
                f"{name} no longer contrasts `.claude` against `.claude/skills` — the wrong "
                "path is the intuitive one, so naming it is the whole point"
            )

    @pytest.mark.parametrize("skill", PLUGIN_SKILLS, ids=[p.parent.name for p in PLUGIN_SKILLS])
    def test_skill_md_frontmatter_is_valid(self, skill: Path):
        # `claude plugin validate --strict` does NOT check skill frontmatter, so this
        # test is the only guard against a typo'd key silently disabling a skill.
        # This set is the PLUGIN's house style, not the specification's limit — the spec
        # defines many more keys (`model`, `effort`, `context`, `agent`, `hooks`, …).
        # Keeping it narrow is deliberate: an unexplained `context: fork` or `model:` on a
        # shipped skill is exactly what should surface for review rather than pass silently.
        supported = {"description", "when_to_use", "disable-model-invocation", "allowed-tools", "disallowed-tools"}
        meta = _skill_frontmatter(skill)

        unknown = set(meta) - supported
        assert not unknown, (
            f"{skill}: frontmatter key(s) {sorted(unknown)} are outside the set this plugin "
            f"deliberately restricts itself to ({sorted(supported)}). If one is genuinely needed, "
            "add it here with a reason rather than working around it."
        )
        assert isinstance(meta.get("description"), str) and meta["description"].strip(), (
            f"{skill}: `description` must be a non-empty string — it is what the model matches on"
        )
        tools = meta.get("allowed-tools")
        if tools is not None:
            assert isinstance(tools, list), f"{skill}: `allowed-tools` must be a YAML array of bare tool names"
            for tool in tools:
                assert isinstance(tool, str) and "(" not in tool, (
                    f"{skill}: `allowed-tools` entry {tool!r} uses the scoped form from .claude/commands; "
                    "the plugin spec takes bare names like 'Bash'"
                )

    @pytest.mark.parametrize("skill", PLUGIN_SKILLS, ids=[p.parent.name for p in PLUGIN_SKILLS])
    def test_cli_driving_skills_preflight_the_version_check(self, skill: Path):
        # Both READMEs tell users that a CLI-driving skill checks `coder-eval --version`
        # and stops with an install hint. Nothing verified that, and `task` did not do it
        # while invoking the CLI twice — so a user without the CLI wrote N task files and
        # then got a bare `command not found`. Declared as a set so a skill that STOPS
        # driving the CLI has to be removed deliberately.
        has_check = "coder-eval --version" in skill.read_text(encoding="utf-8")
        if skill.parent.name in SKILLS_REQUIRING_THE_CLI:
            assert has_check, (
                f"{skill} shells out to the coder-eval CLI but never preflights "
                "`coder-eval --version` — the user learns it is missing only mid-flow"
            )
        else:
            assert not has_check, (
                f"{skill} preflights `coder-eval --version` but is not in "
                "SKILLS_REQUIRING_THE_CLI — add it there, or drop the check"
            )

    @pytest.mark.parametrize("skill", PLUGIN_SKILLS, ids=[p.parent.name for p in PLUGIN_SKILLS])
    def test_model_invocation_flags_match_the_design(self, skill: Path):
        name = skill.parent.name
        assert name in SKILL_DISABLE_MODEL_INVOCATION, (
            f"{name} is a new skill — declare whether it is explicit-invocation only in SKILL_DISABLE_MODEL_INVOCATION"
        )
        meta = _skill_frontmatter(skill)
        expected = SKILL_DISABLE_MODEL_INVOCATION[name]
        assert meta.get("disable-model-invocation", False) is expected, (
            f"{skill}: expected disable-model-invocation {expected}, got {meta.get('disable-model-invocation')!r}"
        )

    def test_every_declared_skill_ships(self):
        # The other side of SKILL_DISABLE_MODEL_INVOCATION: the per-skill tests are
        # parametrized over what is on disk, so without this a deleted skill would
        # pass everything silently.
        on_disk = {p.parent.name for p in PLUGIN_SKILLS}
        missing = sorted(set(SKILL_DISABLE_MODEL_INVOCATION) - on_disk)
        assert not missing, f"declared skill(s) with no skills/<name>/SKILL.md on disk: {missing}"

    def test_bundled_run_layout_matches_the_shared_source(self):
        # The one hand-copied file in the plugin: reference/run-layout.md mirrors
        # .claude/shared/run-layout.md verbatim, minus the pointer comment the
        # original carries on its first line. Generating one hand-written file
        # from another would add machinery without adding a source of truth, so
        # this byte-equality assert is the sensor instead.
        shared = self.REPO_ROOT / ".claude" / "shared" / "run-layout.md"
        bundled = PLUGIN_ROOT / "reference" / "run-layout.md"
        pointer, _, body = shared.read_text(encoding="utf-8").partition("\n")
        assert "plugins/coder-eval/reference/run-layout.md" in pointer, (
            f"{shared} must open with the pointer comment naming its mirror, so an editor of one "
            "file learns about the other"
        )
        assert body == bundled.read_text(encoding="utf-8"), (
            f"{bundled} drifted from {shared} — they are a verbatim mirror; copy the shared file "
            "over the bundled one (keeping the pointer comment only in the shared original)"
        )

    @pytest.mark.parametrize(
        "skill", PLUGIN_TEXT_FILES, ids=[str(p.relative_to(PLUGIN_ROOT)) for p in PLUGIN_TEXT_FILES]
    )
    def test_bundled_files_reference_no_repo_paths(self, skill: Path):
        text = skill.read_text(encoding="utf-8")
        offenders = [token for token in REPO_PATH_TOKENS if token in text]
        assert not offenders, (
            f"{skill} names this repository's path(s) {offenders} — an installed plugin is copied "
            "without its parent directories, so they do not exist at runtime. Bundle what the skill "
            "needs under plugins/coder-eval/ and address it via ${CLAUDE_PLUGIN_ROOT}."
        )

    def test_lint_tasks_skill_is_read_only(self):
        # Assert BOTH keys, because neither alone carries the contract: `allowed-tools` names
        # the tools this skill expects to use, `disallowed-tools` removes the write tools from
        # the pool. Assert only the allowlist and a denylist regression passes; assert only the
        # denylist and a widened allowlist (say `Bash`) passes.
        #
        # Neither key is the real guarantee, which is why the skill body carries a STANDING
        # prohibition too: per the skills spec, `disallowed-tools` "clears when you send your
        # next message", and this skill's step 1 deliberately asks the user one before linting a
        # whole directory. So the frontmatter covers the first turn and the prose covers the
        # rest — `test_lint_tasks_read_only_rule_survives_the_next_turn` guards that half.
        meta = _skill_frontmatter(PLUGIN_ROOT / "skills" / "lint-tasks" / "SKILL.md")

        # `and allowed` first: an ABSENT allowed-tools is the weakest state, not the
        # strongest, and an empty set would satisfy the subset check vacuously.
        allowed = set(meta.get("allowed-tools") or [])
        assert allowed and allowed <= {"Read", "Glob", "Grep"}, (
            f"lint-tasks pre-approves {sorted(allowed - {'Read', 'Glob', 'Grep'})} — an allowlist, "
            "not a denylist, so anything beyond reading breaks the advisory contract"
        )
        assert {"Write", "Edit", "NotebookEdit"} <= set(meta.get("disallowed-tools") or []), (
            "lint-tasks must name every write tool in `disallowed-tools` — that is the half "
            "that actually removes them from the pool"
        )

    def test_lint_tasks_read_only_rule_survives_the_next_turn(self):
        # The frontmatter deny is turn-scoped ("clears when you send your next message"), and
        # step 1 asks the user a question before linting a directory — so for most of a real
        # review the prose rule is the only thing enforcing read-only. Deleting it would leave
        # a skill that advertises "Read-only." in its description with nothing behind it after
        # the first reply.
        text = _normalized(PLUGIN_ROOT / "skills" / "lint-tasks" / "SKILL.md")
        assert "Never modify a file" in text, "lint-tasks lost its standing read-only prohibition"
        assert "standing, not per-turn" in text, (
            "lint-tasks no longer says its read-only rule outlives the frontmatter deny — the "
            "deny clears on the user's next message, which step 1 explicitly solicits"
        )

    def test_lint_tasks_does_not_flag_the_shipped_activation_template(self):
        # A prose sensor, guarding against deletion rather than judging quality — but it
        # covers the one interaction where two shipped skills could contradict each other:
        # the activation suite `check-skill` writes is exactly the shape a naive coverage
        # pass reads as "one criterion, no content check" and flags. The worked example is
        # in the repo (reference/templates/activation.yaml), so the carve-out cannot be
        # written vaguely: it must be structural, since the file may be renamed.

        text = (PLUGIN_ROOT / "skills" / "lint-tasks" / "SKILL.md").read_text(encoding="utf-8")
        for token in ("dataset:", "skill_triggered", "classification_match", "suite_thresholds"):
            assert token in text, (
                f"lint-tasks must name {token!r} in its activation-suite carve-out — without the "
                "structural detection it will flag the suites check-skill generates as broken"
            )
        assert "do not apply" in text, (
            "lint-tasks names the carve-out's conditions but no longer EXEMPTS anything — an "
            "inverted carve-out would keep every token above and still flag activation suites"
        )
        # The conditions must still describe the template check-skill actually copies, or the
        # carve-out has quietly stopped covering the one file it exists for.
        template = yaml.safe_load((self.TEMPLATES / "activation.yaml").read_text(encoding="utf-8"))
        assert template.get("dataset"), "the shipped activation template lost its `dataset:` block"
        types = {c.get("type") for c in template["success_criteria"]}
        assert types & {"skill_triggered", "classification_match"}, (
            f"the shipped activation template's criteria are {sorted(types)} — no longer "
            "classification-style, so lint-tasks' structural carve-out would not match it"
        )
        assert any(c.get("suite_thresholds") for c in template["success_criteria"]), (
            "the shipped activation template lost `suite_thresholds` — the carve-out's third "
            "condition no longer holds, so lint-tasks would flag the suite check-skill writes"
        )

    def test_skill_listing_budget_is_bounded(self):
        # See SKILL_LISTING_BUDGET_CHARS for why a plugin should self-limit here.
        # Filesystem-derived: no hardcoded skill names, no per-skill numbers.
        per_skill: dict[str, int] = {}
        for path in PLUGIN_SKILLS:
            meta = _skill_frontmatter(path)
            per_skill[path.parent.name] = len(meta.get("description") or "") + len(meta.get("when_to_use") or "")
        total = sum(per_skill.values())
        assert total <= SKILL_LISTING_BUDGET_CHARS, (
            f"the plugin's skill descriptions total {total} characters, over the "
            f"{SKILL_LISTING_BUDGET_CHARS} ceiling (per skill: {sorted(per_skill.items())}). Prefer "
            "trimming an existing description; the listing budget is shared with every skill the "
            "user has installed. Raising the ceiling is allowed in a commit that says why."
        )

    def test_cli_driving_skills_are_named_in_the_install_prose(self):
        # Both READMEs promise that the CLI-driving skills preflight `coder-eval --version`.
        # SKILLS_REQUIRING_THE_CLI is the declared set, but nothing tied it to the prose, so
        # `optimize-skill` joined the set and both files went on naming three of four — a
        # first-run user of the fourth gets a bare `command not found`. Derived from the set,
        # with no skill names written down here, so a fifth cannot ship silently either.
        #
        # Scoped to the install paragraph, not the whole file: every skill name appears
        # SOMEWHERE in both documents, so a whole-file match would assert nothing at all.
        for surface in ("plugins/coder-eval/README.md", "docs/PLUGIN.md"):
            text = _normalized(self.REPO_ROOT / surface)
            anchor = text.find("coder-eval --version")
            assert anchor != -1, (
                f"{surface} no longer mentions `coder-eval --version` — the install paragraph "
                "this sensor anchors on is gone, so the promise it checks is unverifiable"
            )
            # The sentence naming the skills sits just before the anchor; take a generous
            # window either side rather than guessing at sentence boundaries.
            paragraph = text[max(0, anchor - 400) : anchor + 400]
            missing = sorted(name for name in SKILLS_REQUIRING_THE_CLI if f"`{name}`" not in paragraph)
            assert not missing, (
                f"{surface}'s install paragraph does not name {missing}, which "
                f"SKILLS_REQUIRING_THE_CLI declares must preflight `coder-eval --version`. A "
                "user of an unnamed skill is told nothing about needing the CLI and meets a "
                "bare `command not found` mid-task."
            )

    def test_analyze_routes_fixes_to_the_right_layer(self):
        # A shallow keyword sensor: it guards against the guidance being DELETED, not
        # against it being badly written. The root-cause token has to survive too, because
        # the routing rule is attached to it — `prompt_gap` naming a missing piece of
        # knowledge is meaningless if nothing says which layer should have supplied it.
        # Whitespace-collapsed so a reflowed paragraph does not fail it — these files are
        # hard-wrapped prose, and a sensor that breaks on rewrapping trains people to
        # distrust it.
        text = _normalized(PLUGIN_ROOT / "skills" / "analyze" / "SKILL.md")
        assert "prompt_gap" in text, "analyze lost the `prompt_gap` root-cause token"
        for phrase in ("fix the prompt", "file the tool bug", "which layer"):
            assert phrase in text, (
                f"analyze no longer routes a prompt_gap to the layer that should own it "
                f"(missing {phrase!r}) — patching the prompt instead turns the score green "
                "and changes nothing for users"
            )

    @pytest.mark.parametrize(
        "doc",
        [p for p in PLUGIN_TEXT_FILES if p.suffix == ".md"],
        ids=[str(p.relative_to(PLUGIN_ROOT)) for p in PLUGIN_TEXT_FILES if p.suffix == ".md"],
    )
    def test_bundled_markdown_fences_balance(self, doc: Path):
        # A skill body is an instruction document; an unbalanced fence silently swallows
        # everything after it. `analyze` shipped a ```markdown block containing a ```diff
        # block, and because a closing fence may not carry an info string, the inner
        # opener closed the outer block early and the next bare ``` opened one that never
        # closed — burying 32 lines including the whole Principles section. Nothing caught
        # it, because it is still valid YAML frontmatter and valid-ish Markdown.
        #
        # CommonMark rule applied here: a fence closes only on a run of backticks at least
        # as long as the opener AND carrying no info string. Nesting therefore requires the
        # OUTER fence to be longer (````markdown wrapping ```diff).
        open_len = 0
        for n, raw in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line.startswith("```"):
                continue
            ticks = len(line) - len(line.lstrip("`"))
            info = line[ticks:].strip()
            if open_len == 0:
                open_len = ticks
                opened_at = n
            elif ticks >= open_len and not info:
                open_len = 0
        assert open_len == 0, (
            f"{doc}: code fence opened at line {opened_at} is never closed. A closing fence "
            "may not carry an info string, so a nested block needs a LONGER outer fence "
            "(````markdown around ```diff). Everything after the opener renders as code."
        )

    def test_cli_setup_is_bundled_and_read_by_the_cli_driving_skills(self):
        # Installing the plugin does not install the CLI, so every CLI-driving skill has to
        # handle a missing binary. The POLICY for that (offer, ask, verify, never install
        # unprompted) is declared once in reference/cli-setup.md; each skill keeps only the
        # one-line check locally. Both halves are asserted: the reference ships, and every
        # skill that needs it points at it.
        setup = PLUGIN_ROOT / "reference" / "cli-setup.md"
        assert setup.exists() and setup.read_text(encoding="utf-8").strip(), (
            f"{setup} must exist and be non-empty — the CLI-driving skills read it at runtime"
        )
        pointer = "${CLAUDE_PLUGIN_ROOT}/reference/cli-setup.md"
        for name in sorted(SKILLS_REQUIRING_THE_CLI):
            text = (PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            assert pointer in text, (
                f"{name} shells out to the CLI but no longer points at {pointer} — it has "
                "forked the install policy, or dropped it"
            )
        # The policy is a shared declaration, so a skill must not restate the install
        # command: two copies drift, and the wrong installer is a real footgun.
        for name in sorted(SKILLS_REQUIRING_THE_CLI):
            text = (PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            assert "uv tool install" not in text and "pip install" not in text, (
                f"{name} restates the install command that reference/cli-setup.md declares — "
                "point at the reference instead"
            )

    def test_task_skill_declares_repo_convention_precedence(self):
        # The bundled rubric is a floor, not a house style. A repository that already
        # authors tasks has its own conventions, and a skill that quietly overrides them
        # with the plugin's defaults produces work its maintainers have to undo — so the
        # precedence has to be stated, and the conventions adopted have to be reported
        # (otherwise "I followed the repo" is unfalsifiable).
        text = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        assert "the repo wins" in text or "the repository wins" in text, (
            "task no longer says repo-local convention beats the bundled rubric — it will "
            "impose the plugin's defaults on a repository that already declared its own"
        )
        assert "conventions you adopted" in text or "conventions adopted" in text, (
            "task does not report WHICH conventions it adopted — an unreported precedence "
            "rule cannot be checked by the person reading the result"
        )
        rubric = _normalized(PLUGIN_ROOT / "reference" / "task-rubric.md")
        assert "the repo wins" in rubric or "the repository wins" in rubric, (
            "reference/task-rubric.md does not carry the same precedence line — the rubric "
            "is read at review time too, and must not contradict the authoring skill"
        )

    def test_task_skill_documents_outcome_mode(self):
        # `/coder-eval:optimize-skill`'s execution track says "no suite -> stop and point at
        # /coder-eval:task". That instruction was unfollowable: `task` had no notion of a
        # dataset, a row or a split, and its guidance was explicitly one-file-per-task — the
        # precise anti-pattern, since N task files yield no suite.json rollup and `--split test`
        # silently re-runs the train rows. This asserts the mode exists and names what it emits.
        text = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        assert "Outcome-suite mode" in text, (
            "task/SKILL.md no longer documents outcome-suite mode — optimize-skill's execution "
            "track hands suite authoring here and has nothing to hand it to"
        )
        assert "dataset-backed" in text and "one ROW per scenario" in text, (
            "task/SKILL.md does not state the dataset-backed, one-row-per-scenario constraint, "
            "which is the whole structural difference between the modes"
        )
        # Step 2.5 is what makes the rows DERIVED from the skill rather than invented beside it.
        assert "Rule inventory" in text and "R1" in text, (
            "task/SKILL.md no longer requires a numbered rule table (Step 2.5) — without it the "
            "rows are whatever the author thought of, and nothing says what the suite covers"
        )
        assert "checked mechanically" in text, (
            "the rule table no longer asks how each rule could be checked MECHANICALLY, which is "
            "what turns an inventory into criteria"
        )
        # All five artifacts, by the names the rest of the plugin uses for them.
        for artifact in ("suite YAML", "rows JSONL", "fixture directory", "grader script", "expectations"):
            assert artifact in text, (
                f"task/SKILL.md's outcome-mode file list no longer names the {artifact!r} — an "
                "artifact nobody is told to write is an artifact that does not get written"
            )
        assert "OUTSIDE the fixture" in text, (
            "task/SKILL.md no longer says the grader and its expectations live OUTSIDE the fixture "
            "directory. Everything under the fixture is copied into every sandbox, so the answer "
            "key ships with the exam — measured at +0.030 mean on a real suite"
        )

    def test_task_skill_supersedes_one_file_per_task(self):
        # A parity sensor, not two independent ones: the ORIGINAL guidance and its outcome-mode
        # OVERRIDE must both be present. Deleting either alone is the failure — the first leaves
        # default mode undocumented, the second leaves outcome mode contradicting the file it is
        # written in, and a reader following the wrong half writes N task files that produce no
        # rollup.
        text = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        assert "One file per task" in text, (
            "task/SKILL.md lost the default-mode one-file-per-task guidance that outcome mode "
            "declares itself an exception to"
        )
        assert "several** task files" in text, (
            "task/SKILL.md lost the 'a single request can produce several task files' guidance — "
            "the other passage outcome mode supersedes"
        )
        assert "In outcome mode this is replaced" in text, (
            "Step 4 still says 'One file per task' with no outcome-mode override beside it. The "
            "two passages must be superseded WHERE THEY ARE READ; a rule stated only at the top "
            "of the file is not read by someone who jumped to Step 4"
        )
        assert "supersedes the one-file-per-task guidance in two places" in text, (
            "task/SKILL.md no longer names how many passages outcome mode overrides, so a third "
            "one can appear without anyone noticing it was left un-overridden"
        )

    def test_task_skill_has_discrimination_gate(self):
        # The one error class an A/B cannot detect: a wrong check biases every arm EQUALLY, so the
        # ranking, the paired test and the confirmation all agree with each other and are all wrong
        # together. Proving the grader separates a known-good artifact from a known-bad one is the
        # only place it gets caught, and it has to happen before a stage is paid for.
        text = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        assert "Step 6.5" in text, (
            "task/SKILL.md no longer has a Step 6.5 — the grader discrimination gate. Four shipped "
            "surfaces cite it by that number (see test_shipped_surfaces_cite_a_step_that_exists)"
        )
        assert "known-good" in text and "known-bad" in text, (
            "the discrimination gate no longer names both artifacts, so there is nothing to compare"
        )
        assert "separation margin" in text, (
            "the gate no longer requires the MARGIN to be reported. A gate whose result is not "
            "stated is a step someone can say they did"
        )
        # It cites the rubric rather than restating the fairness questions — one copy, asked by
        # both the skill that writes a grader and the skill that reviews one.
        assert "task-rubric.md" in text and "Grader fairness" in text, (
            "Step 6.5 no longer cites the rubric's grader-fairness section, so either the questions "
            "have been restated here (two copies to drift) or dropped"
        )
        # Step 6's split half: `--split` silently drops unlabelled rows, so every metric would be
        # computed over a smaller suite than the file suggests.
        # Anchored on the requirement, not on the flag names: `--split test` also appears in the
        # "Which mode" section, so asserting the flags alone survives deleting Step 6's paragraph.
        assert "--split train" in text and "Both must exit 0" in text, (
            "task/SKILL.md's outcome-mode validation no longer plans BOTH splits, so a partly "
            "labelled dataset ships silently — `--split` keeps the matching rows and DROPS the "
            "unlabelled ones, and every later metric is computed over the smaller suite"
        )

    def test_shipped_surfaces_cite_a_step_that_exists(self):
        # A prose cross-reference nothing guards. `test_bundled_plugin_root_references_resolve`
        # covers ${CLAUDE_PLUGIN_ROOT} FILE paths in .md files only — it cannot see "step 6.5", and
        # it does not scan .py at all. These four surfaces are what a user copies, and the step they
        # point at is the ONE safeguard against a silently-wrong grader.
        citing = (
            PLUGIN_ROOT / "reference" / "templates" / "outcome-grader" / "verify.py",
            PLUGIN_ROOT / "reference" / "templates" / "outcome-grader" / "expectations" / "core-1.json",
            PLUGIN_ROOT / "reference" / "templates" / "outcome.yaml",
            PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md",
        )
        target = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        citers = [path for path in citing if "step 6.5" in _normalized(path).casefold()]
        assert citers, (
            "no shipped surface cites the discrimination gate any more — if the step was renumbered, "
            "this test is looking for the wrong string and guards nothing"
        )
        assert "Step 6.5" in target, (
            f"{[p.name for p in citers]} point at `/coder-eval:task` step 6.5, which that skill no "
            "longer has. An author following the pointer finds nothing, and the grader ships ungated"
        )

    def test_task_rubric_carves_out_spec_from_leak_rule(self):
        # Both halves, so an edit cannot delete one and leave the other. Without the prohibition an
        # outcome prompt may state the rule under test, which grades transcription; without the
        # carve-out "follow the spec literally" is ungradeable, because it needs a spec to follow.
        rubric = _normalized(PLUGIN_ROOT / "reference" / "task-rubric.md")
        assert "MAY** state the *spec*" in rubric, (
            "the rubric lost the outcome-suite spec carve-out — an outcome prompt must be allowed to "
            "name the output path and the format, or the rule 'follow the user's spec' cannot be graded"
        )
        # No weaker fallback: a bare "MUST NEVER" is satisfied by any unrelated sentence in the
        # rubric, so the disjunct that was here loosened the sensor to nothing.
        assert "MUST NEVER** state a rule the *body*" in rubric, (
            "the rubric lost the prohibition the carve-out narrows, so the carve-out now reads as "
            "permission to put the graded behaviour in the prompt"
        )
        assert "without the skill" in rubric, (
            "the rubric no longer gives the TEST that separates the two (could an agent without the "
            "skill satisfy the check from the prompt alone?) — the rule is unappliable without it"
        )

    def test_ci_skill_covers_experiments_and_pins(self):
        # Two silent-wrong-answer bugs in an emitted workflow. Omitting the experiment
        # drops the `agent:` config it supplies, so the gate measures something other than
        # what the suite measures locally; and a `version:` input that ignores the repo's
        # pin runs the gate on a different CLI than the repo is authored against.
        text = _normalized(PLUGIN_ROOT / "skills" / "ci" / "SKILL.md")
        assert "extra-args" in text and "experiment" in text, (
            "the ci skill does not say how to pass an experiment through to the run — a "
            "suite that resolves through one silently measures something else without it"
        )
        assert "pin" in text, (
            "the ci skill no longer conditions the `version:` input on whether the repository pins a coder-eval version"
        )

    def test_ci_skill_teaches_the_split_selector(self):
        # A gate that runs the TRAIN rows scores the skill partly on its own training data and
        # drifts optimistic exactly as the skill improves — the direction that HIDES a regression.
        # The emitted workflow is copied into users' repos, so a skill that never mentions the
        # selector produces a fleet of gates quietly measuring the wrong half of every split suite.
        text = _normalized(PLUGIN_ROOT / "skills" / "ci" / "SKILL.md")
        assert "--split" in text, (
            "the ci skill does not name --split — a suite whose rows are split-labelled will be "
            "gated on whichever half the default selects, which is both halves"
        )
        # Sliced to the SECTION, not the file. `extra-args` appears three times in the
        # pre-existing experiment subsection above, so a whole-file test is unconditionally true
        # and stays green with the snippet deleted — verified.
        section = text.split("The split, if the suite carries one", 1)[-1]
        assert 'extra-args: "--split test"' in section, (
            "the ci skill names --split but never shows it INSIDE extra-args — that input is the "
            "only channel the emitted workflow has for it, and a reader who has to guess the "
            "wiring guesses a `split:` action input that does not exist"
        )
        assert "train" in text, (
            "the ci skill does not say WHY the test split is the one to gate on — without the "
            "reason a reader drops the flag as noise, and a gate on train rows looks green while "
            "the skill is being tuned against the very rows it is scored on"
        )

    def test_ci_skill_does_not_recommend_a_recursive_task_glob(self):
        # `action.yml` expands the `tasks:` input unquoted (`args+=($CE_TASKS)`) with
        # globstar OFF, so `a/**/*.yaml` degrades to `a/*/*.yaml` and silently drops every
        # top-level task — a depth-dependent "measured the wrong set" bug. nullglob is off
        # too, so an unmatched depth pattern reaches the CLI literally and exits 1. Both
        # reproduced by hand. The snippet must therefore show neither `**` in its tasks
        # value nor a fixed ladder of depths.
        skill = PLUGIN_ROOT / "skills" / "ci" / "SKILL.md"
        assert "globstar" in skill.read_text(encoding="utf-8"), (
            "the ci skill emits explicit globs but no longer says WHY — without the reason, "
            "the next reader simplifies them back to `**` and loses the top-level tasks"
        )

        # Scoped to every surface CE026 already scans, not just the skill: the `ci` skill was
        # taught to avoid `**` while five snippets across README.md, docs/CI_GATE.md and the
        # CI tutorial still showed `tasks: tests/tasks/**/*.yaml`, so the plugin contradicted
        # the repo's own onboarding docs — and those are the ones integrators copy.
        from tests.lint.action_docs import default_doc_paths

        offenders = [
            f"{path}:{n}: {line.strip()}"
            for path in default_doc_paths(REPO_ROOT)
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"^\s*tasks:.*\*\*", line)
        ]
        assert not offenders, (
            "recursive `**` glob in a documented `tasks:` input value — the action word-splits "
            "and pathname-expands that value with globstar off, so it silently drops every task "
            "above the deepest matching level. Emit explicit per-depth globs or a file "
            "list:\n\n" + "\n".join(f"  {o}" for o in offenders)
        )

    def test_check_skill_detects_before_scaffolding(self):
        # Scaffolding a second activation suite beside one that already covers the same
        # skill is worse than doing nothing: two suites drift, and the user pays for both
        # on every run. The existence check therefore has to happen BEFORE row design,
        # which is where the token spend is committed.
        text = _normalized(PLUGIN_ROOT / "skills" / "check-skill" / "SKILL.md")
        check = text.find("existing suite")
        assert check != -1 and "skill_triggered" in text, (
            "check-skill names no `existing suite` check — it will scaffold a parallel suite "
            "over one that already covers the skill"
        )
        assert "extend" in text[check : check + 600], (
            "check-skill finds an existing suite but does not offer to EXTEND it — reporting "
            "coverage and then scaffolding anyway is the same duplication with extra steps"
        )
        design = text.find("Design the rows")
        assert design != -1, "check-skill's row-design step was renamed — re-anchor this guard"
        assert check < design, (
            "the existing-suite check must come before row design — that is the step which "
            "commits the token spend this check exists to avoid"
        )

    def test_check_skill_defers_to_an_experiment_supplied_plugins_block(self):
        # A task that redeclares what the experiment layer already provides drifts from it
        # silently — the same reason `task` tells authors to omit `agent:` entirely.
        text = _normalized(PLUGIN_ROOT / "skills" / "check-skill" / "SKILL.md")
        assert "experiment" in text and "agent.plugins" in text, (
            "check-skill no longer mentions an experiment-supplied `agent.plugins` block"
        )
        assert "inherit it" in text, (
            "check-skill does not tell the agent to INHERIT an experiment-supplied "
            "`agent.plugins` rather than writing its own — a task that redeclares what the "
            "experiment provides drifts from it with nothing to catch the divergence"
        )

    def test_init_stops_when_the_repo_is_already_configured(self):
        # `init` is a scaffolder pointed at repositories that have nothing. Run against one
        # that already has a suite, it wrote a "first task" beside an existing tree and
        # reported success — so the inventory has to be able to END the skill, not just
        # precede it.
        text = _normalized(PLUGIN_ROOT / "skills" / "init" / "SKILL.md")
        assert "already configured" in text or "already has" in text, (
            "init no longer recognizes an already-configured repository"
        )
        assert "stop" in text, "init's inventory step no longer stops — it only reports"
        for redirect in ("/coder-eval:lint-tasks", "/coder-eval:analyze"):
            assert redirect in text, (
                f"init reports an existing suite but does not point at {redirect} — the user "
                "is told what they have and given nothing to do with it"
            )

    def test_lint_tasks_scale_guidance_stays_read_only(self):
        # The durable half of the scale guard. `lint-tasks` has no `Bash`, so it cannot run
        # `git diff` to find a changed set however sensible that would be — it has to ASK
        # for one. Prose drifts; the read-only contract must not. (The frontmatter tool set
        # itself is already covered by `test_lint_tasks_skill_is_read_only`.)
        text = (PLUGIN_ROOT / "skills" / "lint-tasks" / "SKILL.md").read_text(encoding="utf-8")
        for command in ("git diff", "git status"):
            assert command not in text, (
                f"lint-tasks names `{command}`, which it has no `Bash` to run — at scale it "
                "must ask the user for the changed set instead of proposing a command"
            )

    def test_cli_setup_declares_pin_resolution(self):
        # A repository that already uses coder-eval usually pins the version, and the
        # damage from ignoring that pin is asymmetric: validating with a newer CLI
        # produces schema errors indistinguishable from real ones, and "fixing" them
        # edits the repo's tasks into a shape its own pinned CLI rejects. Asserted on a
        # concrete heading plus the stop rule rather than on the token `pin`, which
        # appeared nowhere in this file before the section landed — so any passing
        # mention would have satisfied a looser test and guarded nothing.
        text = _normalized(PLUGIN_ROOT / "reference" / "cli-setup.md")
        assert "## Version pin" in text, (
            "reference/cli-setup.md lost its `## Version pin` section — the CLI-driving "
            "skills would go back to validating a pinned repository with whatever binary "
            "happens to be on PATH"
        )
        assert "If the resolved version differs from the pin, stop and say so." in text, (
            "cli-setup.md no longer states the stop rule for a pin mismatch — resolving the "
            "pin is only useful if a mismatch halts the skill instead of being reported and "
            "then ignored"
        )

    def test_cli_setup_conditions_the_upgrade_suggestion_on_a_pin(self):
        # "Version skew" used to terminate in "suggest upgrading", full stop. Against a
        # pinned repository that is the single most destructive thing these skills could
        # recommend, so the upgrade advice must now sit BEHIND the pin question.
        section = (
            (PLUGIN_ROOT / "reference" / "cli-setup.md").read_text(encoding="utf-8").partition("## Version skew")[2]
        )
        assert section.strip(), "reference/cli-setup.md lost its `## Version skew` section"
        upgrade = section.find("upgrade coder-eval")
        assert upgrade != -1, "the `Version skew` section no longer names the upgrade command at all"
        assert "pin" in section[:upgrade], (
            "`Version skew` suggests upgrading before it has asked whether the project pins "
            "a version — upgrading past a pin is a repo-breaking action, not a fix"
        )
        assert "match the pin" in " ".join(section.split()), (
            "`Version skew` no longer says what to do when a pin DOES exist — the branch "
            "that makes the upgrade advice conditional rather than merely delayed"
        )

    def test_repo_layout_is_bundled_and_read_by_its_readers(self):
        # Sibling of the cli-setup guard above, for the third shared reference. "Where does
        # this repository keep its tasks and runs?" is a question every shipped skill asks —
        # most to read the trees, `ci` to write the resolved glob into a workflow; inlining
        # the answer once per skill is how the hardcoded `tasks/` / `runs/latest` assumptions
        # got there in the first place. Both halves are asserted: the reference ships, and
        # every skill that needs it points at it. (Counts stay out of this comment on
        # purpose — the mapping below is the source of truth and a seventh skill already
        # made two hardcoded numbers here stale.)
        layout = PLUGIN_ROOT / "reference" / "repo-layout.md"
        assert layout.exists() and layout.read_text(encoding="utf-8").strip(), (
            f"{layout} must exist and be non-empty — the shipped skills read it at runtime "
            "to find the repository's eval tree"
        )
        # Both directions, so neither the mapping nor the tree can drift silently: a
        # declared skill must ship, and a shipped skill must declare its stance.
        on_disk = {p.parent.name for p in PLUGIN_SKILLS}
        declared = set(SKILL_NEEDS_EVAL_ROOT_DISCOVERY)
        assert not declared - on_disk, (
            f"SKILL_NEEDS_EVAL_ROOT_DISCOVERY names skill(s) that do not ship: {sorted(declared - on_disk)}"
        )
        assert not on_disk - declared, (
            f"new skill(s) {sorted(on_disk - declared)} have not declared whether they need eval-root "
            "discovery — add them to SKILL_NEEDS_EVAL_ROOT_DISCOVERY. `False` is fine for a skill that "
            "touches no task or run tree, but defaulting to it silently is how a hardcoded `tasks/` "
            "gets back in."
        )

        pointer = "${CLAUDE_PLUGIN_ROOT}/reference/repo-layout.md"
        for name in sorted(n for n, needed in SKILL_NEEDS_EVAL_ROOT_DISCOVERY.items() if needed):
            text = (PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            assert pointer in text, (
                f"{name} has to locate the repository's eval tree but no longer points at "
                f"{pointer} — it has forked the discovery policy, or gone back to assuming a path"
            )

    @pytest.mark.parametrize("skill", PLUGIN_SKILLS, ids=[p.parent.name for p in PLUGIN_SKILLS])
    def test_skills_do_not_hardcode_an_eval_root(self, skill: Path):
        # The other half of the guard above: pointing at the reference is worthless if the
        # skill still falls back to one repository's layout when handed nothing. Whitespace-
        # normalized because these bodies are hard-wrapped prose — the `analyze` sentence this
        # rule exists for spans two source lines, so a literal match on the raw text would be
        # green today and stay green if the sentence came back.
        text = re.sub(r"\s+", " ", skill.read_text(encoding="utf-8"))
        offenders = [phrase for phrase in EVAL_ROOT_DEFAULT_PHRASINGS if phrase in text]
        assert not offenders, (
            f"{skill} still defaults to this repository's layout ({offenders}) — a repository "
            "that names its eval tree anything else silently gets the wrong path, or none. "
            "Follow reference/repo-layout.md and discover it instead."
        )

    def test_task_rubric_is_bundled_and_read_by_its_readers(self):
        # Both directions of the shared-SSOT decision: the rubric ships, and every skill
        # that is supposed to apply it actually points at it.
        rubric = PLUGIN_ROOT / "reference" / "task-rubric.md"
        assert rubric.exists() and rubric.read_text(encoding="utf-8").strip(), (
            f"{rubric} must exist and be non-empty — `task` and `lint-tasks` read it at runtime"
        )
        pointer = "${CLAUDE_PLUGIN_ROOT}/reference/task-rubric.md"
        for name in sorted(RUBRIC_READERS):
            skill = PLUGIN_ROOT / "skills" / name / "SKILL.md"
            assert pointer in skill.read_text(encoding="utf-8"), (
                f"{skill} no longer reads {pointer} — it has silently forked the shared rubric"
            )
