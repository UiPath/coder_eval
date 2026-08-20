"""Lint tests: doc surfaces."""

from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from tests.lint_tests.shared import (
    _TOP_LEVEL_CELL,
    PLUGIN_ROOT,
    REPO_ROOT,
    RUN_RECORD_CONSUMERS,
    _legacy_top_level_keys,
    _normalized,
    _record_fields_referenced,
)


@pytest.mark.lint
class TestCE027DocEnvVarParity:
    """CE027 — documented framework env-var assignments must be backed by a
    real consumer (a Settings field/alias or an os.getenv read in src/).

    `Settings` sets no `env_prefix` and uses `extra="ignore"`, so a documented
    `NAME=value` whose name matches no field is silently dropped at runtime — the
    `CODER_EVAL_API_BACKEND` (real field: `API_BACKEND`) doc bug. Scans real
    Markdown/YAML surfaces, so it lives here rather than in the AST-only runner.
    """

    REPO_ROOT = REPO_ROOT

    def test_repo_docs_have_no_unbacked_env_vars(self):
        from tests.lint.doc_env_parity import default_doc_paths, find_unbacked_env_vars

        findings = find_unbacked_env_vars(default_doc_paths(self.REPO_ROOT), self.REPO_ROOT / "src")
        assert not findings, (
            "\nDocumented framework env-var assignment(s) that no Settings field/alias or "
            "src/ reference backs — they are silently dropped at runtime (Settings uses "
            'extra="ignore"). Fix the spelling to a real env name, or add the consumer:\n\n'
            + "\n".join(f"  {path}: {', '.join(names)}" for path, names in sorted(findings.items()))
        )

    def test_catches_the_coder_eval_api_backend_shadow(self):
        # The exact regression: a CODER_EVAL_-prefixed spelling of a real field.
        from tests.lint.doc_env_parity import scan_doc_env_assignments, settings_env_names, src_env_literals

        valid = settings_env_names() | src_env_literals(self.REPO_ROOT / "src")
        found = scan_doc_env_assignments("      CODER_EVAL_API_BACKEND=bedrock\n")
        assert "CODER_EVAL_API_BACKEND" in found
        assert "CODER_EVAL_API_BACKEND" not in valid  # would be flagged

    def test_real_backend_field_is_backed(self):
        from tests.lint.doc_env_parity import settings_env_names

        assert "API_BACKEND" in settings_env_names()

    @pytest.mark.parametrize(
        "line",
        [
            "AWS_BEARER_TOKEN_BEDROCK=${{ secrets.BEDROCK_TOKEN }}",  # secret RHS, not an assignment of BEDROCK_TOKEN
            "[Codex Agent Guide](docs/CODEX_AGENT_GUIDE.md)",  # markdown link, not an assignment
            'pattern: "^API_KEY = \\"\\\\w+\\""',  # regex example with a space before '='
            "the CODEX_MODEL setting selects the model",  # bare prose mention, not an assignment
            "X-API_KEY=v",  # hyphenated token — prefix embedded, not a standalone name
            "dir/API_THING=v",  # path segment — prefix embedded
            "http://API_HOST=1",  # URL segment — prefix embedded
        ],
    )
    def test_non_assignment_shapes_are_not_flagged(self, line: str):
        from tests.lint.doc_env_parity import scan_doc_env_assignments, settings_env_names, src_env_literals

        valid = settings_env_names() | src_env_literals(self.REPO_ROOT / "src")
        unbacked = [t for t in scan_doc_env_assignments(line) if t not in valid]
        assert unbacked == [], f"false positive on non-assignment shape: {unbacked}"

    def test_name_side_of_env_value_literal_counts_as_backed(self):
        # `--env CODER_EVAL_IN_CONTAINER=1` in src makes the doc assignment backed.
        from tests.lint.doc_env_parity import src_env_literals

        names = src_env_literals(self.REPO_ROOT / "src")
        assert "CODER_EVAL_IN_CONTAINER" in names

    def test_src_scan_requires_a_real_consumer_not_any_literal(self, tmp_path: Path):
        # A bare uppercase constant that no code reads must NOT count as "backed",
        # or it could silently mask a documented-but-unconsumed assignment.
        from tests.lint.doc_env_parity import src_env_literals

        (tmp_path / "m.py").write_text(
            'CONST = "CODER_EVAL_BOGUS"\nx = os.getenv("CODER_EVAL_REAL")\n', encoding="utf-8"
        )
        names = src_env_literals(tmp_path)
        assert "CODER_EVAL_REAL" in names  # a genuine os.getenv read is backed
        assert "CODER_EVAL_BOGUS" not in names  # a bare constant is not


@pytest.mark.lint
class TestCE028DocIndexParity:
    """CE028 — the flat index surfaces (README, docs/index.md, docs/llms.txt) are
    generated from the mkdocs nav + extra.docs_index; disk must match.

    Also enforces the invariants the render depends on: nav<->blurb bijection,
    every published docs/ page is in the nav, and the hand-written tutorials
    table stays in parity with the nav. Reasons over Markdown/YAML, so it lives
    here rather than in the AST runner.
    """

    REPO_ROOT = REPO_ROOT

    @property
    def _nav(self):
        from tests.lint.doc_indexes import load_nav

        return load_nav(self.REPO_ROOT / "mkdocs.yml")

    @property
    def _blurbs(self):
        from tests.lint.doc_indexes import load_blurbs

        return load_blurbs(self.REPO_ROOT / "mkdocs.yml")

    def test_repo_indexes_match_generated_output(self):
        from tests.lint.doc_indexes import check

        findings = check(self.REPO_ROOT)
        assert not findings, (
            "\nGenerated index surface(s) drifted from the mkdocs nav — run `make docs-indexes` "
            "to regenerate:\n\n" + "\n\n".join(f"{path}:\n{diff}" for path, diff in sorted(findings.items()))
        )

    def test_every_nav_page_has_a_blurb(self):
        from tests.lint.doc_indexes import missing_blurbs

        missing = missing_blurbs(self._nav, self._blurbs)
        assert not missing, f"nav pages without an extra.docs_index blurb (tutorial leaves exempt): {missing}"

    def test_no_orphan_blurbs(self):
        from tests.lint.doc_indexes import orphan_blurbs

        orphans = orphan_blurbs(self._nav, self._blurbs)
        assert not orphans, f"extra.docs_index blurbs with no matching nav page: {orphans}"

    def test_every_published_doc_is_in_the_nav(self):
        from tests.lint.doc_indexes import docs_missing_from_nav

        missing = docs_missing_from_nav(self.REPO_ROOT, self._nav)
        assert not missing, f"published docs/ pages absent from the nav (add to nav or the exclusion set): {missing}"

    def test_tutorials_table_matches_nav(self):
        from tests.lint.doc_indexes import tutorials_table_drift

        drift = tutorials_table_drift(self.REPO_ROOT, self._nav)
        assert drift is None, drift

    def test_tutorials_avoid_mkdocs_only_admonitions(self):
        # Tutorials are read on GitHub as often as on the docs site — from the repo, from a
        # PR diff, from a search result. `!!! note` is mkdocs-material syntax that GitHub
        # does not understand: it renders the marker as literal text and turns the indented
        # body into an accidental code block. Tutorials 01-07 use plain `>` blockquotes,
        # which render correctly in both, so this pins that convention for new ones.
        #
        # Scoped to tutorials on purpose: reference pages under docs/ are site-first and one
        # (DATASETS.md) predates this rule.
        offenders = []
        for path in sorted((self.REPO_ROOT / "docs" / "tutorials").glob("*.md")):
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.startswith("!!! ") or line.startswith("??? "):
                    offenders.append(f"{path.relative_to(self.REPO_ROOT)}:{line_no}: {line.strip()}")
        assert not offenders, (
            "mkdocs-material admonition(s) in a tutorial — these render as literal text plus "
            "a stray code block on GitHub. Use a `>` blockquote instead:\n  " + "\n  ".join(offenders)
        )

    def test_real_mkdocs_yaml_parses_despite_env_tag(self):
        # The real mkdocs.yml carries `!ENV [CI, false]`; load_nav must tolerate it.
        nav = self._nav
        assert any(p.doc == "index.md" for p in nav)
        assert any(p.doc == "DIALOG_MODE.md" for p in nav)

    def test_nav_loader_does_not_mutate_global_safeloader(self):

        from tests.lint.doc_indexes import load_nav

        load_nav(self.REPO_ROOT / "mkdocs.yml")
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load("x: !ENV [A, b]")

    def test_python_object_tags_are_still_rejected(self):

        from tests.lint.doc_indexes import load_nav

        # Prove the private loader stays safe_load-equivalent for object construction.
        load_nav(self.REPO_ROOT / "mkdocs.yml")

        class _Probe(yaml.SafeLoader):
            pass

        _Probe.add_multi_constructor("!", lambda loader, suffix, node: None)
        with pytest.raises(yaml.constructor.ConstructorError):
            yaml.load("x: !!python/object/apply:os.system ['echo hi']", Loader=_Probe)

    @pytest.mark.parametrize(
        ("doc", "route"),
        [
            ("index.md", "/docs"),
            ("USER_GUIDE.md", "/docs/user-guide"),
            ("agents/CLAUDE_CODE.md", "/docs/agents/claude-code"),
            ("tutorials/README.md", "/docs/tutorials"),
        ],
    )
    def test_route_for_known_shapes(self, doc: str, route: str):
        from tests.lint.doc_indexes import route_for

        assert route_for(doc) == route

    def test_write_is_idempotent(self, tmp_path: Path):
        import shutil

        from tests.lint.doc_indexes import check, write

        # Copy the pieces write() touches into a scratch tree so the live repo is untouched.
        (tmp_path / "docs").mkdir()
        shutil.copy(self.REPO_ROOT / "mkdocs.yml", tmp_path / "mkdocs.yml")
        for rel in ("README.md", "docs/index.md", "docs/llms.txt", "docs/tutorials/README.md"):
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.REPO_ROOT / rel, dest)

        write(tmp_path)
        first = (tmp_path / "README.md").read_text(encoding="utf-8")
        write(tmp_path)
        assert (tmp_path / "README.md").read_text(encoding="utf-8") == first
        assert check(tmp_path) == {}

    def test_missing_marker_raises_clear_error(self):
        from tests.lint.doc_indexes import _replace_between

        with pytest.raises(ValueError, match="marker pair"):
            _replace_between("no markers here", "<!-- start -->", "<!-- end -->", "body")

    def test_drift_is_detected(self, tmp_path: Path):
        # A hand-edit between the markers must be reported by check().
        import shutil

        from tests.lint.doc_indexes import check

        (tmp_path / "docs").mkdir()
        shutil.copy(self.REPO_ROOT / "mkdocs.yml", tmp_path / "mkdocs.yml")
        for rel in ("README.md", "docs/index.md", "docs/llms.txt", "docs/tutorials/README.md"):
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.REPO_ROOT / rel, dest)

        readme = tmp_path / "README.md"
        text = readme.read_text(encoding="utf-8")
        readme.write_text(text.replace("| [User Guide]", "| [Tampered Guide]"), encoding="utf-8")
        findings = check(tmp_path)
        assert str(readme) in findings


@pytest.mark.lint
class TestCE029DocYamlExamples:
    """CE029 — self-contained YAML examples in the docs must validate.

    A published snippet that raises when copy-pasted reads as a broken feature.
    The motivating bug: the `prompt_mutations` recipe used `text:` where the
    field is `content:`, and every mutation model sets `extra="forbid"`. Scans
    real Markdown, so it lives here rather than in the AST-only runner.
    """

    REPO_ROOT = REPO_ROOT

    @staticmethod
    def _check(text: str) -> str | None:
        from tests.lint.doc_examples import extract_yaml_blocks, validate_block

        blocks = extract_yaml_blocks(Path("doc.md"), text)
        assert len(blocks) == 1, f"expected exactly one yaml block, got {len(blocks)}"
        return validate_block(blocks[0])

    _VALID_TASK = """```yaml
task_id: "demo"
description: "A demo task"
initial_prompt: "Write hello.py"
success_criteria:
  - type: "file_exists"
    path: "hello.py"
    description: "hello.py exists"
```
"""

    def test_repo_doc_examples_validate(self):
        from tests.lint.doc_examples import default_doc_paths, find_invalid_doc_examples

        findings = find_invalid_doc_examples(default_doc_paths(self.REPO_ROOT))
        assert not findings, (
            "\nSelf-contained YAML example(s) in the docs that do not validate against their "
            "model — a reader who copy-pastes this hits a ValidationError. Fix the example, or "
            f"mark the block with `{'<!-- lint-skip: doc-yaml -->'}` if it is intentionally partial:\n\n"
            + "\n".join(f"  {path}:\n    " + "\n    ".join(errs) for path, errs in sorted(findings.items()))
        )

    def test_valid_task_block_passes(self):
        assert self._check(self._VALID_TASK) is None

    def test_catches_the_prompt_mutations_regression(self):
        # The exact historical bug this rule exists for: `text:` where the
        # PromptSuffix field is `content:` (every mutation model forbids extras).
        finding = self._check(
            """```yaml
experiment_id: prompt-phrasing
description: "Terse vs. detailed"
variants:
  - variant_id: terse
  - variant_id: detailed
    prompt_mutations:
      - type: suffix
        text: "Think step by step."
```
"""
        )
        assert finding is not None
        assert "content" in finding

    def test_schematic_placeholder_blocks_are_skipped(self):
        # The task guide's overview block deliberately writes `agent: { ... }`.
        assert (
            self._check(
                """```yaml
task_id: "my_task"
description: "What this task tests"
initial_prompt: "Instructions..."
agent: { ... }
success_criteria: [ ... ]
```
"""
            )
            is None
        )

    def test_fragment_blocks_are_not_validated(self):
        # A bare criteria list is illustrative, not a document. Validating it
        # would flag most blocks in the docs and get the rule deleted.
        assert (
            self._check(
                """```yaml
success_criteria:
  - type: "file_exists"
    path: "app.py"
```
"""
            )
            is None
        )

    def test_lint_skip_marker_is_honored(self):
        from tests.lint.doc_examples import SKIP_MARKER

        broken = self._VALID_TASK.replace('  - type: "file_exists"', "  - type: no_such_criterion")
        assert self._check(broken) is not None, "control: the block must fail without the marker"
        assert self._check(f"{SKIP_MARKER}\n\n{broken}") is None

    def test_non_yaml_fences_are_ignored(self):
        from tests.lint.doc_examples import extract_yaml_blocks

        text = '```json\n{"task_id": 1}\n```\n\n```bash\ncoder-eval run\n```\n'
        assert extract_yaml_blocks(Path("doc.md"), text) == []

    def test_info_string_attributes_are_still_captured(self):
        # mkdocs-material allows ```yaml title="x"; such a block must not be
        # silently skipped, or a broken example there escapes the rule.
        from tests.lint.doc_examples import extract_yaml_blocks

        text = '```yaml title="task.yaml"\ntask_id: 1\n```\n'
        blocks = extract_yaml_blocks(Path("doc.md"), text)
        assert len(blocks) == 1

    def test_valid_experiment_block_passes(self):
        assert (
            self._check(
                """```yaml
experiment_id: demo-experiment
description: "A demo experiment"
variants:
  - variant_id: baseline
  - variant_id: treatment
    agent:
      model: claude-sonnet-4-6
```
"""
            )
            is None
        )


@pytest.mark.lint
class TestCE030DocSchemaParity:
    """CE030 — a registered model's fields must be documented or exempted with a reason.

    Every P0/P1 defect in the docs overhaul was an undocumented user-facing
    field. This gate makes that recurrence impossible for a small, explicit
    registry of models: adding a field forces a doc update or a reasoned
    EXEMPT entry. Scans real Markdown, so it lives here, not in the AST runner.
    """

    REPO_ROOT = REPO_ROOT

    def test_registered_models_are_fully_documented(self):
        from tests.lint.doc_schema_parity import find_undocumented_fields

        findings = find_undocumented_fields(self.REPO_ROOT)
        assert not findings, (
            "\nRegistered model field(s) documented nowhere in their doc page (and not EXEMPT) — "
            "document them (mention the field name as `inline code`) or add an EXEMPT entry with a "
            "reason in tests/lint/doc_schema_parity.py:\n\n"
            + "\n".join(f"  {model}: {', '.join(fields)}" for model, fields in sorted(findings.items()))
        )

    def test_exempt_fields_carry_a_reason(self):
        from tests.lint.doc_schema_parity import EXEMPT

        for model_name, fields in EXEMPT.items():
            for field_name, reason in fields.items():
                assert reason and reason.strip(), f"EXEMPT[{model_name}][{field_name}] has an empty reason"

    def test_exemptions_reference_real_fields(self):
        # An exemption left behind after a field rename would silently mask a
        # real undocumented field. Pin every exempt name to a real model field.
        from tests.lint.doc_schema_parity import DOCUMENTED_MODELS, EXEMPT

        by_name = {m.__name__: m for m, _ in DOCUMENTED_MODELS}
        for model_name, fields in EXEMPT.items():
            assert model_name in by_name, f"EXEMPT names unknown model {model_name!r}"
            real = set(by_name[model_name].model_fields)
            for field_name in fields:
                assert field_name in real, f"EXEMPT[{model_name}] names non-field {field_name!r}"

    def test_detects_an_undocumented_field(self):
        from pydantic import BaseModel, Field

        from tests.lint.doc_schema_parity import undocumented_fields

        class Synthetic(BaseModel):
            documented: str = Field(default="")
            undocumented: str = Field(default="")

        doc = "The `documented` field is described here."
        assert undocumented_fields(Synthetic, doc, {}) == ["undocumented"]

    def test_inline_code_match_only(self):
        # A field mentioned in prose without backticks does NOT count as documented.
        from pydantic import BaseModel, Field

        from tests.lint.doc_schema_parity import undocumented_fields

        class Synthetic(BaseModel):
            widget: str = Field(default="")

        assert undocumented_fields(Synthetic, "The widget setting is great.", {}) == ["widget"]
        assert undocumented_fields(Synthetic, "The `widget` setting is great.", {}) == []

    def test_exempt_field_is_not_reported(self):
        from pydantic import BaseModel, Field

        from tests.lint.doc_schema_parity import undocumented_fields

        class Synthetic(BaseModel):
            internal: str = Field(default="")

        assert undocumented_fields(Synthetic, "", {"internal": "set by the framework"}) == []

    def test_missing_doc_file_fails_loudly(self):
        # A moved/renamed doc must fail the gate, not vacuously pass. The
        # integration wrapper maps a missing file to "" → every field reported.
        from tests.lint.doc_schema_parity import find_undocumented_fields

        findings = find_undocumented_fields(self.REPO_ROOT / "no_such_dir")
        assert findings, "a missing doc file must surface every field, not return empty"
        assert any("RunLimits" in key for key in findings)


@pytest.mark.lint
class TestCE031DeadConfigFields:
    """CE031 — a behavior-driving config field must be read somewhere in src/.

    Guards the dead-config class that SimulationConfig.parallel_trials was: a
    field users set in a task YAML that no code reads by name, so it silently
    does nothing. Scans the whole src/ tree for attribute reads, so it lives
    here rather than in the per-file AST runner.
    """

    REPO_ROOT = REPO_ROOT
    SRC = REPO_ROOT / "src"

    def test_no_dead_config_fields(self):
        from tests.lint.dead_config_fields import find_dead_config_fields

        findings = find_dead_config_fields(self.SRC)
        assert not findings, (
            "\nConfig field(s) that no code in src/ reads by name — dead config a user could set "
            "with no effect. Wire the field to real behavior, remove it, or (if it is consumed only "
            "via serialization) add an EXEMPT entry with a reason in tests/lint/dead_config_fields.py:\n\n"
            + "\n".join(f"  {model}: {', '.join(fields)}" for model, fields in sorted(findings.items()))
        )

    def test_detects_a_dead_field(self):
        from pydantic import BaseModel, Field

        from tests.lint.dead_config_fields import dead_config_fields

        class Synthetic(BaseModel):
            wired: str = Field(default="")
            orphan: str = Field(default="")

        # `wired` is read as an attribute somewhere; `orphan` is not.
        assert dead_config_fields(Synthetic, consumed={"wired"}, exempt={}) == ["orphan"]

    def test_consumed_field_not_flagged(self):
        from pydantic import BaseModel, Field

        from tests.lint.dead_config_fields import dead_config_fields

        class Synthetic(BaseModel):
            live: str = Field(default="")

        assert dead_config_fields(Synthetic, consumed={"live"}, exempt={}) == []

    def test_exempt_field_not_flagged(self):
        from pydantic import BaseModel, Field

        from tests.lint.dead_config_fields import dead_config_fields

        class Synthetic(BaseModel):
            serialized_only: str = Field(default="")

        assert dead_config_fields(Synthetic, consumed=set(), exempt={"serialized_only": "read via model_dump"}) == []

    def test_would_catch_parallel_trials_shape(self):
        # A field named like the removed parallel_trials, absent from the consumed
        # set, must be reported — the exact regression this rule exists for.
        from pydantic import BaseModel, Field

        from tests.lint.dead_config_fields import dead_config_fields

        class Synthetic(BaseModel):
            parallel_trials: bool = Field(default=True)

        assert dead_config_fields(Synthetic, consumed={"n_trials", "max_turns"}, exempt={}) == ["parallel_trials"]

    def test_exemptions_reference_real_fields(self):
        # A stale EXEMPT entry (field renamed/removed) would silently mask a dead field.
        from tests.lint.dead_config_fields import CONSUMED_MODELS, EXEMPT

        by_name = {m.__name__: m for m in CONSUMED_MODELS}
        for model_name, fields in EXEMPT.items():
            assert model_name in by_name, f"EXEMPT names unregistered model {model_name!r}"
            real = set(by_name[model_name].model_fields)
            for field_name, reason in fields.items():
                assert field_name in real, f"EXEMPT[{model_name}] names non-field {field_name!r}"
                assert reason and reason.strip(), f"EXEMPT[{model_name}][{field_name}] has an empty reason"

    def test_registered_fields_are_actually_consumed_on_the_real_tree(self):
        # Belt: prove the attribute scan really finds known-live fields, so a
        # broken scanner (returning everything or nothing) can't pass silently.
        from tests.lint.dead_config_fields import consumed_attr_names

        consumed = consumed_attr_names(self.SRC)
        for name in ("n_trials", "max_usd", "stratify_field"):
            assert name in consumed, f"expected {name!r} to be read as an attribute in src/"


@pytest.mark.lint
class TestGeneratedSurfaceEngine:
    """The write/diff engine both generated-surface checkers (CE028, CE033) route through.

    Extracted from two near-verbatim copies. The copies had diverged in exactly one
    respect — only the plugin-reference one created a missing target (and its parents),
    because it was the only surface whose file might not exist yet. The shared engine
    keeps that more general behaviour, so these tests pin it for both callers.
    """

    def test_write_all_creates_missing_targets_including_parents(self, tmp_path: Path):
        from tests.lint.generated import write_all

        target = tmp_path / "nested" / "deeper" / "out.md"
        assert write_all({target: "body\n"}) == [target]
        assert target.read_text(encoding="utf-8") == "body\n"

    def test_write_all_leaves_an_already_current_file_untouched(self, tmp_path: Path):
        from tests.lint.generated import write_all

        target = tmp_path / "out.md"
        write_all({target: "body\n"})
        before = target.stat().st_mtime_ns
        assert write_all({target: "body\n"}) == [target]
        assert target.stat().st_mtime_ns == before, "an unchanged file must not be rewritten"

    def test_diff_all_is_empty_when_disk_matches(self, tmp_path: Path):
        from tests.lint.generated import diff_all

        target = tmp_path / "out.md"
        target.write_text("body\n", encoding="utf-8")
        assert diff_all({target: "body\n"}) == {}

    def test_diff_all_reports_a_missing_file_as_full_drift(self, tmp_path: Path):
        from tests.lint.generated import diff_all

        # A generated file that was never written is drift, not a crash — the diff has to
        # render rather than raise, or `make lint` fails with a traceback instead of a fix.
        target = tmp_path / "out.md"
        findings = diff_all({target: "body\n"})
        assert list(findings) == [str(target)]
        assert "+body" in findings[str(target)]


@pytest.mark.lint
class TestPersistedCriterionResultFieldsAreDocumented:
    """Every `CriterionResult` base field must be named in `docs/REPORT_SCHEMA.md` § CriterionResult.

    CE030's doc-parity family one model over, and NOT a numbered rule: it is one derived assertion
    over one section rather than the configurable model→guide machinery CE030 owns, and adding a
    number would imply a generality it does not have.

    It exists because the gap was real and silent. `CriterionResult` is a PERSISTED consumer contract —
    `task.json` is read by the evalboard and by anything else pointed at a run tree — and CE030's set
    is `TaskDefinition` / `RunLimits` / `Dataset` / `SimulationConfig`, so adding `weight` to the model
    and forgetting the schema doc broke nothing and failed nothing. A reviewer caught it; this is what
    catches the next one.

    Scoped to the BASE model on purpose. The subclasses' own fields are documented in their own
    bullets in that section, and sweeping them in would make the assertion pass or fail on which
    bullet a name happens to appear in rather than on whether it is documented.
    """

    SCHEMA: ClassVar[Path] = REPO_ROOT / "docs" / "REPORT_SCHEMA.md"
    HEADING: ClassVar[str] = "### CriterionResult"

    def _section(self) -> str:
        text = self.SCHEMA.read_text(encoding="utf-8")
        start = text.index(self.HEADING)
        # To the next same-or-higher heading, so a later section cannot satisfy this one.
        rest = text[start + len(self.HEADING) :]
        end = min((rest.index(m) for m in ("\n### ", "\n## ") if m in rest), default=len(rest))
        return rest[:end]

    def test_every_base_field_is_named(self):
        from coder_eval.models import CriterionResult

        section = self._section()
        assert section.strip(), f"GAP: {self.HEADING} in docs/REPORT_SCHEMA.md is empty"
        fields = set(CriterionResult.model_fields)
        assert fields, "GAP: CriterionResult declares no fields"
        undocumented = sorted(name for name in fields if f"`{name}`" not in section)
        assert not undocumented, (
            f"docs/REPORT_SCHEMA.md's {self.HEADING} section does not name {undocumented}. "
            "task.json is a persisted consumer contract, and CE030's doc-parity family covers "
            "TaskDefinition / RunLimits / Dataset / SimulationConfig only — so a new field here is "
            "documented nowhere and nothing fails."
        )


@pytest.mark.lint
class TestRunRecordFieldVocabulary:
    """Every task.json field the run-analysis surfaces name must exist on the models.

    `jq` returns `null` for a key that does not exist instead of failing, so a wrong
    field name does not surface as an error — it produces a table of nulls that reads
    like a run with nothing in it. Both surfaces shipped six such names at once
    (`turns`, `total_tokens`, `assistant_turn_count`, `max_turns`, `criteria_count`,
    `all_criteria_perfect`), and the failure is worst exactly where the instruction
    applies: the >20-task path, where the agent is explicitly told NOT to fall back to
    reading whole files.

    Scoped deliberately: only the fenced blocks that mention `success_criteria_results`
    (the summary-extraction programs), and only the HEAD of each dotted path. Deeper
    segments are not checked because `task_config` is a free-form dict, so
    `.task_config.resolved.run_limits.max_turns` is unverifiable from the schema. The
    allowed set unions the run-level and criterion-level models rather than tracking
    which scope each expression sits in — a weakening that still catches every name
    above, since none of them exists on either model.
    """

    @staticmethod
    def _known_fields() -> set[str]:
        from coder_eval.models import ClassificationCriterionResult, CriterionResult, EvaluationResult

        return {
            name
            for model in (EvaluationResult, CriterionResult, ClassificationCriterionResult)
            for name in model.model_json_schema(mode="serialization").get("properties", {})
        }

    @staticmethod
    def _summary_blocks(text: str) -> list[str]:
        blocks, current, inside = [], [], False
        for line in text.splitlines():
            if line.strip().startswith("```"):
                if inside:
                    blocks.append("\n".join(current))
                current, inside = [], not inside
                continue
            if inside:
                current.append(line)
        return [b for b in blocks if "success_criteria_results" in b]

    @pytest.mark.parametrize("relpath", RUN_RECORD_CONSUMERS)
    def test_documented_task_json_fields_exist_on_the_models(self, relpath: str):
        doc = REPO_ROOT / relpath
        blocks = self._summary_blocks(doc.read_text(encoding="utf-8"))
        assert blocks, (
            f"{relpath}: no fenced block mentioning `success_criteria_results` — either the "
            "summary-extraction program was removed or renamed, and this guard is now inert"
        )
        known = self._known_fields()
        for block in blocks:
            unknown = sorted(_record_fields_referenced(block) - known)
            assert not unknown, (
                f"{relpath}: {unknown} are read off a run record but exist on neither "
                "EvaluationResult nor CriterionResult. jq yields null for a missing key, so this "
                "ships as a summary full of nulls rather than an error. Check the real field name "
                "(turn records are `iterations`; tokens and cost live under `total_token_usage`; "
                "the turn cap under `task_config.resolved.run_limits`; a criterion's type is "
                "`criterion_type` and it passes when `score >= pass_threshold`)."
            )

    def test_legacy_keys_named_by_analyze_are_real_model_aliases(self):
        # The skill now tells the agent that older runs spell two keys differently, which
        # is the opposite failure from the one above: not a name that never existed, but a
        # name that existed and was renamed. A wrong legacy name is just as silent — jq
        # yields null for it too — so every legacy TOP-LEVEL key the skill names must be a
        # name the loader genuinely still accepts. Derived from `AliasChoices` on the model,
        # so a third generation is one model edit and this guard follows it.
        from pydantic import AliasChoices

        from coder_eval.models import EvaluationResult

        accepted = {
            choice
            for info in EvaluationResult.model_fields.values()
            if isinstance(info.validation_alias, AliasChoices)
            for choice in info.validation_alias.choices
            if isinstance(choice, str)
        }
        skill = PLUGIN_ROOT / "skills" / "analyze" / "SKILL.md"
        named = _legacy_top_level_keys(skill.read_text(encoding="utf-8"))
        assert named, (
            f"{skill}: no `| … | … | {_TOP_LEVEL_CELL} |` row — either the two-generation "
            "table was removed or its third cell was reworded, and this guard is now inert"
        )
        unknown = sorted(named - accepted)
        assert not unknown, (
            f"{skill} presents {unknown} as legacy top-level key(s), but the loader accepts "
            f"only {sorted(accepted)} (read off EvaluationResult's validation_alias). An "
            "invented legacy name reads back as null exactly like a current one would."
        )

    def test_analyze_does_not_deny_the_legacy_key_absolutely(self):
        # The skill used to say flatly "There is no top-level `turns`", which is true of
        # current runs and false of anything written before the rename — so an agent
        # reading a real older run was told its correct extraction was wrong. The four
        # OTHER names in that sentence were never top-level in any generation and were
        # denied on purpose; rewriting the sentence must not take them with it.
        text = _normalized(PLUGIN_ROOT / "skills" / "analyze" / "SKILL.md")
        assert "There is no top-level `turns`" not in text, (
            "analyze denies the legacy `turns` key absolutely again — it is what runs "
            "written before the rename actually carry. Make it conditional on the file."
        )
        assert "`iterations`" in text, "analyze no longer names `iterations` as the current key"
        denial = text.partition("There is no top-level")[2].partition(".")[0]
        assert denial, "analyze lost the never-top-level denial sentence entirely"
        for name in ("`total_tokens`", "`total_cost_usd`", "`max_turns`", "`criteria_count`"):
            assert name in denial, (
                f"analyze stopped denying a top-level {name}, which is absent in EVERY "
                "generation — collateral damage from making the `turns` clause conditional"
            )

    def test_catches_the_vocabulary_that_actually_shipped(self):
        # Mutation guard: the exact block both files carried before this was fixed.
        original = """
        {task_id, final_status, weighted_score, duration_seconds, total_cost_usd,
         total_tokens, assistant_turn_count, max_turns, max_turns_exhausted,
         iteration_count, model_used, criteria_count, all_criteria_perfect,
         failed_criteria: [{type, description, score, error_excerpt}]}
        """
        caught = _record_fields_referenced(original) - self._known_fields()
        assert {"total_tokens", "assistant_turn_count", "criteria_count", "all_criteria_perfect"} <= caught
