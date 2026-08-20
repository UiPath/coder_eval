"""Lint tests: onboarding surfaces."""

import re
from pathlib import Path
from typing import ClassVar

import pytest

from tests.lint.cli_flags import long_flags
from tests.lint_tests.shared import PLUGIN_ROOT, REPO_ROOT


@pytest.mark.lint
class TestCE026ActionDocSurfaces:
    """CE026 — the Action's onboarding surfaces must be truthful and self-sufficient.

    The motivating bug: docs/CI_GATE.md said "there is nothing to install" above a
    copy-pasteable `uses:` step with no agent runtime, while the correcting
    prerequisite note sat 11 lines below and the tutorial's sibling snippet *did*
    show the steps. An integrator who copied it got a run that dies on a missing
    `claude` binary. Reasons over Markdown + YAML, so it lives here rather than in
    the AST-only runner (precedent: CE027-CE031).
    """

    REPO_ROOT = REPO_ROOT
    ACTION_YML = REPO_ROOT / "action.yml"
    PR_CHECKS = REPO_ROOT / ".github" / "workflows" / "pr-checks.yml"

    def test_primary_action_snippets_show_agent_runtime_prereqs(self):
        from tests.lint.action_docs import default_doc_paths, find_missing_prereqs

        findings = find_missing_prereqs(default_doc_paths(self.REPO_ROOT))
        assert not findings, (
            "\nAction quickstart snippet(s) that a reader can copy but that omit the agent-runtime "
            "prerequisite steps:\n\n" + "\n".join(f"  {f}" for f in findings)
        )

    def test_no_unqualified_zero_install_claims_near_action_snippets(self):
        from tests.lint.action_docs import default_doc_paths, find_unscoped_absolute_claims

        findings = find_unscoped_absolute_claims(default_doc_paths(self.REPO_ROOT))
        assert not findings, (
            "\nUnqualified zero-install absolute(s) beside an Action snippet — scope the claim to the "
            "channel it is about:\n\n" + "\n".join(f"  {f}" for f in findings)
        )

    def test_marketplace_links_match_the_action_listing_name(self):
        from tests.lint.action_docs import action_listing_name, default_doc_paths, find_slug_mismatches

        listing = action_listing_name(self.ACTION_YML)
        findings = find_slug_mismatches(default_doc_paths(self.REPO_ROOT), listing)
        assert not findings, (
            f"\nMarketplace link/badge(s) inconsistent with action.yml `name: {listing}` — a rename "
            "404s every one of them:\n\n" + "\n".join(f"  {f}" for f in findings)
        )

    def test_plugin_skills_are_covered_by_the_doc_scan(self):
        # The `ci` skill emits an Action snippet users copy verbatim, so it must be
        # held to the same prerequisite standard as the docs pages.
        from tests.lint.action_docs import default_doc_paths

        ci_skill = PLUGIN_ROOT / "skills" / "ci" / "SKILL.md"
        scanned = {p.resolve() for p in default_doc_paths(self.REPO_ROOT)}
        assert ci_skill.resolve() in scanned, (
            f"{ci_skill} is outside default_doc_paths() — its Action snippet would go unchecked"
        )

    def test_ci_skill_snippet_shows_agent_runtime_prereqs(self):
        from tests.lint.action_docs import find_missing_prereqs

        findings = find_missing_prereqs([PLUGIN_ROOT / "skills" / "ci" / "SKILL.md"])
        assert not findings, (
            "\nThe ci skill emits a workflow without the agent-runtime prerequisite steps — a user "
            "who copies it gets a run that dies on a missing `claude` binary:\n\n"
            + "\n".join(f"  {f}" for f in findings)
        )

    def test_action_snippets_pass_only_real_action_inputs(self):
        from tests.lint.action_docs import action_input_names, default_doc_paths, find_unknown_action_inputs

        names = action_input_names(self.ACTION_YML)
        findings = find_unknown_action_inputs(default_doc_paths(self.REPO_ROOT), names)
        assert not findings, (
            "\nAction snippet(s) passing a `with:` key action.yml does not declare — GitHub ignores "
            "unknown inputs, so the copied step silently does less than the snippet promises:\n\n"
            + "\n".join(f"  {f}" for f in findings)
        )

    def test_action_input_names_reads_the_real_action(self):
        # Belt: prove the parser returns real inputs, so a broken reader (empty set)
        # can't make the clause above pass vacuously.
        from tests.lint.action_docs import action_input_names

        names = action_input_names(self.ACTION_YML)
        assert {"tasks", "junit-path", "env"} <= names, names

    def test_catches_an_unknown_action_input(self, tmp_path: Path):
        from tests.lint.action_docs import find_unknown_action_inputs

        page = tmp_path / "page.md"
        page.write_text(
            "```yaml\n- uses: UiPath/coder_eval@v0\n  with:\n    tasks: t.yaml\n    junit: out.xml\n```\n",
            encoding="utf-8",
        )
        findings = find_unknown_action_inputs([page], {"tasks", "junit-path"})
        assert len(findings) == 1
        assert "`with: junit:`" in findings[0].message

    def test_finds_the_step_at_any_nesting_depth(self, tmp_path: Path):
        # Pages show the step as a whole workflow, a bare step list, or one step alone.
        from tests.lint.action_docs import find_unknown_action_inputs

        page = tmp_path / "page.md"
        page.write_text(
            "```yaml\njobs:\n  eval:\n    steps:\n      - uses: UiPath/coder_eval@v0\n"
            "        with:\n          bogus: 1\n```\n",
            encoding="utf-8",
        )
        assert len(find_unknown_action_inputs([page], {"tasks"})) == 1

    def test_unparseable_or_unrelated_blocks_are_ignored(self, tmp_path: Path):
        from tests.lint.action_docs import find_unknown_action_inputs

        # A deliberate fragment next to a real reference must not raise or fire.
        page = tmp_path / "page.md"
        page.write_text(
            "```yaml\n- uses: UiPath/coder_eval@v0\n  with:\n    tasks: t.yaml\n```\n\n"
            "```yaml\n  : : not: valid: yaml\n```\n\n"
            "```yaml\n- uses: actions/checkout@v6\n  with:\n    anything: goes\n```\n",
            encoding="utf-8",
        )
        assert find_unknown_action_inputs([page], {"tasks"}) == []

    def test_required_prereqs_match_the_dogfood_job(self):
        # The constant is pinned to the executable reference: the dogfood job proves
        # in CI that these steps are what a fresh runner needs before `uses: ./`.
        from tests.lint.action_docs import DOGFOOD_JOB, REQUIRED_PREREQ_TOKENS, dogfood_prereq_tokens

        tokens = dogfood_prereq_tokens(self.PR_CHECKS)
        for required in REQUIRED_PREREQ_TOKENS:
            assert any(required in token for token in tokens), (
                f"{required!r} is documented as a prerequisite but the {DOGFOOD_JOB} job no longer "
                f"runs it before `uses: ./` (job steps: {sorted(tokens)}). Update "
                "REQUIRED_PREREQ_TOKENS and the doc snippets together."
            )

    def test_catches_a_snippet_missing_prereqs(self, tmp_path: Path):
        from tests.lint.action_docs import find_missing_prereqs

        page = tmp_path / "page.md"
        page.write_text(
            "# Gate\n\n```yaml\n- uses: UiPath/coder_eval@v0\n  with:\n    tasks: t.yaml\n```\n",
            encoding="utf-8",
        )
        findings = find_missing_prereqs([page])
        assert len(findings) == 1
        assert "actions/setup-node" in findings[0].message

    def test_only_the_first_action_block_is_checked(self, tmp_path: Path):
        # Later single-input illustrations must stay quiet, or the rule becomes a nuisance.
        from tests.lint.action_docs import find_missing_prereqs

        page = tmp_path / "page.md"
        page.write_text(
            "```yaml\n"
            "- uses: actions/setup-node@v4\n"
            "- run: npm install -g @anthropic-ai/claude-code\n"
            "- uses: UiPath/coder_eval@v0\n"
            "```\n\n"
            "Score floor:\n\n"
            '```yaml\n- uses: UiPath/coder_eval@v0\n  with:\n    minimum-task-score: "0.8"\n```\n',
            encoding="utf-8",
        )
        assert find_missing_prereqs([page]) == []

    def test_prereq_skip_marker_opts_out(self, tmp_path: Path):
        from tests.lint.action_docs import find_missing_prereqs

        page = tmp_path / "page.md"
        page.write_text(
            "<!-- lint-skip: action-prereq: illustrative fragment -->\n```yaml\n- uses: UiPath/coder_eval@v0\n```\n",
            encoding="utf-8",
        )
        assert find_missing_prereqs([page]) == []

    def test_page_without_the_action_is_ignored(self, tmp_path: Path):
        from tests.lint.action_docs import find_missing_prereqs

        page = tmp_path / "page.md"
        page.write_text("```yaml\n- uses: actions/checkout@v6\n```\n", encoding="utf-8")
        assert find_missing_prereqs([page]) == []

    def test_catches_the_nothing_to_install_regression(self, tmp_path: Path):
        # The exact sentence this rule exists for.
        from tests.lint.action_docs import find_unscoped_absolute_claims

        page = tmp_path / "page.md"
        page.write_text(
            "you reference it by repo path — there is nothing to install:\n\n"
            "```yaml\n- uses: UiPath/coder_eval@v0\n```\n",
            encoding="utf-8",
        )
        findings = find_unscoped_absolute_claims([page])
        assert len(findings) == 1
        assert "nothing to install" in findings[0].message

    def test_scoped_claim_is_allowed(self, tmp_path: Path):
        from tests.lint.action_docs import find_unscoped_absolute_claims

        page = tmp_path / "page.md"
        page.write_text(
            "you reference it by repo path — there is no Marketplace install step:\n\n"
            "```yaml\n- uses: UiPath/coder_eval@v0\n```\n",
            encoding="utf-8",
        )
        assert find_unscoped_absolute_claims([page]) == []

    def test_claim_far_from_a_snippet_is_ignored(self, tmp_path: Path):
        from tests.lint.action_docs import find_unscoped_absolute_claims

        page = tmp_path / "page.md"
        page.write_text(
            "there is nothing to install\n" + "\n" * 40 + "```yaml\n- uses: UiPath/coder_eval@v0\n```\n",
            encoding="utf-8",
        )
        assert find_unscoped_absolute_claims([page]) == []

    def test_catches_a_renamed_listing(self, tmp_path: Path):
        from tests.lint.action_docs import find_slug_mismatches

        page = tmp_path / "page.md"
        page.write_text("[coder_eval](https://github.com/marketplace/actions/coder_eval)\n", encoding="utf-8")
        assert find_slug_mismatches([page], "coder_eval") == []
        assert len(find_slug_mismatches([page], "coder eval x")) == 1

    def test_shields_label_must_decode_to_the_listing_name(self, tmp_path: Path):
        from tests.lint.action_docs import decode_shields_label, find_slug_mismatches

        # A single `_` renders as a space, which is why the badge needs the doubled form.
        assert decode_shields_label("coder__eval") == "coder_eval"
        assert decode_shields_label("coder_eval") == "coder eval"

        page = tmp_path / "page.md"
        page.write_text(
            "[![m](https://img.shields.io/badge/marketplace-coder_eval-2ea44f.svg)](https://x)\n",
            encoding="utf-8",
        )
        findings = find_slug_mismatches([page], "coder_eval")
        assert len(findings) == 1
        assert "displays as 'coder eval'" in findings[0].message


@pytest.mark.lint
class TestCE033PluginReferenceParity:
    """CE033 — the plugin's bundled criteria reference is generated from the models.

    An installed plugin is copied to ~/.claude/plugins/cache/ without its parent
    directories, so its skills cannot read docs/TASK_DEFINITION_GUIDE.md at
    runtime — the criterion vocabulary has to ship inside plugins/coder-eval/,
    where a hand-maintained copy would drift on the next criterion change. The
    SuccessCriterion union is the SSOT; `make plugin-reference` writes the copy
    and this class diffs it. Reasons over Markdown + pydantic metadata, so it
    lives here rather than in the AST runner.
    """

    REPO_ROOT = REPO_ROOT

    def test_generated_reference_matches_disk(self):
        from tests.lint.plugin_reference import check

        findings = check(self.REPO_ROOT)
        assert not findings, (
            "\nThe plugin's bundled criteria reference drifted from the criterion models — run "
            "`make plugin-reference` to regenerate:\n\n"
            + "\n\n".join(f"{path}:\n{diff}" for path, diff in sorted(findings.items()))
        )

    def test_every_criterion_type_appears_in_the_reference(self):
        # Union-driven, so a 15th criterion must appear with zero edits to the generator.
        from tests.lint.plugin_reference import _VARIANTS, _tag, render_criteria

        rendered = render_criteria()
        for cls in _VARIANTS:
            assert f"### `{_tag(cls)}`" in rendered, f"{cls.__name__} is missing from the rendered reference"

    def test_generated_reference_lists_every_accepted_alias(self):
        # The loader accepts legacy `type:` values and rejects removed ones, and until now
        # neither fact reached the bundled reference — so an authoring agent reading a task
        # that uses one had nothing to look it up in, and no way to know the replacement.
        # Driven off the same two maps the validator consumes, so a new alias cannot ship
        # undocumented; the constants are the SSOT and this asserts the render followed.
        from coder_eval.models import NORMALIZED_CRITERION_ALIASES, REMOVED_CRITERION_TYPES
        from tests.lint.plugin_reference import render_criteria

        rendered = render_criteria()
        assert NORMALIZED_CRITERION_ALIASES or REMOVED_CRITERION_TYPES, (
            "both alias maps are empty — this guard is inert; drop it along with the section"
        )
        for alias, overlay in NORMALIZED_CRITERION_ALIASES.items():
            assert f"`{alias}`" in rendered, f"legacy alias {alias} is not documented in the reference"
            assert f"`{overlay['type']}`" in rendered, f"{alias}'s replacement type is not named"
        for name, hint in REMOVED_CRITERION_TYPES.items():
            assert f"`{name}`" in rendered, f"removed type {name} is not documented in the reference"
            assert hint.split(".")[0] in rendered, f"{name}'s migration hint is not rendered"

    def test_common_base_fields_are_not_repeated_per_criterion(self):
        from tests.lint.plugin_reference import render_criteria

        common_section, _, per_criterion = render_criteria().partition("## Criterion types")
        # Match the table-row form the render emits a field NAME in, rather than a bare
        # token, so a criterion whose prose happens to mention "weight" cannot fail this
        # spuriously. A table row is now the ONLY form a field name is emitted in —
        # optional fields are described rows too — so this one assertion covers required
        # and optional alike.
        for field in ("weight", "pass_threshold", "suite_thresholds", "stop_early"):
            assert f"`{field}`" in common_section, f"{field} must be documented once, in Common fields"
            assert f"| `{field}` |" not in per_criterion, (
                f"{field} is inherited and must not be repeated in a per-criterion section"
            )

    def test_every_criterion_field_has_a_description(self):
        # Optional fields now render their description into a table cell, so a field
        # declared without one produces a silently empty cell in the shipped
        # reference. Union-driven: a 15th criterion is covered with zero edits here.
        from coder_eval.models import BaseSuccessCriterion, LiveSuccessCriterion
        from tests.lint.plugin_reference import _DISCRIMINATOR, _VARIANTS

        missing = [
            f"{cls.__name__}.{name}"
            for cls in (*_VARIANTS, BaseSuccessCriterion, LiveSuccessCriterion)
            for name, info in cls.model_fields.items()
            if name != _DISCRIMINATOR and not (info.description or "").strip()
        ]
        assert not missing, (
            f"criterion field(s) with no `description=`: {sorted(set(missing))} — the bundled "
            "reference renders every field's description, so a missing one ships as an empty cell"
        )

    def test_optional_fields_render_with_descriptions(self):
        # Read the expected text off the model rather than hardcoding it, so a
        # reworded description does not fail this test spuriously.
        from coder_eval.models import CommandExecutedCriterion
        from tests.lint.plugin_reference import _cell, render_criteria

        described = _cell(CommandExecutedCriterion.model_fields["require_success"].description or "")
        assert described, "require_success lost its description — the fixture for this test is gone"
        assert f"| `require_success` | {described} |" in render_criteria(), (
            "an optional field must render as a described table row, not a bare name"
        )

    def test_render_is_deterministic(self):
        from tests.lint.plugin_reference import render_criteria

        assert render_criteria() == render_criteria()

    def test_docstringless_criterion_renders(self):
        from typing import Literal

        from coder_eval.models import BaseSuccessCriterion
        from tests.lint.plugin_reference import render_criteria

        class Undocumented(BaseSuccessCriterion):
            type: Literal["undocumented"] = "undocumented"

        Undocumented.__doc__ = None
        rendered = render_criteria([Undocumented])
        assert "### `undocumented`" in rendered
        assert "No fields beyond the common ones." in rendered

    def test_pipe_in_description_is_escaped(self):
        from typing import Literal

        from pydantic import Field

        from coder_eval.models import BaseSuccessCriterion
        from tests.lint.plugin_reference import render_criteria

        class Piped(BaseSuccessCriterion):
            """A criterion whose required field description contains a pipe."""

            type: Literal["piped"] = "piped"
            mode: str = Field(description="one of: a | b | c")

        row = next(line for line in render_criteria([Piped]).splitlines() if line.startswith("| `mode`"))
        # Escaped pipes keep the row at two columns: leading, separator, trailing.
        assert row.count("|") - row.count(r"\|") == 3, row

    def test_write_is_idempotent(self, tmp_path: Path):
        from tests.lint.plugin_reference import check, write

        write(tmp_path)
        first = (tmp_path / "plugins/coder-eval/reference/criteria.md").read_text(encoding="utf-8")
        write(tmp_path)
        assert (tmp_path / "plugins/coder-eval/reference/criteria.md").read_text(encoding="utf-8") == first
        assert check(tmp_path) == {}

    def test_render_carries_no_types_or_defaults(self):
        # The render deliberately emits neither field types nor defaults (see the
        # module docstring); these tokens are what a reintroduced column would leak.
        from tests.lint.plugin_reference import render_criteria

        rendered = render_criteria()
        assert "PydanticUndefined" not in rendered
        assert "typing.Optional" not in rendered

    def test_drift_is_detected(self, tmp_path: Path):
        from tests.lint.plugin_reference import check, write

        write(tmp_path)
        target = tmp_path / "plugins/coder-eval/reference/criteria.md"
        target.write_text(target.read_text(encoding="utf-8").replace("### `file_exists`", "### `tampered`"))
        assert str(target) in check(tmp_path)


@pytest.mark.lint
class TestCE046CliFlagsAreDocumented:
    """CE046 — every long `--flag` a CLI command declares must appear in `docs/USER_GUIDE.md`.

    The CE030 doc-parity family, one surface over: CE030 gates a model's fields against its guide
    section, and a CLI flag is the same contract with a different declaration site. An undocumented
    flag is a capability the user pays for and cannot find.

    The scan set is DERIVED from `coder_eval.cli.app`, and the only exclusion mechanism is Typer's
    `hidden=True`. There is deliberately no exemption map: "this command is not part of the
    user-facing surface" is already declared once, in `cli/__init__.py`, and a second hand-kept
    list would be that same fact spelled twice.

    **Boundary**, stated so a green run is not mistaken for a proof:

    - it pins DOCUMENTATION, never behaviour — a flag can be documented and do nothing;
    - short flags (`-j`, `-e`) are out of scope by construction, matching CE043;
    - a flag Typer DERIVES from the parameter name carries no `param_decls` and is invisible to
      it — the same blind spot CE043 declares, stated on `tests/lint/cli_flags.py`;
    - "documented" means the name appears anywhere in the guide, including inside a fenced
      example — the failure message names the flag table because that is where a row BELONGS,
      not because the check can tell one from the other.
    """

    GUIDE: ClassVar[Path] = REPO_ROOT / "docs" / "USER_GUIDE.md"

    def test_every_cli_flag_appears_in_the_user_guide(self) -> None:
        from tests.lint.cli_flags import documented_commands, undocumented_flags

        missing = undocumented_flags(documented_commands(), self.GUIDE.read_text(encoding="utf-8"))
        assert not missing, "\n  ".join(["undocumented CLI flags:", *missing])

    def test_the_scan_sees_the_real_commands(self) -> None:
        # Anti-vacuity: an empty command map or an empty flag set would make the check above pass
        # while reading nothing.
        from tests.lint.cli_flags import documented_commands

        commands = documented_commands()
        assert {"run", "plan"} <= set(commands), sorted(commands)
        assert "--split" in long_flags(commands["run"])

    def test_the_command_set_is_derived_not_hardcoded(self) -> None:
        # Compared against `app` directly. A hardcoded expected list here would reintroduce the
        # checklist the derivation exists to replace — `evaluate` and `aggregate` are easy to miss.
        from coder_eval.cli import app
        from tests.lint.cli_flags import documented_commands

        expected = {c.name or c.callback.__name__ for c in app.registered_commands if c.hidden is not True}
        callback = app.registered_callback
        if callback is not None and callback.callback is not None and callback.hidden is not True:
            expected.add(callback.callback.__name__)
        assert set(documented_commands()) == expected

    def test_a_hidden_command_is_not_scanned(self) -> None:
        # `_run-task-internal`'s `--input` / `--task-dir` are absent from the guide, correctly:
        # the command is `hidden=True`, so it never reaches the check and needs no exemption.
        from tests.lint.cli_flags import documented_commands

        assert "_run-task-internal" not in documented_commands()

    def test_it_catches_an_undocumented_flag(self) -> None:
        import typer

        from tests.lint.cli_flags import undocumented_flags

        def _stub(nowhere: str | None = typer.Option(None, "--nowhere")) -> None: ...

        missing = undocumented_flags({"stub": _stub}, "a guide that mentions no such flag")
        assert len(missing) == 1 and "--nowhere" in missing[0], missing

    def test_a_flag_that_prefixes_another_is_not_auto_satisfied(self) -> None:
        """`--sample` is a prefix of `--sample-per-stratum`, and both are real flags.

        Under a bare substring test this check CANNOT FAIL for `--sample`: deleting every mention
        of it from the guide leaves the longer sibling satisfying the shorter one. Measured on the
        real guide, so this is the sensor's own blind spot pinned rather than a hypothetical.
        """
        from tests.lint.cli_flags import documented_commands, undocumented_flags

        guide = self.GUIDE.read_text(encoding="utf-8")
        without_sample = re.sub(r"--sample(?![\w-])", "--smpl", guide)
        assert "--sample-per-stratum" in without_sample, "the longer sibling must survive the edit"

        missing = undocumented_flags(documented_commands(), without_sample)
        assert missing and all("--sample`" in m or "--sample " in m for m in missing), missing

    def test_a_boolean_pair_documented_with_spaces_still_counts(self) -> None:
        """`--preserve/--no-preserve` arrives as ONE unspaced string; the guide writes it spaced.

        Matching the raw decl would fail on a CORRECTLY documented flag and demand an edit that
        makes the guide worse — so the decl is split on `/` and each bare name matched alone.
        """
        import typer

        from tests.lint.cli_flags import undocumented_flags

        def _stub(preserve: bool = typer.Option(True, "--preserve/--no-preserve")) -> None: ...

        spaced = "the sandbox is kept unless `--preserve / --no-preserve` says otherwise"
        assert undocumented_flags({"stub": _stub}, spaced) == []
        assert undocumented_flags({"stub": _stub}, "only `--preserve` here") != []
