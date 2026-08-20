"""Lint tests: computed claims."""

import ast
from pathlib import Path
from typing import ClassVar

import pytest

from tests.lint_tests.shared import REPO_ROOT


@pytest.mark.lint
class TestCE039ComputedClaims:
    """CE039: a prose surface's arithmetic must be checked by COMPUTING it, not by matching text.

    Every other prose sensor in this file asserts a string is PRESENT. None asserts that what it
    says is TRUE — and two false claims shipped past all of them in one change: "the statistic
    cannot be computed from one run dir" (it can) and a successive-halving cost "saving" that is
    arithmetically a premium.

    The registry lives in `tests/lint/computed_claims.py`; this class runs it, and adds the
    coverage rule that makes it a sensor CLASS rather than three more bespoke sensors — an
    arithmetic-bearing table no registered claim names is a failure. Like CE026-CE031 and
    CE033-CE038 it is a test class rather than a `BaseRule` in `tests/lint/runner.py`, which is an
    AST walk over `src/**/*.py`.
    """

    def test_registered_claims_hold(self, tmp_path: Path):
        from tests.lint.computed_claims import CLAIMS, evaluate_claims

        failures = evaluate_claims(tmp_path)
        assert not failures, (
            "a registered computed claim about the optimize surfaces no longer holds:\n  "
            + "\n  ".join(failures)
            + "\n\nEach claim is RECOMPUTED from the prose, so this is the prose being wrong rather "
            + "than a token having moved. Why each exists:\n  "
            + "\n  ".join(f"{c.id}: {c.why}" for c in CLAIMS)
        )

    def test_every_arithmetic_table_is_covered(self):
        # The coverage rule. A new table carrying arithmetic must be named by a claim that computes
        # it, or it ships unchecked and nothing says so.
        from tests.lint.computed_claims import COVERED_SURFACES, uncovered_tables

        uncovered = [entry for surface in COVERED_SURFACES for entry in uncovered_tables(surface)]
        assert not uncovered, (
            f"arithmetic-bearing tables no ComputedClaim covers: {uncovered}. Register a claim in "
            "tests/lint/computed_claims.py::CLAIMS whose `covers` names the table's header "
            "signature and whose `check` RECOMPUTES what the table asserts — a table nobody "
            "computes is exactly how a cost 'saving' that was really a premium shipped."
        )

    def test_arithmetic_tables_finds_exactly_the_known_tables(self):
        # Pinned so a widened predicate is a visible diff rather than a quietly noisier gate.
        from tests.lint.computed_claims import METHOD, SKILL, arithmetic_tables, table_signature

        found = {
            surface.name: [table_signature(t) for t in arithmetic_tables(surface.read_text(encoding="utf-8"))]
            for surface in (METHOD, SKILL)
        }
        assert found == {
            "optimize-method.md": ["Spend | Runs", "arms `A` | flat | halved | premium"],
            "SKILL.md": [
                "survivors gated `S` | Holm threshold | discordant rows needed at 8 paired rows | at 20",
                "rule | rows failing | headroom | ceiling | against the 0.0255 floor",
            ],
        }, f"the arithmetic-table predicate now finds {found}"

    def test_every_claim_surface_exists(self):
        # A claim pointing at a deleted file is a claim that silently stops checking.
        from tests.lint.computed_claims import CLAIMS

        missing = sorted(c.id for c in CLAIMS if not c.surface.is_file())
        assert not missing, f"claims registered against a file that no longer exists: {missing}"

    def test_the_matcher_catches_a_wrong_cell(self, tmp_path: Path):
        """The self-test. Runs the REAL halving checker against a hand-built table with a bad cell.

        Mirrors `_wrong_skill_count_offenders`: a sensor with no self-test can be reverted to a
        no-op with every test still green, which is the exact failure class this file exists to
        prevent. Nothing transient, nothing to revert — the wrong table is built here.
        """
        from tests.lint.computed_claims import TIMES, _check_halving_premium

        # TIMES is the surfaces' own multiplication sign, imported rather than typed: ruff's
        # RUF001 flags the literal as ambiguous, and the normalizer under test is what makes it
        # parseable in the first place.
        good = (
            "| arms `A` | flat | halved | premium |\n"
            "| --- | --- | --- | --- |\n"
            f"| 4 | `4 {TIMES} M_train` | `4 {TIMES} M_train` | **none** |\n"
            f"| 5 | `5 {TIMES} M_train` | `5.5 {TIMES} M_train` | `M_train/2` |\n"
        )
        assert _check_halving_premium(good, tmp_path) == []

        # A=5 halving costs half a train split; claiming **none** is the error v1 shipped.
        wrong = good.replace(
            f"| 5 | `5 {TIMES} M_train` | `5.5 {TIMES} M_train` | `M_train/2` |",
            f"| 5 | `5 {TIMES} M_train` | `5.5 {TIMES} M_train` | **none** |",
        )
        failures = _check_halving_premium(wrong, tmp_path)
        assert failures and any("premium" in f and "A=5" in f for f in failures), failures

    def test_the_sizing_matcher_catches_a_wrong_cell(self, tmp_path: Path):
        """The sizing claim's self-test, in the same shape as the halving one above.

        The table a user sizes a suite from is exactly the kind that goes stale plausibly: every
        cell is a small integer, and a wrong one reads like a right one. So the checker is run here
        against a hand-built table with one cell moved.
        """
        from tests.lint.computed_claims import _check_sizing_table

        good = (
            "| survivors gated `S` | Holm threshold | discordant rows needed at 8 paired rows | at 20 |\n"
            "| --- | --- | --- | --- |\n"
            "| 1 | `alpha/1` | 3 | 4 |\n"
            "| 5 | `alpha/5` | 4 | 5 |\n"
        )
        assert _check_sizing_table(good, tmp_path) == []

        # One row understated: a user reads "3 disagreeing rows is enough at S=5" and sizes for it.
        wrong = good.replace("| 5 | `alpha/5` | 4 | 5 |", "| 5 | `alpha/5` | 3 | 5 |")
        failures = _check_sizing_table(wrong, tmp_path)
        assert failures and any("S=5 at 8 rows" in f for f in failures), failures

        # And a threshold column that stops being alpha/S is caught before any count is compared —
        # otherwise a table could pass by having both halves wrong in agreement.
        skewed = good.replace("| 5 | `alpha/5` | 4 | 5 |", "| 5 | `alpha/2` | 4 | 5 |")
        assert any("not alpha/S" in f for f in _check_sizing_table(skewed, tmp_path))

    def test_a_wrong_split_symbol_is_reported_not_raised(self, tmp_path: Path):
        """A cell that PARSES but names an unexpected symbol must fail, not raise.

        Found by mutating the shipped table: renaming Stage C's `M_test` to `M_holdout` — the exact
        drift class this claim exists for — reached the evaluator and raised out of the lint test.
        A rule that crashes on the drift it is for reads as a broken sensor rather than a wrong
        table, so the caller catches it and names the symbol.
        """
        from tests.lint.computed_claims import METHOD, TIMES, _check_cost_table

        text = METHOD.read_text(encoding="utf-8")
        assert _check_cost_table(text, tmp_path) == []

        renamed = text.replace(f"`6 {TIMES} M_test`", f"`6 {TIMES} M_holdout`", 1)
        assert renamed != text, "the anchor moved — re-derive it from the cost table"
        failures = _check_cost_table(renamed, tmp_path)
        assert failures and any("M_holdout" in f for f in failures), failures

    def test_the_search_loop_may_not_be_priced_above_the_smallest_stage_a(self, tmp_path: Path):
        """Invariant 5's self-test, in the shape of the split-symbol one above.

        The search loop exists because it runs ONE arm; the plausible wrong edit is to co-run the
        incumbent "for a fair comparison", which doubles the phase and deletes its reason to exist.
        Priced at `4 x M_train` it would be dearer than a whole two-arm Stage A and the prose would
        still read as a saving, so the invariant is computed rather than matched.
        """
        from tests.lint.computed_claims import METHOD, TIMES, _check_cost_table

        text = METHOD.read_text(encoding="utf-8")
        assert _check_cost_table(text, tmp_path) == []

        overpriced = text.replace(f"`1 {TIMES} M_train`", f"`4 {TIMES} M_train`", 1)
        assert overpriced != text, "the anchor moved — re-derive it from the cost table's search row"
        failures = _check_cost_table(overpriced, tmp_path)
        assert failures and any("search round costs" in f for f in failures), failures

    def test_the_coverage_rule_fails_on_an_unregistered_table(self, tmp_path: Path):
        """The second self-test, and the committed replacement for "edit a shipped file to prove it".

        Three tables, only two of whose signatures a claim covers; the third must be reported by
        header.
        """
        from tests.lint.computed_claims import ComputedClaim, uncovered_tables

        surface = tmp_path / "surface.md"
        surface.write_text(
            "| a | b |\n| --- | --- |\n| `2 * M` | x |\n\n"
            "| c | d |\n| --- | --- |\n| `ceil(N/2)` | y |\n\n"
            "| e | f |\n| --- | --- |\n| `N+1` | z |\n",
            encoding="utf-8",
        )
        claims = [
            ComputedClaim(id="c", surface=surface, why="", covers=("a | b", "c | d"), check=lambda _t, _p: []),
        ]
        uncovered = uncovered_tables(surface, claims)
        assert len(uncovered) == 1 and "`e | f`" in uncovered[0], uncovered

    def test_evaluate_expression_rejects_a_call_it_does_not_whitelist(self):
        from tests.lint.computed_claims import evaluate_expression

        with pytest.raises(ValueError, match="other than ceil"):
            evaluate_expression("open('x')", {})
        with pytest.raises(ValueError, match="disallowed"):
            evaluate_expression("[1, 2]", {})

    def test_evaluate_expression_names_an_unbound_symbol(self):
        # Never a bare NameError from three frames down, where prose drift is indistinguishable
        # from a parser bug.
        from tests.lint.computed_claims import evaluate_expression

        with pytest.raises(ValueError, match="M_holdout"):
            evaluate_expression("2 * M_holdout", {"M_train": 1.0})

    def test_evaluate_expression_handles_ceil_and_unicode_multiply(self):
        from tests.lint.computed_claims import TIMES, evaluate_expression

        expr = f"(N+1) {TIMES} M_train/2 + ceil((N+1)/2) {TIMES} M_train"
        got = evaluate_expression(expr, {"N": 4.0, "M_train": 12.0})
        assert got == 66.0

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [("2 + 3", 5.0), ("7 - 2", 5.0), ("3 * 4", 12.0), ("9 / 2", 4.5), ("-5", -5.0), ("-(2 + 1)", -3.0)],
    )
    def test_evaluate_expression_computes_every_operator_it_admits(self, expr: str, expected: float) -> None:
        """The whitelist IS the dispatch, so every admitted operator must have a working function.

        Both halves are covered — the four binary operators and the one unary — because a dict
        keyed on a type cannot be partially implemented but CAN be mis-mapped: `ast.Sub` pointing at
        `operator.add` would type-check and pass every other test in this file.
        """
        from tests.lint.computed_claims import evaluate_expression

        assert evaluate_expression(expr, {}) == expected

    def test_evaluate_expression_rejects_an_operator_it_does_not_implement(self) -> None:
        """The case CE044 existed for, now proven by BEHAVIOUR rather than by a parity scan.

        CE044 compared the whitelist tuple against the `match` arms and reported a drift. There is
        no drift to report now — admitting `ast.Mod` means adding it to `_BINARY_OPS` with a
        function — so what needs asserting is that an unadmitted operator RAISES and names itself,
        which is what tells prose drift apart from a parser bug.
        """
        from tests.lint.computed_claims import evaluate_expression

        with pytest.raises(ValueError, match="disallowed operator Mod"):
            evaluate_expression("7 % 2", {})
        with pytest.raises(ValueError, match="disallowed operator Pow"):
            evaluate_expression("2 ** 3", {})

    def test_the_dispatch_tables_are_the_whitelist(self) -> None:
        """One declaration, asserted as one: nothing else may enumerate the allowed operators.

        The anti-vacuity guard for the two tests above — if either table were emptied they would
        still pass by raising, so this pins that the tables are non-empty and disjoint by arity.
        """
        from tests.lint.computed_claims import _BINARY_OPS, _UNARY_OPS

        assert set(_BINARY_OPS) == {ast.Add, ast.Sub, ast.Mult, ast.Div}
        assert set(_UNARY_OPS) == {ast.USub}
        assert not set(_BINARY_OPS) & set(_UNARY_OPS), "an operator cannot be both arities"

    def test_the_execution_sign_claim_is_registered(self):
        # Deleting the claim would otherwise reduce coverage silently — `covers=()` means the
        # table-coverage rule cannot notice its absence, because it checks a SENTENCE.
        from tests.lint.computed_claims import CLAIMS

        assert "execution-sign-resolution" in {c.id for c in CLAIMS}

    def test_the_execution_sign_claim_fails_on_a_reworded_sentence(self, tmp_path: Path):
        """The presence half's self-test, in the style of the wrong-cell matchers above.

        A reworded sign sentence must FAIL loudly and be re-pinned deliberately, rather than
        quietly stop being checked.
        """
        from tests.lint.computed_claims import METHOD, _check_execution_sign_resolution

        text = METHOD.read_text(encoding="utf-8")
        assert _check_execution_sign_resolution(text, tmp_path / "real") == []

        # Every occurrence: the file states the rule twice, and leaving one behind would make the
        # sensor look green against a reworded sentence.
        assert text.count("candidate minus incumbent") >= 2, "the anchor moved — re-derive it"
        reworded = text.replace("candidate minus incumbent", "the candidate's score less the incumbent's")
        failures = _check_execution_sign_resolution(reworded, tmp_path / "reworded")
        assert failures and any("candidate minus incumbent" in f for f in failures), failures

    def test_the_execution_sign_claim_fails_on_a_reversed_verdict(self, tmp_path: Path, monkeypatch):
        """The behavioural half's self-test, driving the REAL claim function.

        A claim whose behavioural half was reverted to `return []` would pass every other test in
        this class. So `exec_gate` — which the claim imports at call time — is swapped for a
        wrapper that negates `mean_diff` on one declaration order only: exactly the shape a gate
        reading `first_declared - second_declared` produces, and exactly the defect the method
        file says promotes the arm that lost.

        Patched on `tests.optimize_fixtures`, the shared builder module the claim now imports from —
        NOT on the test file it used to live in, which is the inversion that module exists to undo.
        """
        import tests.optimize_fixtures as fixtures
        from tests.lint.computed_claims import METHOD, _check_execution_sign_resolution

        text = METHOD.read_text(encoding="utf-8")
        real_exec_gate = fixtures.exec_gate

        def _sign_blind(run_dir, **kwargs):
            verdict = real_exec_gate(run_dir, **kwargs)
            # The claim builds each arm's fixture UNDER a named parent, so the marker is in the
            # path rather than in `run_dir.name` (which is the builder's own "round1-gate").
            if "declared-candidate-first" in run_dir.as_posix() and verdict.mean_diff is not None:
                return verdict.model_copy(update={"mean_diff": -verdict.mean_diff})  # CE048 scans src/ only
            return verdict

        monkeypatch.setattr(fixtures, "exec_gate", _sign_blind)
        failures = _check_execution_sign_resolution(text, tmp_path / "blind")
        assert failures, "the claim's behavioural half did not notice a sign-blind gate"
        assert any("candidate first" in f for f in failures), failures
        assert any("disagree" in f for f in failures), failures

    def test_parse_markdown_tables_skips_fenced_blocks(self):
        from tests.lint.computed_claims import parse_markdown_tables

        text = "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```\n| x | y |\n| --- | --- |\n| 3 | 4 |\n```\n"
        tables = parse_markdown_tables(text)
        assert [t.header for t in tables] == [["a", "b"]]


@pytest.mark.lint
class TestEstimatorLedger:
    """The estimator-change protocol's pure predicate — the half that does not touch git.

    A rendered statistic can step for IDENTICAL data when an estimator or resample count changes,
    and nothing in a run artifact distinguishes that from a real change in the measurement. The
    `## Estimator changes` table in `docs/REPORT_SCHEMA.md` is where such a step becomes
    attributable, and the `estimator-protocol` CI job demands a row for a PR that causes one.

    Only `main()` shells out; everything asserted here is a pure function of a
    `path -> diff text` map, so the retro-validation below uses the REAL diff text from the commit
    that motivated the rule rather than checking out history.
    """

    BASE_DOC: ClassVar[str] = (
        "## Estimator changes\n\n| Date | Change | Constant / fixture | Observed step | PR / commit |\n"
        "| --- | --- | --- | --- | --- |\n| 2026-08-13 | a | b | c | d |\n\n## Next section\n"
    )

    @staticmethod
    def _with_extra_row(doc: str) -> str:
        return doc.replace("\n\n## Next section", "\n| 2026-08-16 | e | f | g | h |\n\n## Next section")

    def test_a_snapshot_change_requires_the_ledger(self) -> None:
        from tests.lint.estimator_ledger import check

        failures = check(["tests/_fixtures/report_snapshots/run_full.md"], {}, self.BASE_DOC, self.BASE_DOC)
        assert failures and "run_full.md" in failures[0], failures

    def test_a_constant_change_requires_the_ledger(self) -> None:
        from tests.lint.estimator_ledger import check

        module = "src/coder_eval/reports_stats.py"
        failures = check(
            [module],
            {module: "-BOOTSTRAP_RESAMPLES = 1000\n+BOOTSTRAP_RESAMPLES = 2000\n"},
            self.BASE_DOC,
            self.BASE_DOC,
        )
        assert failures and any("BOOTSTRAP_RESAMPLES" in f for f in failures), failures

    def test_a_gate_constant_change_requires_the_ledger(self) -> None:
        # The watch set is not `reports_stats`-only: every gate constant steps a rendered gate
        # number exactly the way BOOTSTRAP_RESAMPLES stepped a rendered CI.
        from tests.lint.estimator_ledger import check

        module = "src/coder_eval/optimize/gate.py"
        failures = check(
            [module],
            {module: "-MATERIALITY_FLOOR = 0.25\n+MATERIALITY_FLOOR = 0.10\n"},
            self.BASE_DOC,
            self.BASE_DOC,
        )
        assert failures and any("MATERIALITY_FLOOR" in f for f in failures), failures

    def test_a_new_ledger_row_satisfies_it(self) -> None:
        from tests.lint.estimator_ledger import check

        module = "src/coder_eval/reports_stats.py"
        assert (
            check(
                [module],
                {module: "-BOOTSTRAP_RESAMPLES = 1000\n+BOOTSTRAP_RESAMPLES = 2000\n"},
                self.BASE_DOC,
                self._with_extra_row(self.BASE_DOC),
            )
            == []
        )

    def test_an_unrelated_edit_to_the_page_does_not_satisfy_it(self) -> None:
        # Why the check is a ROW COUNT and not "the file was touched": the ledger lives in a busy
        # page, and a typo fix three sections away would otherwise clear the gate.
        from tests.lint.estimator_ledger import check

        module = "src/coder_eval/reports_stats.py"
        edited = self.BASE_DOC.replace("## Next section", "## Next section (renamed)")
        assert edited != self.BASE_DOC
        assert check([module], {module: "+BOOTSTRAP_RESAMPLES = 2000\n"}, self.BASE_DOC, edited)

    def test_an_unrelated_change_is_clean(self) -> None:
        from tests.lint.estimator_ledger import check

        assert check(["src/coder_eval/reports.py"], {}, self.BASE_DOC, self.BASE_DOC) == []

    def test_a_comment_only_diff_in_reports_stats_is_clean(self) -> None:
        # `git diff -U0` hunks are context-free changed lines, so matching on the ASSIGNMENT
        # keeps a comment reflow beside the constant from firing.
        from tests.lint.estimator_ledger import check

        module = "src/coder_eval/reports_stats.py"
        hunk = "-# BOOTSTRAP_RESAMPLES is the one resample count.\n+# BOOTSTRAP_RESAMPLES: the one resample count.\n"
        assert check([module], {module: hunk}, self.BASE_DOC, self.BASE_DOC) == []

    def test_every_watched_constant_exists_on_its_module(self) -> None:
        """The anti-rename parity assertion — the single most important test in this phase.

        A renamed constant would make the diff scan match nothing and the job pass **silently**,
        which is worse than no job at all. It lives here rather than at module import because the
        CI job installs nothing and the checker must load without `coder_eval`.
        """
        import importlib

        from tests.lint.estimator_ledger import WATCHED_CONSTANTS

        missing = []
        for path, name in WATCHED_CONSTANTS:
            module_name = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
            if not hasattr(importlib.import_module(module_name), name):
                missing.append(f"{module_name}.{name}")
        assert not missing, (
            f"{missing} no longer resolve. A renamed watched constant does not fail the "
            "estimator-protocol job — it makes the job match nothing and pass silently."
        )

    def test_every_watched_constants_real_source_line_still_matches(self) -> None:
        """`hasattr` is not enough: the DIFF SCAN is a regex, and a re-declaration can dodge it.

        `BOOTSTRAP_RESAMPLES: Final[int] = 2000` keeps `hasattr` true while changing the shape the
        scan matches — the same silent pass the parity test above exists to prevent, one layer
        down. So the pattern is run against each constant's real source line.
        """
        from tests.lint.estimator_ledger import WATCHED_CONSTANTS, _assignment_pattern

        repo = REPO_ROOT
        unmatched = []
        for path, name in WATCHED_CONSTANTS:
            source = (repo / path).read_text(encoding="utf-8")
            declaration = next((line for line in source.splitlines() if line.startswith(name)), None)
            if declaration is None or not _assignment_pattern(name).search(f"+{declaration}"):
                unmatched.append(f"{path}::{name}")
        assert not unmatched, (
            f"{unmatched} are declared in a shape the diff scan does not match — the job would "
            "see the change and say nothing."
        )

    def test_every_snapshot_directory_still_exists_and_holds_fixtures(self) -> None:
        """The fixture half's anti-rename guard, and it is not symmetrical with the constants'.

        `git diff --name-only` reports only a rename's POST-image path, so moving a fixture
        directory disables this half FOREVER while every unit test here — which hardcodes literal
        paths — stays green.
        """
        from tests.lint.estimator_ledger import SNAPSHOT_DIRS, SNAPSHOT_SUFFIXES

        repo = REPO_ROOT
        empty = [
            d
            for d in SNAPSHOT_DIRS
            if not any(p.suffix in SNAPSHOT_SUFFIXES for p in (repo / d).glob("*") if p.is_file())
        ]
        assert not empty, f"{empty} hold no pinned fixtures — moved, or renamed past the watch list?"

    def test_the_optimize_renders_are_watched(self) -> None:
        # The estimator-FORM blind spot's only backstop: `bootstrap_p_floor`'s value is rendered
        # into these fixtures, so a form change lands there even though no constant moved.
        from tests.lint.estimator_ledger import is_watched_snapshot

        assert is_watched_snapshot("tests/_fixtures/optimize_renders/activation_gate.md")
        assert is_watched_snapshot("tests/_fixtures/optimize_verdicts/activation_gate.json")
        assert not is_watched_snapshot("tests/_fixtures/report_snapshots/_snapshot.py")
        assert not is_watched_snapshot("tests/_fixtures/golden_streams/anything.md")

    def test_estimator_rows_reads_the_real_page(self) -> None:
        # Anti-vacuity: a broken parser returning 0 would make every row-count comparison pass.
        from tests.lint.estimator_ledger import LEDGER_DOC, estimator_rows

        page = (REPO_ROOT / LEDGER_DOC).read_text(encoding="utf-8")
        assert estimator_rows(page) >= 1

    def test_estimator_rows_finds_the_table_by_signature_not_by_position(self) -> None:
        # A second table added above the ledger inside the section must not retarget the count.
        from tests.lint.estimator_ledger import estimator_rows

        decoy = self.BASE_DOC.replace(
            "## Estimator changes\n\n",
            "## Estimator changes\n\n| x | y |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\n\n",
        )
        assert estimator_rows(decoy) == 1

    def test_main_passes_and_fails_on_a_real_repository(self, tmp_path: Path) -> None:
        """`main()` is the merge-blocking half, so it is exercised against real git, not mocked.

        A fixture repo makes the plumbing this rule actually depends on observable: three-dot
        resolution through the merge base, `--diff-filter=M` on the fixture side, and `git show`
        of the base doc.
        """
        import os
        import subprocess

        from tests.lint.estimator_ledger import LEDGER_DOC, main

        def run(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        doc = tmp_path / LEDGER_DOC
        doc.parent.mkdir(parents=True)
        doc.write_text(self.BASE_DOC, encoding="utf-8")
        stats = tmp_path / "src" / "coder_eval" / "reports_stats.py"
        stats.parent.mkdir(parents=True)
        stats.write_text("BOOTSTRAP_RESAMPLES = 1000\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-qm", "base")

        cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            run("checkout", "-qb", "feature")
            stats.write_text("BOOTSTRAP_RESAMPLES = 2000\n", encoding="utf-8")
            run("add", "-A")
            run("commit", "-qm", "bump")
            assert main("main") == 1, "a watched constant moved with no ledger row and the job passed"

            doc.write_text(self._with_extra_row(self.BASE_DOC), encoding="utf-8")
            run("add", "-A")
            run("commit", "-qm", "ledger")
            assert main("main") == 0, "the ledger gained a row and the job still failed"
        finally:
            os.chdir(cwd)

    def test_the_module_imports_under_a_bare_interpreter(self) -> None:
        """The CI job installs nothing, so the whole import chain must be stdlib-only.

        Asserted by importing it in a subprocess under `-S`, which skips site-packages entirely —
        so `pydantic` and an installed `coder_eval` are both unreachable, exactly as in the job.
        `-c` puts the cwd on `sys.path`, which is how the job's `python -m` finds `tests` too. One
        future `import pydantic` in `tests/__init__.py` would otherwise turn every PR red with a
        ModuleNotFoundError.
        """
        import subprocess
        import sys

        repo = REPO_ROOT
        result = subprocess.run(
            [sys.executable, "-S", "-c", "import tests.lint.estimator_ledger"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_it_fires_on_the_historical_commit_that_motivated_it(self) -> None:
        """Retro-validation, with `b306a99`'s real inputs rather than a checkout of history.

        That commit set `BOOTSTRAP_RESAMPLES = 2000` and moved a snapshot's two CI upper bounds.
        BOTH signals must fire, or the rule was written against a defect it could not have caught.

        The two inputs are RESOLVED against the tree rather than typed as literals: the module and
        the fixture must still exist at those paths, or this is only a restatement of the two
        trigger tests above with different strings.
        """
        from tests.lint.estimator_ledger import check

        repo = REPO_ROOT
        module = "src/coder_eval/reports_stats.py"
        snapshot = "tests/_fixtures/report_snapshots/experiment_replicates.md"
        assert (repo / module).is_file() and (repo / snapshot).is_file(), (
            "the commit's own inputs have moved — this test no longer retro-validates anything"
        )
        # And the constant is still declared there, so the diff line below is a real shape.
        assert "BOOTSTRAP_RESAMPLES" in (repo / module).read_text(encoding="utf-8")

        failures = check(
            [module, snapshot],
            {module: "+BOOTSTRAP_RESAMPLES = 2000\n"},
            self.BASE_DOC,
            self.BASE_DOC,
        )
        assert any("BOOTSTRAP_RESAMPLES" in f for f in failures), failures
        assert any("experiment_replicates.md" in f for f in failures), failures

    def test_the_documented_watch_list_matches_the_code(self) -> None:
        """`docs/REPORT_SCHEMA.md` enumerates the watched constants; that list is derived here.

        Two surfaces spelling one set is the drift CE036/`LEAK_LOCATOR_FIELDS` already needed a
        two-way test for. Adding a constant to `WATCHED_CONSTANTS` without documenting it leaves a
        consumer reading a boundary the job no longer has.
        """
        from tests.lint.estimator_ledger import LEDGER_DOC, WATCHED_CONSTANTS

        page = (REPO_ROOT / LEDGER_DOC).read_text(encoding="utf-8")
        section = page.split("## Estimator changes", 1)[1].split("\n## ", 1)[0]
        undocumented = sorted({name for _module, name in WATCHED_CONSTANTS if f"`{name}`" not in section})
        # This matches the NAME only, and that is a stated limitation rather than an oversight:
        # `FLOOR_RESOLUTION` moved module and this section went on attributing it to the old one with
        # every assertion green. A module check was written and then removed for being unfailable —
        # the section legitimately names an old module inside a ledger row describing the move, and
        # every watched module is named somewhere in the section anyway, so neither a subset rule nor
        # a proximity rule distinguishes the two. Deferred in `.claude/harness-candidates.md`.
        assert not undocumented, (
            f"{undocumented} are watched by the estimator-protocol job but absent from "
            f"{LEDGER_DOC}'s boundary paragraph, which claims to name the watch set."
        )

    def test_the_failure_message_names_the_escape_hatch(self) -> None:
        # A row EDITED rather than added does not raise the count, so a pure-correction PR fails.
        # That is intended, and the message has to say what to do about it.
        from tests.lint.estimator_ledger import check

        failures = check(["tests/_fixtures/report_snapshots/run_full.md"], {}, self.BASE_DOC, self.BASE_DOC)
        assert any("step was zero" in f for f in failures), failures
