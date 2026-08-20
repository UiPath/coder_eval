"""Lint tests: The stdlib-only `verify.py` an author copies — the execution track's measuring instrument."""

from pathlib import Path

import pytest

from tests.lint.import_resolution import resolved_module
from tests.lint_tests.plugin_base import PluginArtifactsBase
from tests.lint_tests.shared import PLUGIN_ROOT, _normalized


@pytest.mark.lint
class TestTheShippedOutcomeGrader(PluginArtifactsBase):
    """The stdlib-only `verify.py` an author copies — the execution track's measuring instrument.

    One of five classes carved out of `TestPluginArtifacts`; the shared class attributes and
    grader helpers live on :class:`PluginArtifactsBase`.
    """

    def test_outcome_grader_prints_float_on_first_line(self, tmp_path: Path):
        # The protocol `score_from_stdout` reads: line 1 parses as a float in [0.0, 1.0], every
        # later line is detail. A grader that printed its detail first would score 0.0 with the
        # criterion reporting a parse error, on every row of every arm.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": ["alpha"]}}}
        score, output, code = self._grade(tmp_path, spec, "# Report\n\nalpha\n")
        assert code == 0
        assert 0.0 <= score <= 1.0
        assert score == 1.0, output
        assert len(output.splitlines()) > 1, "the score line carries no detail — a 0.0 would be unreadable"
        # The other end of the same protocol, asserted here rather than in a third first-line test:
        # rule attribution is the LAST line, so adding it cannot have moved the score off line 1.
        assert not output.splitlines()[0].startswith("RULES "), output
        assert output.splitlines()[-1].startswith("RULES "), output

    def test_outcome_grader_na_drops_from_denominator(self, tmp_path: Path):
        # THE load-bearing rule. A check that does not apply must leave the numerator AND the
        # denominator: scoring it as a failure charges the row for a question nobody asked, and
        # scoring it as a pass inflates every arm equally — which is worse, because it is
        # invisible to every comparison downstream.
        #
        # The fixture is one PASS + one FAIL + one N/A, so the two failure modes are separated
        # NUMERICALLY (0.5 here; 1/3 if the N/A were counted as a failure, 2/3 as a pass) rather
        # than only by a substring in the detail lines.
        artifact = "# Report\n\nalpha\n"
        with_na = {
            "path": "out/report.md",
            "checks": {
                "mentions#hit": {"all_of": ["alpha"]},
                "mentions#miss": {"all_of": ["absent"]},
                "json_field": {},  # declares no field: N/A
            },
        }
        without = {
            "path": "out/report.md",
            "checks": {"mentions#hit": {"all_of": ["alpha"]}, "mentions#miss": {"all_of": ["absent"]}},
        }
        na_score, na_output, _ = self._grade(tmp_path / "a", with_na, artifact)
        plain_score, _, _ = self._grade(tmp_path / "b", without, artifact)
        assert na_score == plain_score == 0.5, na_output
        assert "N/A" in na_output and "1/2 applicable" in na_output, na_output

    def test_outcome_grader_na_never_depends_on_the_artifact(self, tmp_path: Path):
        """Two arms answering one row must be scored over the SAME denominator.

        The defect this pins is the one that inverts an A/B verdict rather than merely biasing it.
        When a check returns N/A because the ARTIFACT is the wrong shape, an arm that ignored the
        requirement entirely drops that check and is scored 1/1, while an arm that complied and got
        one field wrong is scored 1/2 — the worse artifact wins, and nothing in the report says so.

        So the N/A trigger must be a property of the ROW. Here the row asks for JSON; the arm that
        did not produce JSON must FAIL that question, not escape it.
        """
        spec = {
            "path": "out/report.md",
            "checks": {"mentions": {"all_of": ["summary"]}, "json_field": {"field": "status", "equals": "ok"}},
        }
        complied, complied_out, _ = self._grade(tmp_path / "a", spec, '{"summary": "did it", "status": "failed"}')
        ignored, ignored_out, _ = self._grade(tmp_path / "b", spec, "# summary\n\nprose, not JSON\n")
        assert "1/2 applicable" in complied_out, complied_out
        assert "1/2 applicable" in ignored_out, ignored_out
        assert complied == ignored == 0.5, (complied_out, ignored_out)

    def test_outcome_grader_rejects_a_string_where_a_list_is_required(self, tmp_path: Path):
        # `"all_of": "one needle"` — the most natural slip in a hand-written expectations file.
        # Iterating a string yields CHARACTERS, so every check would report "all present" against
        # any artifact of moderate length: a silent 1.0 on every row of every arm, which is exactly
        # the class of defect this instrument exists to avoid.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": "runs a lint step"}}}
        score, output, code = self._grade(tmp_path, spec, "An unrelated summary sentence.\n")
        assert code == 0
        assert score == 0.0, output
        assert "must be a LIST" in output, output

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ({"paths": "out/report.md", "checks": {}}, "string `path`"),
            ({"path": "out/report.md", "checks": [1, 2]}, "`checks` must be an object"),
            ([1, 2], "string `path`"),
        ],
        ids=["path-key-typo", "checks-is-a-list", "spec-is-a-list"],
    )
    def test_outcome_grader_reports_a_malformed_expectations_file(self, tmp_path: Path, spec, expected: str):
        # Author errors in the expectations file must report a score and a reason, never a
        # traceback: coder-eval checks the exit code BEFORE parsing the score line, so a crashed
        # grader is a 0.0 whose cause is not in the report.
        score, output, code = self._grade(tmp_path, spec, "x\n", artifact_path="out/report.md")
        assert code == 0
        assert score == 0.0
        assert expected in output, output

    def test_outcome_grader_keeps_one_check_to_one_detail_line(self, tmp_path: Path):
        # A detail carrying a newline would split into extra lines that read as further check
        # results. Here the forged text arrives through the artifact, quoted back by `mentions`'
        # "missing [...]" detail — so the line count is a fact about the OUTPUT FORMAT, not about
        # what a well-behaved check happens to return.
        forged = "absent\nPASS mentions#ghost: all present"
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": [forged]}}}
        score, output, code = self._grade(tmp_path, spec, "nothing here\n")
        assert code == 0 and score == 0.0
        lines = output.splitlines()
        # score + summary + exactly one detail line + the trailing RULES line
        assert len(lines) == 4, f"one check produced {len(lines) - 3} detail lines: {lines}"
        assert lines[-1].startswith("RULES "), lines
        assert not any(line.startswith("PASS") for line in lines), lines

    def test_outcome_grader_labels_let_one_check_be_declared_twice(self, tmp_path: Path):
        # JSON object keys are unique, so two `mentions` entries would silently collapse to the
        # last one — halving the denominator with no message. The `#label` suffix is what lets a
        # row carry several independent checks of one kind, which is also what makes it continuous.
        spec = {
            "path": "out/report.md",
            "checks": {"mentions#a": {"all_of": ["alpha"]}, "mentions#b": {"all_of": ["absent"]}},
        }
        score, output, code = self._grade(tmp_path, spec, "alpha only\n")
        assert code == 0
        assert score == 0.5, output
        assert "mentions#a" in output and "mentions#b" in output, output

    def test_outcome_grader_score_line_is_read_by_the_real_criterion(self, tmp_path: Path):
        # End to end through `RunCommandChecker._score_from_stdout`, not this file's own
        # `float(stdout.splitlines()[0])`: the tests would otherwise agree with the grader about a
        # protocol neither shares with the code that actually reads it.
        from coder_eval.criteria.run_command import RunCommandChecker
        from coder_eval.models import RunCommandCriterion

        spec = {
            "path": "out/report.md",
            "checks": {"mentions#a": {"all_of": ["alpha"]}, "mentions#b": {"all_of": ["absent"]}},
        }
        _score, stdout, code = self._grade(tmp_path, spec, "alpha only\n")
        criterion = RunCommandCriterion(description="grader", command="python3 verify.py r1", score_from_stdout=True)
        result = RunCommandChecker()._score_from_stdout(criterion, code, stdout, "")
        assert result.error is None, result.error
        assert result.score == 0.5
        assert "mentions#b" in (result.details or ""), "the detail lines did not survive into the criterion result"
        # `rule_row_map` reads the RULES line back out of exactly this field, so the round trip
        # through the real criterion is what proves the attribution is reachable at all.
        assert "RULES " in (result.details or ""), "the RULES line did not survive into the criterion result"

    def test_outcome_grader_missing_artifact_scores_zero_and_exits_zero(self, tmp_path: Path):
        # BOTH halves, because they are separate failures. `_score_from_stdout` checks the exit
        # code BEFORE it parses line 1 and returns early, so a non-zero exit discards whatever the
        # grader computed — the score becomes 0.0 no matter what the first line said.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": ["alpha"]}}}
        score, output, code = self._grade(tmp_path, spec, None)
        assert score == 0.0
        assert code == 0, "a non-zero exit discards the score line before it is ever parsed"
        assert "artifact not found" in output, output

    def test_outcome_grader_missing_expectations_file_scores_zero_and_exits_zero(self, tmp_path: Path):
        import shutil
        import subprocess
        import sys

        grader_dir = tmp_path / "grader"
        grader_dir.mkdir()
        shutil.copy(self.GRADER, grader_dir / "verify.py")
        (grader_dir / "expectations").mkdir()
        completed = subprocess.run(
            [sys.executable, str(grader_dir / "verify.py"), "no-such-row"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0
        assert float(completed.stdout.splitlines()[0]) == 0.0
        assert "no expectations file" in completed.stdout

    def test_outcome_grader_all_na_does_not_divide_by_zero(self, tmp_path: Path):
        # 0/0 is neither a perfect row nor a failed one — it is a row that measured NOTHING.
        # Printing 1.0 here would read as perfect; raising would take the whole run down. It
        # prints 0.0 and says so, which is what makes `/coder-eval:task`'s discrimination gate
        # catch such a row for free: the known-GOOD artifact scores 0.0 on it.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": []}, "json_field": {}}}
        score, output, code = self._grade(tmp_path, spec, "# Report\n\nanything\n")
        assert code == 0
        assert score == 0.0
        assert "0/0 applicable" in output, output

    def test_outcome_grader_unknown_check_excluded_from_denominator(self, tmp_path: Path):
        # A typo'd check name must not silently inflate the score by counting as a pass, and must
        # not fail a row for a check the author never wrote. It is skipped, loudly.
        spec = {
            "path": "out/report.md",
            "checks": {"mentions": {"all_of": ["alpha"]}, "menshuns": {"all_of": ["never present"]}},
        }
        score, output, code = self._grade(tmp_path, spec, "# Report\n\nalpha\n")
        assert code == 0
        assert score == 1.0
        assert "SKIP unknown check" in output and "1/1 applicable" in output, output

    def test_outcome_grader_a_raising_check_fails_that_check_only(self, tmp_path: Path):
        # One bad check must not crash the grader or zero an otherwise-scored row. `mentions`
        # raises on a non-list `all_of`, which is the cheapest real instance.
        spec = {
            "path": "out/report.md",
            "checks": {"mentions#ok": {"all_of": ["alpha"]}, "mentions#broken": {"all_of": 5}},
        }
        score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0
        assert score == 0.5, output
        assert "raised" in output, output

    def test_outcome_grader_emits_rules_line(self, tmp_path: Path):
        # Present, valid JSON, and NOT on line 1 — where `score_from_stdout` reads the score.
        spec = {
            "path": "out/report.md",
            "rules": {"mentions#core": "R1", "mentions#other": "R2"},
            "checks": {"mentions#core": {"all_of": ["alpha"]}, "mentions#other": {"all_of": ["absent"]}},
        }
        score, output, code = self._grade(tmp_path, spec, "alpha only\n")
        assert code == 0 and score == 0.5, output
        assert self._rules_line(output) == {"R1": "pass", "R2": "fail"}, output

    def test_outcome_grader_rules_line_absent_attribution_is_empty_object(self, tmp_path: Path):
        # A grader with nothing to attribute emits `RULES {}` — it must never OMIT the line. A
        # consumer has to tell "this row attributed nothing" from "this grader predates the
        # contract", and a missing line is the only signal for the second.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": ["alpha"]}}}
        _score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0
        assert self._rules_line(output) == {}, output

    def test_outcome_grader_rule_fails_if_any_check_fails(self, tmp_path: Path):
        # ANY-FAIL, ALL-NA — and the direction is load-bearing rather than a convention. Any-fail
        # counts the MOST rows as failing a rule, so the headroom estimate built on it is an UPPER
        # bound: a rule that cannot clear the noise floor even under the most generous attribution
        # is definitively unpromotable, which is the only claim the ceiling table makes. A
        # proportional or all-fail rule would make that claim unsound in the dangerous direction.
        spec = {
            "path": "out/report.md",
            "rules": {"mentions#a": "R1", "mentions#b": "R1", "mentions#c": "R2", "json_field": "R3"},
            "checks": {
                "mentions#a": {"all_of": ["alpha"]},
                "mentions#b": {"all_of": ["absent"]},
                "mentions#c": {"all_of": ["alpha"]},
                "json_field": {},
            },
        }
        _score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0
        assert self._rules_line(output) == {"R1": "fail", "R2": "pass", "R3": "na"}, output

    def test_outcome_grader_a_bare_check_name_attributes_every_label(self, tmp_path: Path):
        # One entry for a check declared three times, which is the shape an author writes by
        # default. Without the bare-name fallback the rule would silently attribute NO rows, and a
        # rule with no failing rows reads as "no headroom" — advice to stop, from a typo.
        spec = {
            "path": "out/report.md",
            "rules": {"mentions": "R1"},
            "checks": {
                "mentions#a": {"all_of": ["alpha"]},
                "mentions#b": {"all_of": ["alpha"]},
                "mentions#c": {"all_of": ["absent"]},
            },
        }
        _score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0
        assert self._rules_line(output) == {"R1": "fail"}, output

    def test_outcome_grader_reports_a_rules_entry_matching_no_check(self, tmp_path: Path):
        # A renamed or mistyped check key leaves its rule looking untouched by this row. Named in
        # the details rather than dropped, and it must not cost the row its score — attribution is
        # an annotation, never a measurement.
        spec = {
            "path": "out/report.md",
            "rules": {"mentions#core": "R1", "menshuns#core": "R2"},
            "checks": {"mentions#core": {"all_of": ["alpha"]}},
        }
        score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0 and score == 1.0, output
        assert "matching no declared check" in output, output
        assert self._rules_line(output) == {"R1": "pass"}, output

    def test_outcome_grader_a_malformed_rules_block_costs_no_score(self, tmp_path: Path):
        # `"rules": [...]` — an annotation typo. It reports and moves on: a row that measured
        # correctly must not be zeroed because its bookkeeping was wrong.
        spec = {"path": "out/report.md", "rules": ["R1"], "checks": {"mentions": {"all_of": ["alpha"]}}}
        score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0 and score == 1.0, output
        assert "`rules` must be an object" in output, output
        assert self._rules_line(output) == {}, output

    @pytest.mark.parametrize(
        ("argv", "spec_text", "artifact", "why"),
        [
            ([], None, None, "wrong argv"),
            (["r1"], None, None, "no expectations file"),
            (["r1"], "{not json", None, "invalid JSON"),
            (["r1"], "[1, 2]", None, "spec is not an object"),
            (["r1"], '{"path": "out/report.md", "checks": [1]}', None, "checks is not an object"),
            (["r1"], '{"path": "out/report.md", "checks": {}}', None, "artifact missing"),
            (["r1"], '{"path": "out", "checks": {}}', "dir", "artifact is a directory"),
        ],
        ids=["argv", "no-spec", "bad-json", "spec-not-object", "checks-not-object", "no-artifact", "artifact-is-dir"],
    )
    def test_outcome_grader_emits_rules_on_every_early_exit(
        self, tmp_path: Path, argv: list[str], spec_text: str | None, artifact: str | None, why: str
    ):
        # EVERY exit path, including the ones that run no check at all. `_report` owns the line for
        # exactly this reason: a consumer that cannot distinguish a crashed grader from an old one
        # has no way to tell "attribution is unavailable" from "this rule failed nowhere".
        import shutil
        import subprocess
        import sys

        grader_dir = tmp_path / "grader"
        grader_dir.mkdir()
        shutil.copy(self.GRADER, grader_dir / "verify.py")
        (grader_dir / "expectations").mkdir()
        if spec_text is not None:
            (grader_dir / "expectations" / "r1.json").write_text(spec_text, encoding="utf-8")
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        if artifact == "dir":
            (sandbox / "out").mkdir()

        completed = subprocess.run(
            [sys.executable, str(grader_dir / "verify.py"), *argv],
            cwd=sandbox,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, (why, completed.stderr)
        assert float(completed.stdout.splitlines()[0]) == 0.0, (why, completed.stdout)
        assert self._rules_line(completed.stdout) == {}, (why, completed.stdout)

    def test_every_line_of_the_shipped_grader_is_reachable(self, tmp_path: Path):
        """No dead code in the one file users COPY — measured, not reviewed.

        **Why this exists rather than a review note.** The scaffold is 460-odd lines of executable
        Python that ships to users, and it sits outside coverage entirely: `pyproject.toml` sets
        ``source = ["src/coder_eval"]``, and every other test here drives it through `subprocess`,
        where nothing is measured. So a branch that can never fire is invisible — and one shipped:
        a `__pycache__` filter guarding a path the file list never yielded, whose own test passed
        with the filter deleted. Reproduced before writing this: an added
        ``if "this-can-never-match" in path.parts: continue`` passed all 1331 tests in this file
        and `test_optimize_gate.py` (since split into the eight `test_optimize_*` files).

        It drives the scaffold IN-PROCESS rather than through `subprocess`, which is what makes the
        measurement possible at all, and covers every exit path the module docstring declares. That
        overlaps deliberately with the behaviour tests above: those assert what the grader SAYS,
        this asserts that every line of it can still be reached. Neither implies the other — a
        behaviour test passes over dead code, and this passes over wrong answers.

        **Boundary.** Line reachability, not branch reachability: a condition that is always true
        still executes its line. And the `if __name__` block is excluded, since an imported module
        never runs it — the `sys.exit` wrappers there are covered by the subprocess tests instead.
        """
        import contextlib
        import importlib.util
        import io
        import json
        import os
        import shutil

        import coverage

        grader = tmp_path / "outcome-grader"
        shutil.copytree(self.GRADER.parent, grader, ignore=shutil.ignore_patterns("__pycache__"))
        specs = grader / "expectations"
        # Every shape the dispatch loop and the spec reader can meet, so a line that only a
        # malformed input reaches still counts as live.
        (specs / "ok.json").write_text(
            json.dumps(
                {
                    "path": "out/report.md",
                    "rules": {"mentions#a": "R1", "mentions#gone": "R2", "bad": 7},
                    "checks": {
                        "mentions#a": {"all_of": ["alpha"]},
                        "mentions#b": {"all_of": ["absent"]},
                        "mentions#na": {"all_of": []},
                        "mentions#raises": {"all_of": "a string, not a list"},
                        "mentions#type": {"all_of": 5},
                        "json_field": {"field": "status", "equals": "ok"},
                        "json_field#absent": {"field": "nope"},
                        "json_field#none": {},
                        "json_field#bad": {"field": 7},
                        "unknown_check": {},
                        "mentions#params": "not an object",
                        "bad": {"all_of": ["x"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        (specs / "structured.json").write_text(
            json.dumps(
                {
                    "path": "out/data.json",
                    "checks": {
                        "json_field": {"field": "status", "equals": "ok"},
                        "json_field#missing": {"field": "nowhere-in-the-document"},
                        "json_field#present": {"field": "results"},
                        "chatty": {},
                        "weird": {},
                    },
                }
            ),
            encoding="utf-8",
        )
        (specs / "badrules.json").write_text(
            json.dumps({"path": "out/report.md", "rules": ["R1"], "checks": {"mentions": {"all_of": ["alpha"]}}}),
            encoding="utf-8",
        )
        (specs / "badruleid.json").write_text(
            json.dumps({"path": "out/report.md", "rules": {"mentions": 7}, "checks": {"mentions": {"all_of": ["a"]}}}),
            encoding="utf-8",
        )
        (specs / "unreadable.json").write_text(json.dumps({"path": "out/locked.md", "checks": {}}), encoding="utf-8")
        (specs / "notjson.json").write_text("{ not json", encoding="utf-8")
        (specs / "notobject.json").write_text("[1, 2]", encoding="utf-8")
        (specs / "badchecks.json").write_text(json.dumps({"path": "out/report.md", "checks": []}), encoding="utf-8")
        (specs / "nochecks.json").write_text(json.dumps({"path": "out/report.md"}), encoding="utf-8")
        (specs / "isdir.json").write_text(json.dumps({"path": "out", "checks": {}}), encoding="utf-8")

        sandbox = tmp_path / "sandbox"
        (sandbox / "out").mkdir(parents=True)
        (sandbox / "out" / "report.md").write_text("alpha and a chatty line", encoding="utf-8")
        # A LIST at the shallowest level, so the breadth-first walk descends through one.
        (sandbox / "out" / "data.json").write_text(
            json.dumps({"results": [{"status": "ok"}, {"status": "ok"}]}), encoding="utf-8"
        )
        (sandbox / "out" / "locked.md").write_text("unreadable", encoding="utf-8")

        target = str(grader / "verify.py")
        # `config_file=False` and `source=`, not `include=`: this repo's `[tool.coverage.run]` sets
        # `source = ["src/coder_eval"]`, which SILENTLY overrides an include and measures nothing
        # here — the same fail-open shape as the dead filter this test exists to catch.
        measured = coverage.Coverage(data_file=str(tmp_path / ".coverage"), source=[str(grader)], config_file=False)
        measured.start()
        try:
            spec = importlib.util.spec_from_file_location("shipped_grader_under_test", target)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # The dispatch loop's defensive paths are about checks an AUTHOR adds — the file says
            # "REPLACE / EXTEND: the checks your rows actually need" — so exercising them means
            # registering some. With only the two shipped checks they are unreachable, which is a
            # fact about the sample vocabulary, not about the dispatch.
            module.CHECKS["chatty"] = lambda _doc, _params: (print("a check that talks"), (True, "ok"))[1]
            module.CHECKS["weird"] = lambda _doc, _params: (1, "not a bool")

            previous = Path.cwd()
            os.chdir(sandbox)
            try:
                for argv in (
                    ["verify.py"],  # wrong argv
                    ["verify.py", "--fingerprint"],
                    ["verify.py", "ok"],
                    ["verify.py", "structured"],
                    ["verify.py", "missing-row"],
                    ["verify.py", "notjson"],
                    ["verify.py", "notobject"],
                    ["verify.py", "badchecks"],
                    ["verify.py", "nochecks"],
                    ["verify.py", "isdir"],
                    ["verify.py", "badrules"],
                    ["verify.py", "badruleid"],
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        module.main(argv)
                # A scalar artifact: `Artifact.data` stays None where the JSON is not a container.
                (sandbox / "out" / "data.json").write_text("7", encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    module.main(["verify.py", "structured"])

                # An artifact that exists and cannot be READ — the one `OSError` path.
                locked = sandbox / "out" / "locked.md"
                locked.chmod(0o000)
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        module.main(["verify.py", "unreadable"])
                finally:
                    locked.chmod(0o644)
            finally:
                os.chdir(previous)
        finally:
            measured.stop()

        analysis = measured._analyze(target)
        # The `if __name__` block never runs in an imported module; the subprocess tests above are
        # what cover it, and every one of them asserts the exit code it produces.
        source_lines = (grader / "verify.py").read_text(encoding="utf-8").splitlines()
        guard_at = next(i for i, line in enumerate(source_lines, 1) if line.startswith("if __name__"))
        dead = sorted(line for line in analysis.missing if line < guard_at)
        assert not dead, (
            "the shipped grader has lines no input can reach: "
            + ", ".join(f"{line}: {source_lines[line - 1].strip()!r}" for line in dead)
            + ". Dead code in the file users COPY is invisible to every other test here — it runs "
            'through subprocess, and the scaffold is outside `source = ["src/coder_eval"]`. A '
            "`__pycache__` filter guarding a path the file list never yielded shipped exactly this "
            "way, with a test that passed once the filter was deleted."
        )

    def test_outcome_grader_prints_the_protocol_from_exactly_one_place(self):
        # The structural half of "emitted on every exit path", which the parametrized test above
        # can only sample. The top-level `except` is unreachable from a fixture, so what actually
        # guarantees it is that `_report` is the ONE writer of both the score line and the RULES
        # line — a second `print` of either is a new exit path that can drift from the contract.
        source = self.GRADER.read_text(encoding="utf-8")
        assert source.count('print(f"{score:.4f}")') == 1, "the score line is printed from more than one place"
        assert source.count('print("RULES "') == 1, "the RULES line is printed from more than one place"
        assert source.count("def _report(") == 1

    def test_outcome_grader_rules_line_is_last_and_exactly_formatted(self, tmp_path: Path):
        # The exact bytes, because the line is machine-read: sorted keys and compact separators are
        # what let a consumer (and this test) assert a string rather than re-parse to compare.
        spec = {
            "path": "out/report.md",
            "rules": {"mentions#z": "R9", "mentions#a": "R1"},
            "checks": {"mentions#z": {"all_of": ["alpha"]}, "mentions#a": {"all_of": ["alpha"]}},
        }
        _score, output, _code = self._grade(tmp_path, spec, "alpha\n")
        assert output.splitlines()[-1] == 'RULES {"R1":"pass","R9":"pass"}', output

    def test_outcome_grader_discriminates_a_good_artifact_from_a_bad_one(self, tmp_path: Path):
        # The instrument's whole purpose, asserted as a SEPARATION rather than as two scores: a
        # grader that scores a compliant and a violating artifact alike measures nothing, and
        # every number after it is decoration. This is the test-shaped twin of the discrimination
        # gate `/coder-eval:task` step 6.5 requires an author to perform by hand.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": ["alpha", "beta"]}}}
        good, _, _ = self._grade(tmp_path / "good", spec, "# Report\n\nAlpha and BETA.\n")
        bad, _, _ = self._grade(tmp_path / "bad", spec, "# Report\n\nneither one.\n")
        assert good - bad == 1.0, f"separation margin is {good - bad}, not 1.0"

    def test_outcome_grader_is_stdlib_only(self):
        # The sandbox venv installs only what `sandbox.python.env_packages` names, and that list is
        # sized for the AGENT's needs. A third-party import here fails at grading time, on every
        # row, after the whole arm has been paid for.
        import ast
        import sys

        tree = ast.parse(self.GRADER.read_text(encoding="utf-8"))
        imported: set[str] = set()
        unresolved: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                # Through the ONE resolver (CE051) rather than reading `node.module`. Every import
                # in a standalone script is absolute, so the resolver just passes it through — but
                # a scan that is right only for the input it was written against is precisely how
                # `resolved_module` came to exist.
                module = resolved_module(node, str(self.GRADER))
                if module is None:
                    unresolved.append(ast.unparse(node))
                else:
                    imported.add(module.split(".")[0])
        assert imported, "no imports found — the scan is looking at the wrong file"
        assert not unresolved, (
            f"the shipped grader uses a relative import ({unresolved}), which cannot resolve: it is "
            "copied to a path of the user's choosing and run as a standalone script"
        )
        outsiders = sorted(name for name in imported if name not in sys.stdlib_module_names)
        assert not outsiders, f"the shipped grader imports non-stdlib modules: {outsiders}"

    def test_outcome_grader_lives_outside_any_mounted_fixture(self):
        """The answer key must not ship inside the exam.

        Everything under a mounted `template_dir` is copied into EVERY sandbox, so a grader's
        expectations placed there hand the agent exactly what it is being marked against — and the
        run still looks completely normal. `outcome.yaml`'s `sandbox:` comment carries the measured
        cost of that accident; this is the assertion that keeps the shipped layout honest.

        **Why this is not a numbered CE rule.** A tree-walking rule would have exactly one
        discoverable subject today, and that subject's mounted fixture is a placeholder directory —
        so the rule would pass whether or not it worked, which is the vacuous-pass failure CE044 and
        CE045 exist to prevent. It is reserved as CE056 in `.claude/harness-candidates.md`, to be
        promoted when a second outcome suite with a `run_command` grader appears. Scoped to
        `outcome.yaml` by construction, deliberately: this is not a repo-wide scan, and
        `tasks/skills/ci-outcome.yaml` (no `run_command` at all) is not a grader suite.
        """
        from coder_eval.models import RepoSource, RunCommandCriterion, StarterFilesSource, TemplateDirSource
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(self.TEMPLATES / "outcome.yaml")
        # EVERY member of the `TemplateSource` union is decided here, and the `else` is why: a
        # fourth source type must FAIL this test rather than fall through, or the mount set
        # silently shrinks on exactly the change that should widen it.
        mounts: list[Path] = []
        for source in task.sandbox.template_sources or []:
            if isinstance(source, TemplateDirSource):
                mounts.append(Path(source.path).resolve())
            elif isinstance(source, StarterFilesSource):
                # Inline content: each `StarterFile.path` is a sandbox DESTINATION, so this source
                # mounts no host directory and nothing of ours can be under it.
                continue
            elif isinstance(source, RepoSource):
                continue  # a remote clone: no local path exists until it is fetched
            else:
                pytest.fail(
                    f"GAP: {type(source).__name__} is a template source this assertion does not "
                    "classify. If it copies a host directory into the sandbox, its path must join "
                    "`mounts`; if it does not, say so — silence here reads as 'checked'"
                )
        assert mounts, (
            "GAP: no mounted directory was discovered on outcome.yaml, so this assertion proves "
            "nothing. Either the fixture mount was removed or `template_sources` was renamed — "
            "either way the layout is no longer being checked"
        )

        graders = [c for c in task.success_criteria if isinstance(c, RunCommandCriterion)]
        assert graders, "the outcome template ships no `run_command` grader to locate"
        # The path the template TELLS an author to use, and the scaffold this repo actually ships.
        # Both matter: the first is what a user copies, the second is what they copy it from.
        # `$TASK_DIR` is the suite YAML's own directory, exported into every `run_command` — the
        # portable spelling, expanded here the way the sandbox's shell would.
        declared = [
            Path(part.replace("$TASK_DIR", str(self.TEMPLATES)))
            for c in graders
            for part in c.command.split()
            if part.endswith(".py")
        ]
        assert declared, f"no .py path found in the grader command {graders[0].command!r}"
        bundled = self.GRADER.resolve()
        for candidate in [*declared, bundled, bundled.parent / "expectations"]:
            resolved = candidate.resolve() if candidate.is_absolute() else (self.TEMPLATES / candidate).resolve()
            for mount in mounts:
                # The PATH RELATIONSHIP, asserted whether or not the mount exists on disk — skipping
                # a missing directory is how this becomes the vacuous pass it exists to avoid.
                assert mount != resolved and mount not in resolved.parents, (
                    f"{resolved} sits under the mounted fixture {mount}, so it is copied into every "
                    "sandbox. A grader's expectations there tell the agent what it is being marked "
                    "against, every arm's score rises, and no cross-arm comparison can reveal it"
                )

    def test_grader_fairness_is_declared_once(self):
        # One declaration, two readers pointing at it from opposite ends: `task` asks these
        # questions when it WRITES a grader, `optimize-skill` when it reviews a baseline one. Both
        # restated them at one point; a second copy agrees on ordinary input and drifts exactly
        # where either was written for.
        rubric = _normalized(PLUGIN_ROOT / "reference" / "task-rubric.md")
        for question in ("penalise a legitimate alternative", "charge one mistake twice", "ever FAIL, and ever PASS"):
            assert question in rubric, f"the rubric's grader-fairness section lost {question!r}"
        assert "property of the ROW, never of the artifact" in rubric, (
            "the rubric no longer states which N/A triggers are legitimate. An N/A keyed on the "
            "ARTIFACT makes the denominator a function of the arm's own output — an arm that "
            "ignored a requirement is scored out of fewer checks than one that attempted it"
        )
        for name in ("task", "optimize-skill"):
            skill = _normalized(PLUGIN_ROOT / "skills" / name / "SKILL.md")
            assert "Grader fairness" in skill, (
                f"{name}/SKILL.md no longer points at the rubric's grader-fairness section"
            )
            assert "penalise a legitimate alternative" not in skill, (
                f"{name}/SKILL.md restates the fairness questions the rubric declares — two copies "
                "to drift, in the checks that exist to catch a grader nothing else can"
            )
