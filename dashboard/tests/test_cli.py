"""Tests for CLI commands."""

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from dashboard.cli import (
    _activation_case_facts,
    _build_activation_suite,
    _build_skills_suite,
    _enrich_activation_tasks,
    _finalize_activation_run,
    cli,
)


@patch("dashboard.cli.Config")
@patch("dashboard.blob.upload_run")
def test_upload_command(mock_upload_run, mock_config_cls, tmp_path):
    """Test that `dashboard upload` calls upload_run with the right args."""
    run_dir = tmp_path / "test-run"
    run_dir.mkdir()

    mock_cfg = MagicMock()
    mock_cfg.azure_storage_account = "teststorage"
    mock_cfg.azure_blob_container = "runs"
    mock_cfg.azure_storage_key = ""
    mock_config_cls.return_value = mock_cfg

    runner = CliRunner()
    result = runner.invoke(cli, ["upload", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "Uploaded" in result.output
    mock_upload_run.assert_called_once()
    assert "teststorage" in str(mock_upload_run.call_args)
    # No metadata flag → no meta.json written (daily-run re-upload stays as-is).
    assert not (run_dir / "meta.json").exists()


@patch("dashboard.cli.Config")
@patch("dashboard.blob.upload_run")
def test_upload_command_writes_meta(mock_upload_run, mock_config_cls, tmp_path):
    """--title/--description/--adhoc write a meta.json sidecar before upload."""
    import json

    run_dir = tmp_path / "codex-baseline"
    run_dir.mkdir()

    mock_cfg = MagicMock()
    mock_cfg.azure_storage_account = "teststorage"
    mock_cfg.azure_blob_container = "runs"
    mock_cfg.azure_storage_key = ""
    mock_config_cls.return_value = mock_cfg

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "upload",
            str(run_dir),
            "--title",
            "Codex baseline",
            "--description",
            "53 tasks, j=10",
            "--adhoc",
        ],
    )
    assert result.exit_code == 0, result.output
    mock_upload_run.assert_called_once()

    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta == {
        "adhoc": True,
        "title": "Codex baseline",
        "description": "53 tasks, j=10",
    }


def _setup_run_pipeline_mocks(mock_config_cls, tmp_path):
    """Configure shared mocks for `dashboard run` tests."""
    mock_cfg = MagicMock()
    mock_cfg.skills_dir = tmp_path / "skills"
    mock_cfg.uip_authority = ""
    mock_cfg.uip_client_id = ""
    mock_cfg.uip_client_secret = ""
    mock_cfg.uip_tenant = ""
    mock_cfg.uip_scope = "OR.Default"
    mock_cfg.azure_storage_account = "x"
    mock_cfg.azure_blob_container = "runs"
    mock_cfg.azure_storage_key = ""
    mock_config_cls.return_value = mock_cfg

    latest_run = tmp_path / "run-1"
    latest_run.mkdir()
    # run() reads each suite's run.json (written by coder-eval) to merge into one
    # combined summary; the mocked run_tests doesn't write it, so seed a minimal one.
    (latest_run / "run.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "task_results": [],
                "tasks_run": 0,
                "tasks_succeeded": 0,
                "tasks_failed": 0,
                "tasks_error": 0,
            }
        )
    )
    return mock_cfg, latest_run


@patch("dashboard.cli.Config")
def test_cli_run_calls_review(mock_config_cls, tmp_path):
    """`dashboard run` invokes generate_reviews after generate_analysis once for the merged run."""
    _, latest_run = _setup_run_pipeline_mocks(mock_config_cls, tmp_path)

    with (
        patch("dashboard.run.run_tests", return_value=latest_run),
        patch("dashboard.analysis.generate_analysis") as mock_analysis,
        patch("dashboard.review.generate_reviews") as mock_review,
        patch("dashboard.blob.upload_run"),
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["run", "--skip-pull", "--skip-login", "--suite", "smoke"],
        )

    assert result.exit_code == 0, result.output
    mock_analysis.assert_called_once_with(latest_run)
    mock_review.assert_called_once_with(latest_run)


@patch("dashboard.cli.Config")
def test_cli_skip_review(mock_config_cls, tmp_path):
    """--skip-review skips the call entirely."""
    _, latest_run = _setup_run_pipeline_mocks(mock_config_cls, tmp_path)

    with (
        patch("dashboard.run.run_tests", return_value=latest_run),
        patch("dashboard.analysis.generate_analysis"),
        patch("dashboard.review.generate_reviews") as mock_review,
        patch("dashboard.blob.upload_run"),
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "run",
                "--skip-pull",
                "--skip-login",
                "--skip-review",
                "--suite",
                "smoke",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_review.assert_not_called()
    assert "Skipping task reviews" in result.output


@patch("dashboard.cli.Config")
def test_cli_warns_when_skip_analysis_without_skip_review(mock_config_cls, tmp_path):
    """If analysis is skipped, the user should be warned reviews lose context."""
    _, latest_run = _setup_run_pipeline_mocks(mock_config_cls, tmp_path)

    with (
        patch("dashboard.run.run_tests", return_value=latest_run),
        patch("dashboard.review.generate_reviews"),
        patch("dashboard.blob.upload_run"),
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "run",
                "--skip-pull",
                "--skip-login",
                "--skip-analysis",
                "--suite",
                "smoke",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "WARNING: --skip-analysis is set but --skip-review is not" in result.output


@patch("dashboard.cli.Config")
def test_cli_review_failure_does_not_abort(mock_config_cls, tmp_path):
    """If review generation raises, upload_run still runs."""
    _, latest_run = _setup_run_pipeline_mocks(mock_config_cls, tmp_path)

    with (
        patch("dashboard.run.run_tests", return_value=latest_run),
        patch("dashboard.analysis.generate_analysis"),
        patch("dashboard.review.generate_reviews", side_effect=RuntimeError("boom")),
        patch("dashboard.blob.upload_run") as mock_upload,
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["run", "--skip-pull", "--skip-login", "--suite", "smoke"],
        )

    assert result.exit_code == 0, result.output
    mock_upload.assert_called_once()
    assert "Task review generation failed" in result.output


def test_build_skills_suite():
    """Test that _build_skills_suite constructs the right suite."""
    suite = _build_skills_suite("/path/to/skills")
    assert suite.name == "skills"
    assert suite.task_patterns == ["/path/to/skills/tests/tasks/**/*.yaml"]
    # Activation/ is carved out structurally; it's owned by the activation suite.
    assert suite.exclude_patterns == ["/path/to/skills/tests/tasks/activation/**/*.yaml"]
    assert suite.experiment == "/path/to/skills/tests/experiments/nightly.yaml"
    assert suite.uip_login is True
    assert suite.default is True
    assert suite.env == {"SKILLS_REPO_PATH": "/path/to/skills"}


def test_build_activation_suite():
    """Activation runs on the daily (default=True), capped at 20/skill via the suite."""
    suite = _build_activation_suite("/path/to/skills")
    assert suite.name == "activation"
    assert suite.task_patterns == ["/path/to/skills/tests/tasks/activation/**/*.yaml"]
    assert suite.experiment == "/path/to/skills/tests/experiments/activation.yaml"
    assert suite.default is True
    assert suite.concurrency == 20
    assert suite.sample_per_stratum == 20
    assert suite.uip_login is False
    assert suite.env == {"SKILLS_REPO_PATH": "/path/to/skills"}


@patch("dashboard.cli.Config")
def test_cli_run_nests_activation_as_subrun(mock_config_cls, tmp_path):
    """The nightly (skills,activation) leaves the skills run.json EXACTLY as
    coder-eval wrote it (no activation keys grafted on), and writes the activation
    suite into a nested <run>/activation/run.json — self-contained: enriched case
    rows in task_results plus the per-skill rollup in ['activation']."""
    mock_cfg, run_dir = _setup_run_pipeline_mocks(mock_config_cls, tmp_path)
    # Catalog for the rollup: 2 skills, only one of which the suite covers.
    (mock_cfg.skills_dir / "assets").mkdir(parents=True)
    (mock_cfg.skills_dir / "assets" / "skill-status.json").write_text(
        json.dumps({"skills": {"uipath-agents": {}, "uipath-mcp-servers": {}}})
    )

    def fake_run_tests(**kwargs):
        # Activation (the suite carrying --sample-per-stratum) runs into the
        # nested <run>/activation dir it was handed; skills into the run dir.
        if kwargs.get("sample_per_stratum"):
            act_dir = kwargs["run_dir"]
            act_dir.mkdir(parents=True, exist_ok=True)
            (act_dir / "run.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "task_results": [
                            {
                                "task_id": "skill-activation/uipath-agents-001",
                                "status": "SUCCESS",
                                "tags": ["activation"],
                            },
                            {
                                "task_id": "skill-activation/uipath-agents-002",
                                "status": "FAILURE",
                                "tags": ["activation"],
                            },
                        ],
                        "tasks_run": 2,
                        "tasks_succeeded": 1,
                        "tasks_failed": 1,
                        "tasks_error": 0,
                    }
                )
            )
            suite_dir = act_dir / "default" / "skill-activation"
            suite_dir.mkdir(parents=True, exist_ok=True)
            (suite_dir / "suite.json").write_text(
                json.dumps(
                    {
                        "rows_total": 2,
                        "criterion_aggregates": [
                            {
                                "criterion_type": "skill_triggered",
                                "description": "uipath-agents activation",
                                "metrics": {"recall.yes": 1.0},
                                "details": {"per_label": [{"label": "yes", "support": 20}]},
                            }
                        ],
                    }
                )
            )
            # Per-case task.json files feed the row enrichment (prompt/expected/triggered).
            # -001 fired the expected skill; -002 missed it.
            for row_id, prompt, observed in (
                ("uipath-agents-001", "Build a UiPath agent", "yes"),
                ("uipath-agents-002", "Make me an agent", "no"),
            ):
                case_dir = suite_dir / row_id / "00"
                case_dir.mkdir(parents=True, exist_ok=True)
                (case_dir / "task.json").write_text(
                    json.dumps(
                        {
                            "task_config": {"resolved": {"initial_prompt": prompt}},
                            "success_criteria_results": [
                                {
                                    "criterion_type": "skill_triggered",
                                    "description": "uipath-agents activation",
                                    "observed_label": observed,
                                    "expected_label": "yes",
                                }
                            ],
                        }
                    )
                )
            return act_dir
        # Skills suite: first call, run_dir is None → coder-eval picks the dir.
        dest = kwargs.get("run_dir") or run_dir
        (dest / "run.json").write_text(
            json.dumps(
                {
                    "run_id": "run-1",
                    "task_results": [{"task_id": "uipath-troubleshoot/x", "status": "SUCCESS", "tags": []}],
                    "tasks_run": 1,
                    "tasks_succeeded": 1,
                    "tasks_failed": 0,
                    "tasks_error": 0,
                }
            )
        )
        return dest

    with (
        patch("dashboard.run.run_tests", side_effect=fake_run_tests),
        patch("dashboard.analysis.generate_analysis"),
        patch("dashboard.review.generate_reviews"),
        patch("dashboard.blob.upload_run"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--skip-pull", "--skip-login", "--suite", "skills,activation"])

    assert result.exit_code == 0, result.output

    # Skills run.json: untouched by activation — skills-only task list/counts and
    # NO activation keys grafted on.
    skills = json.loads((run_dir / "run.json").read_text())
    assert [t["task_id"] for t in skills["task_results"]] == ["uipath-troubleshoot/x"]
    assert (skills["tasks_run"], skills["tasks_succeeded"], skills["tasks_failed"]) == (1, 1, 0)
    assert "activation" not in skills
    assert "activation_tasks" not in skills

    # Nested activation sub-run: self-contained, enriched, with the rollup.
    act_run = json.loads((run_dir / "activation" / "run.json").read_text())
    act_rows = {t["task_id"]: t for t in act_run["task_results"]}
    assert set(act_rows) == {
        "skill-activation/uipath-agents-001",
        "skill-activation/uipath-agents-002",
    }
    hit = act_rows["skill-activation/uipath-agents-001"]
    assert (hit["prompt"], hit["expected_skill"], hit["triggered_skill"]) == (
        "Build a UiPath agent",
        "uipath-agents",
        "uipath-agents",
    )
    miss = act_rows["skill-activation/uipath-agents-002"]
    assert (miss["prompt"], miss["expected_skill"], miss["triggered_skill"]) == (
        "Make me an agent",
        "uipath-agents",
        "",
    )
    # Rollup over the full 2-skill catalog: agents sampled @ recall 1.0, mcp a gap.
    act = act_run["activation"]
    assert act["n_cases"] == 2
    assert act["n_skills_sampled"] == 1
    assert act["denominator"] == 2
    assert act["score"] == 0.5


def test_activation_case_facts_positive_negative_and_empty():
    """expected_skill comes from the expected_label=='yes' criterion; triggered_skill
    is the comma-joined skills whose observed_label=='yes' ("" when nothing fired)."""
    # Positive, correct: expected skill fired, nothing else.
    positive = [
        {
            "criterion_type": "skill_triggered",
            "description": "uipath-agents activation",
            "observed_label": "yes",
            "expected_label": "yes",
        },
        {
            "criterion_type": "skill_triggered",
            "description": "uipath-rpa activation",
            "observed_label": "no",
            "expected_label": "no",
        },
    ]
    assert _activation_case_facts(positive) == ("uipath-agents", "uipath-agents")

    # Positive, wrong skill: expected agents, rpa fired instead.
    wrong_skill = [
        {
            "criterion_type": "skill_triggered",
            "description": "uipath-agents activation",
            "observed_label": "no",
            "expected_label": "yes",
        },
        {
            "criterion_type": "skill_triggered",
            "description": "uipath-rpa activation",
            "observed_label": "yes",
            "expected_label": "no",
        },
    ]
    assert _activation_case_facts(wrong_skill) == ("uipath-agents", "uipath-rpa")

    # Negative, false positive: nothing expected, something fired.
    negative_fp = [
        {
            "criterion_type": "skill_triggered",
            "description": "uipath-agents activation",
            "observed_label": "yes",
            "expected_label": "no",
        },
    ]
    assert _activation_case_facts(negative_fp) == ("", "uipath-agents")

    # Negative, correct rejection: nothing fired.
    negative_ok = [
        {
            "criterion_type": "skill_triggered",
            "description": "uipath-agents activation",
            "observed_label": "no",
            "expected_label": "no",
        },
    ]
    assert _activation_case_facts(negative_ok) == ("", "")

    # No skill_triggered criteria at all → unknown.
    assert _activation_case_facts([{"criterion_type": "llm_judge"}]) == ("", None)


def test_enrich_activation_tasks_reads_task_json_with_rowid_fallback(tmp_path):
    """Rows with a task.json get prompt/expected_skill/triggered_skill from it; rows
    without one fall back to a row_id-derived skill (negatives → no skill) + nulls."""
    suite_dir = tmp_path / "default" / "skill-activation"
    case_dir = suite_dir / "uipath-agents-003" / "00"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        json.dumps(
            {
                "task_config": {"resolved": {"initial_prompt": "Create a coded agent"}},
                "success_criteria_results": [
                    {
                        "criterion_type": "skill_triggered",
                        "description": "uipath-agents activation",
                        "observed_label": "yes",
                        "expected_label": "yes",
                    },
                ],
            }
        )
    )
    rows = [
        {"task_id": "skill-activation/uipath-agents-003", "status": "SUCCESS"},
        {"task_id": "skill-activation/negative-007", "status": "SUCCESS"},  # no task.json on disk
    ]
    enriched = _enrich_activation_tasks(rows, suite_dir)
    by_id = {r["task_id"]: r for r in enriched}

    have = by_id["skill-activation/uipath-agents-003"]
    assert (have["prompt"], have["expected_skill"], have["triggered_skill"]) == (
        "Create a coded agent",
        "uipath-agents",
        "uipath-agents",
    )

    # Missing task.json: row_id fallback (negative → empty skill), null prompt/triggered.
    gap = by_id["skill-activation/negative-007"]
    assert (gap["prompt"], gap["expected_skill"], gap["triggered_skill"]) == (None, "", None)


def test_finalize_activation_run_enriches_and_rolls_up_only_its_own_runjson(tmp_path):
    """_finalize_activation_run rewrites ONLY the nested activation run.json:
    folds prompt/expected/triggered onto its task_results and attaches the
    per-skill rollup. (The skills run.json isn't even in scope here.)"""
    skills_dir = tmp_path / "skills"
    (skills_dir / "assets").mkdir(parents=True)
    (skills_dir / "assets" / "skill-status.json").write_text(
        json.dumps({"skills": {"uipath-agents": {}, "uipath-mcp-servers": {}}})
    )

    act_dir = tmp_path / "run-1" / "activation"
    suite_dir = act_dir / "default" / "skill-activation"
    suite_dir.mkdir(parents=True)
    # Slim run.json as coder-eval writes it: rows without prompt/verdicts.
    (act_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "task_results": [{"task_id": "skill-activation/uipath-agents-001", "status": "SUCCESS"}],
                "tasks_run": 1,
            }
        )
    )
    (suite_dir / "suite.json").write_text(
        json.dumps(
            {
                "rows_total": 1,
                "criterion_aggregates": [
                    {
                        "criterion_type": "skill_triggered",
                        "description": "uipath-agents activation",
                        "metrics": {"recall.yes": 1.0},
                        "details": {"per_label": [{"label": "yes", "support": 20}]},
                    }
                ],
            }
        )
    )
    case_dir = suite_dir / "uipath-agents-001" / "00"
    case_dir.mkdir(parents=True)
    (case_dir / "task.json").write_text(
        json.dumps(
            {
                "task_config": {"resolved": {"initial_prompt": "Build a UiPath agent"}},
                "success_criteria_results": [
                    {
                        "criterion_type": "skill_triggered",
                        "description": "uipath-agents activation",
                        "observed_label": "yes",
                        "expected_label": "yes",
                    }
                ],
            }
        )
    )

    _finalize_activation_run(act_dir, skills_dir)

    out = json.loads((act_dir / "run.json").read_text())
    row = out["task_results"][0]
    assert (row["prompt"], row["expected_skill"], row["triggered_skill"]) == (
        "Build a UiPath agent",
        "uipath-agents",
        "uipath-agents",
    )
    act = out["activation"]
    assert (act["n_cases"], act["n_skills_sampled"], act["denominator"], act["score"]) == (1, 1, 2, 0.5)
