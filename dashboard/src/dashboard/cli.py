"""CLI entry point: `dashboard <command>`."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from .config import Config


@dataclass(frozen=True)
class Suite:
    name: str
    task_patterns: list[str]
    # Glob patterns whose matches are removed from `task_patterns` results.
    # Use to carve out subdirs that belong to a different (opt-in) suite.
    exclude_patterns: list[str] | None = None
    tags: str | None = None
    concurrency: int | None = None
    experiment: str | None = None
    uip_login: bool = False
    uip_tenant: str | None = None
    env: dict[str, str] | None = None
    # Stratified-sample cap forwarded to coder-eval as --sample-per-stratum:
    # keep up to N rows per stratum (expected_skill) for dataset-backed suites.
    # Lets the runner cap a dataset (e.g. activation) without editing its task YAML.
    sample_per_stratum: int | None = None
    # When False, the suite is opt-in (only runs via `--suite <name>`) and
    # is excluded from the default daily pipeline.
    default: bool = True
    # The skill-activation suite is a per-skill classifier: it runs into a NESTED
    # sub-run dir (<run>/activation/) with its own self-contained run.json, so its
    # classifier rows never touch the skills run.json at all — the skills run is
    # exactly what coder-eval wrote. Every run-level metric stays skills-only by
    # construction; the activation rows surface only on the /runs/<id>/activation
    # page, which reads the nested run.json.
    is_activation: bool = False


SUITES: list[Suite] = [
    # `smoke-pass` (not the umbrella `smoke`): excludes tasks/smoke_negative_path.yaml,
    # which is tagged [smoke, smoke-fail] and is intentionally unsatisfiable. Including
    # it here would make the daily smoke pipeline report a guaranteed failure forever.
    # default=False: opt-in only (`--suite smoke`). These are coder-eval's own
    # self-test tasks, unrelated to skills — keep them out of the merged nightly run.
    Suite(name="smoke", task_patterns=["tasks/*.yaml"], tags="smoke-pass", default=False),
]


def _build_skills_suite(skills_dir: str) -> Suite:
    """Build a skills suite with absolute paths resolved from skills_dir.

    The activation/ benchmark dir is carved out via `exclude_patterns` —
    it lives in its own opt-in suite (see ``_build_activation_suite``).
    """
    return Suite(
        name="skills",
        task_patterns=[f"{skills_dir}/tests/tasks/**/*.yaml"],
        exclude_patterns=[f"{skills_dir}/tests/tasks/activation/**/*.yaml"],
        experiment=f"{skills_dir}/tests/experiments/nightly.yaml",
        concurrency=20,
        uip_login=True,
        env={"SKILLS_REPO_PATH": skills_dir},
    )


def _build_activation_suite(skills_dir: str) -> Suite:
    """Skill-activation benchmark — runs nightly alongside the skills suite.

    Classification eval over the prompt dataset. ``sample_per_stratum=20`` caps it
    at a 20-per-skill stratified random sample (~371 rows) via coder-eval's
    --sample-per-stratum flag — the cap lives here, not in the skills-repo task
    YAML. Cheap enough to run every night. Runs into a NESTED sub-run dir under the
    skills run (<run>/activation/) with its own self-contained run.json (see
    ``is_activation`` + ``_finalize_activation_run``): enriched case rows in
    task_results plus the per-skill rollup in ["activation"]. It rides along in the
    one upload but never touches the skills run.json. Tempdir experiment, max_turns=1.
    """
    return Suite(
        name="activation",
        task_patterns=[f"{skills_dir}/tests/tasks/activation/**/*.yaml"],
        experiment=f"{skills_dir}/tests/experiments/activation.yaml",
        concurrency=20,
        sample_per_stratum=20,
        default=True,
        is_activation=True,
        env={"SKILLS_REPO_PATH": skills_dir},
    )


def _load_skill_catalog(skills_dir: Path) -> list[str]:
    """All skill names from the skills repo manifest — the activation denominator.

    Every skill the platform ships should eventually have activation prompts, so
    the catalog (not just the covered skills) is the denominator: a skill with no
    prompts is a coverage gap that scores 0.
    """
    data = json.loads((skills_dir / "assets" / "skill-status.json").read_text(encoding="utf-8"))
    skills = data.get("skills", data)
    return sorted(skills.keys() if isinstance(skills, dict) else skills)


def compute_activation_rollup(suite_json: dict[str, Any], catalog: list[str], min_prompts: int = 20) -> dict[str, Any]:
    """Build the run-level activation summary the evalboard + Slack read.

    Score = mean over the FULL skill catalog of each skill's recall.yes, but a
    skill counts 0 unless it ran a full sample (>= ``min_prompts`` positive
    prompts). So coverage gaps — skills with too few prompts, or none — drag the
    score down. ``per_skill`` lists every catalog skill, including 0-coverage ones,
    for the activation page's per-skill table.
    """
    covered: dict[str, tuple[float | None, int]] = {}
    for a in suite_json.get("criterion_aggregates") or []:
        if a.get("criterion_type") != "skill_triggered":
            continue
        skill = (a.get("description") or "").removesuffix(" activation").strip()
        if not skill:
            continue
        recall_yes = (a.get("metrics") or {}).get("recall.yes")
        n_yes = next(
            (
                pl.get("support") or 0
                for pl in (a.get("details") or {}).get("per_label") or []
                if pl.get("label") == "yes"
            ),
            0,
        )
        covered[skill] = (recall_yes, n_yes)

    per_skill: list[dict[str, Any]] = []
    for skill in catalog:
        recall_yes, n_yes = covered.get(skill, (None, 0))
        sampled = n_yes >= min_prompts
        contributes = recall_yes if (sampled and recall_yes is not None) else 0.0
        per_skill.append(
            {
                "skill": skill,
                "recall_yes": recall_yes,
                "n_prompts": n_yes,
                "sampled": sampled,
                "contributes": contributes,
            }
        )
    score = sum(p["contributes"] for p in per_skill) / len(catalog) if catalog else 0.0
    return {
        "score": score,
        "denominator": len(catalog),
        "min_prompts": min_prompts,
        "n_skills_sampled": sum(1 for p in per_skill if p["sampled"]),
        "n_cases": suite_json.get("rows_total"),
        "per_skill": per_skill,
    }


def _activation_case_facts(criteria: list[dict[str, Any]]) -> tuple[str, str | None]:
    """From one case's skill_triggered criteria, derive (expected_skill, triggered_skill).

    Each activation case stacks one ``skill_triggered`` criterion per catalog skill,
    carrying the per-skill observed/expected label. ``expected_skill`` is the one
    whose ``expected_label == "yes"`` ("" → a negative case, nothing should fire).
    ``triggered_skill`` is the skill(s) that actually fired (``observed_label ==
    "yes"``), sorted and comma-joined ("" → nothing fired). A mismatch between the
    two is a mistake (a miss, a false positive, or the wrong skill). Returns
    (expected_skill, None) when there are no skill_triggered criteria.
    """
    expected_skill = ""
    fired: list[str] = []
    seen = False
    for c in criteria:
        if c.get("criterion_type") != "skill_triggered":
            continue
        seen = True
        skill = (c.get("description") or "").removesuffix(" activation").strip()
        if (c.get("observed_label") or "").strip().lower() == "yes":
            fired.append(skill)
        if (c.get("expected_label") or "").strip().lower() == "yes":
            expected_skill = skill
    if not seen:
        return "", None
    return expected_skill, ", ".join(sorted(fired))


def _enrich_activation_tasks(rows: list[dict[str, Any]], suite_dir: Path) -> list[dict[str, Any]]:
    """Fold prompt / expected_skill / triggered onto each activation case row.

    The activation sub-run's ``task_results`` are a slim per-row projection without
    the prompt text or the per-skill verdicts. The /runs/<id>/activation cases table
    wants the prompt, the skill the prompt targets (expected_skill) and the skill
    that actually fired (triggered_skill); all live in each case's per-iteration
    ``task.json``. Read it once per row and attach them.

    Best-effort: a row whose task.json is missing/unreadable keeps a row_id-derived
    expected_skill and null prompt/triggered_skill, so re-processing an old run (no
    per-case dirs) never aborts.
    """
    enriched: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        row_id = (r.get("task_id") or "").rsplit("/", 1)[-1]
        # Fallbacks for when task.json is absent: "uipath-agents-003" -> "uipath-agents",
        # "negative-003" -> "" (no skill expected).
        r.setdefault("prompt", None)
        r["expected_skill"] = "" if row_id.startswith("negative") else re.sub(r"-\d+$", "", row_id)
        r["triggered_skill"] = None
        task_json = next(iter(suite_dir.glob(f"{row_id}/*/task.json")), None) if row_id else None
        if task_json is not None:
            try:
                data = json.loads(task_json.read_text(encoding="utf-8"))
                expected_skill, triggered_skill = _activation_case_facts(data.get("success_criteria_results") or [])
                if triggered_skill is not None:
                    r["expected_skill"] = expected_skill
                    r["triggered_skill"] = triggered_skill
                prompt = ((data.get("task_config") or {}).get("resolved") or {}).get("initial_prompt")
                if isinstance(prompt, str):
                    r["prompt"] = prompt
            except (OSError, ValueError):
                pass
        enriched.append(r)
    return enriched


def _finalize_activation_run(run_dir: Path, skills_dir: Path) -> None:
    """Enrich + roll up the nested activation sub-run's OWN run.json, in place.

    The activation suite runs into ``<run>/activation/`` with its own
    coder-eval run.json. This reads that file, folds prompt/expected_skill/
    triggered_skill onto each case row (so the cases table reads one file), and
    attaches the coverage-weighted per-skill rollup under run.json["activation"],
    then writes it back. ONLY this nested file is read or written — the skills
    run.json is never touched, so a failure here can't affect the skills result.

    Best-effort throughout: any step that fails is logged and skipped; the slim
    run.json the suite already wrote stays in place (cases still render, just
    un-enriched / without the rollup).
    """
    run_json = run_dir / "run.json"
    try:
        merged = json.loads(run_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"WARNING: activation sub-run {run_dir.name} has no readable run.json; skipping.")
        return

    # suite.json (the rollup source) + the per-case task.json dirs both live under
    # <run>/activation/<variant>/skill-activation/.
    suite_json = next(iter(run_dir.glob("*/skill-activation/suite.json")), None)
    if suite_json is not None:
        try:
            merged["task_results"] = _enrich_activation_tasks(merged.get("task_results") or [], suite_json.parent)
        except Exception:
            import traceback

            traceback.print_exc()
            print("WARNING: activation row enrichment failed; leaving the slim rows.")
        try:
            catalog = _load_skill_catalog(skills_dir)
            merged["activation"] = compute_activation_rollup(
                json.loads(suite_json.read_text(encoding="utf-8")), catalog
            )
            a = merged["activation"]
            print(
                f"Activation score: {a['score'] * 100:.0f}% "
                f"({a['n_skills_sampled']}/{a['denominator']} skills with >={a['min_prompts']} prompts)"
            )
        except (OSError, ValueError, KeyError):
            import traceback

            traceback.print_exc()
            print("WARNING: activation rollup computation failed (continuing without it).")

    try:
        run_json.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        import traceback

        traceback.print_exc()
        print("WARNING: failed to write enriched activation run.json (leaving the original in place).")


@click.group()
def cli() -> None:
    """Coder-eval dashboard: run tests, upload results to Azure Blob."""


@cli.command()
@click.option("--model", default="claude-sonnet-4-6", help="Model to evaluate.")
@click.option("--tags", default=None, help="Task tag filter (overrides suite defaults).")
@click.option(
    "--suite",
    default=None,
    help="Run only the named suite(s). Comma-separated for several (e.g. 'skills,activation').",
)
@click.option("--skip-pull", is_flag=True, help="Skip git pull steps.")
@click.option("--skip-analysis", is_flag=True, help="Skip AI analysis generation.")
@click.option("--skip-review", is_flag=True, help="Skip post-run task review (review.json) generation.")
@click.option(
    "--skip-login",
    is_flag=True,
    help="Skip UiPath CLI login. Use when already authenticated via 'uip login --interactive'.",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose (DEBUG) logging in coder-eval.")
@click.option("--backend", "-b", default=None, type=click.Choice(["direct", "bedrock", "proxy"]), help="API backend.")
@click.option(
    "--concurrency",
    "-j",
    default=None,
    type=click.IntRange(min=1),
    help="Max tasks to run concurrently per suite. Overrides the suite's built-in default.",
)
def run(
    model: str,
    tags: str | None,
    suite: str | None,
    skip_pull: bool,
    skip_analysis: bool,
    skip_review: bool,
    skip_login: bool,
    verbose: bool,
    backend: str | None,
    concurrency: int | None,
) -> None:
    """Full pipeline: pull repos, run tests, upload to blob.

    uip CLI is expected to already be on PATH (installed via npm by the caller).
    """
    from .blob import upload_run
    from .run import pull_coder_eval, run_tests, uip_login

    cfg = Config()
    print(f"=== Run started at {datetime.now(UTC).isoformat()} ===")

    # 1. Pull coder_eval
    if skip_pull:
        print("Skipping git pull (--skip-pull)")
    else:
        pull_coder_eval()

    # 2. Determine which suites to run. Opt-in suites (default=False) are
    # skipped on a bare `dashboard run` and only execute when named via --suite.
    # --suite accepts a comma-separated list so one invocation can run several
    # (the nightly runs "skills,activation"); each runs → analyzes → uploads
    # independently in the loop below.
    all_suites = [
        _build_skills_suite(str(cfg.skills_dir)),
        _build_activation_suite(str(cfg.skills_dir)),
        *SUITES,
    ]
    if suite:
        wanted = [name.strip() for name in suite.split(",") if name.strip()]
        suites_to_run = [s for s in all_suites if s.name in wanted]
        missing = [name for name in wanted if name not in {s.name for s in all_suites}]
        if missing:
            names = ", ".join(s.name for s in all_suites)
            raise click.BadParameter(f"Unknown suite(s) {missing}. Available: {names}")
    else:
        suites_to_run = [s for s in all_suites if s.default]

    # 2b. Validate UiPath credentials and tenant eagerly (fail-fast before any suite runs)
    login_suites = [s for s in suites_to_run if s.uip_login]
    if login_suites and not skip_login:
        if not all([cfg.uip_authority, cfg.uip_client_id, cfg.uip_client_secret]):
            raise click.UsageError(
                "UIP_AUTHORITY, UIP_CLIENT_ID, and UIP_CLIENT_SECRET must be set in .env"
                " for suites that require uip login."
                " Or run 'uip login --interactive' and pass --skip-login."
            )
        for s in login_suites:
            if not (s.uip_tenant or cfg.uip_tenant):
                raise click.UsageError(f"Suite '{s.name}' requires uip login but no tenant is configured.")

    # 3. Run the suites. Main (non-activation) suites run into the run dir; the
    # activation suite runs into a NESTED sub-run dir (<run>/activation/) with its
    # own self-contained run.json. So the skills run.json is exactly what coder-eval
    # wrote — no merge, no parked rows, nothing grafted on. suites_to_run always
    # orders main suites before activation, so the main run dir exists to nest under
    # (when activation runs alone it falls back to a normal top-level run).
    shared_run_dir: Path | None = None
    activation_run_dir: Path | None = None
    ran_any = False
    for s in suites_to_run:
        if s.uip_login and skip_login:
            print("Skipping UiPath CLI login (--skip-login); assuming already authenticated")
        elif s.uip_login:
            tenant = s.uip_tenant or cfg.uip_tenant
            print(f"Authenticating UiPath CLI (tenant={tenant})...")
            uip_login(
                authority=cfg.uip_authority,
                client_id=cfg.uip_client_id,
                client_secret=cfg.uip_client_secret,
                tenant=tenant,
                scope=cfg.uip_scope,
            )
            print("UiPath CLI login succeeded")
        suite_tags = tags if tags is not None else s.tags
        print(f"\n--- Suite: {s.name} (tags={suite_tags}) ---")

        suite_concurrency = concurrency if concurrency is not None else s.concurrency
        # Tell coder_eval where the skills repo lives so it captures its SHA in env_info.
        component_env = {
            "CODER_EVAL_SKILLS_DIR": str(cfg.skills_dir),
        }
        suite_env = {**component_env, **(s.env or {})}
        if s.is_activation and shared_run_dir is not None:
            target_dir: Path | None = shared_run_dir / "activation"
        else:
            target_dir = shared_run_dir
        latest_run = run_tests(
            model=model,
            tags=suite_tags,
            task_patterns=s.task_patterns,
            exclude_patterns=s.exclude_patterns,
            concurrency=suite_concurrency,
            experiment=s.experiment,
            extra_env=suite_env,
            verbose=verbose,
            sample_per_stratum=s.sample_per_stratum,
            backend=backend,
            run_dir=target_dir,
        )
        if s.is_activation and shared_run_dir is not None:
            activation_run_dir = latest_run
        elif shared_run_dir is None:
            shared_run_dir = latest_run
        if (latest_run / "run.json").exists():
            ran_any = True
            print(f"Suite '{s.name}' completed ({latest_run.name})")
        else:
            print(f"WARNING: suite '{s.name}' wrote no run.json — excluded from the run.")

    if shared_run_dir is None or not ran_any:
        print("No suite produced results; nothing to report or upload.")
        return

    run_id = shared_run_dir.name

    # 3b. Finalize the activation sub-run IN PLACE: enrich its case rows + attach
    # the per-skill rollup, touching ONLY <run>/activation/run.json. The skills
    # run.json is never opened here, so this step cannot degrade the skills result.
    if activation_run_dir is not None:
        _finalize_activation_run(activation_run_dir, cfg.skills_dir)

    # Log line from the (untouched) skills run.json, plus an activation note.
    try:
        summary = json.loads((shared_run_dir / "run.json").read_text(encoding="utf-8"))
        act_note = ""
        if activation_run_dir is not None:
            act = json.loads((activation_run_dir / "run.json").read_text(encoding="utf-8"))
            act_note = f" + {len(act.get('task_results') or [])} activation cases"
        print(
            f"\nRun '{run_id}': {summary.get('tasks_run', 0)} tasks "
            f"({summary.get('tasks_succeeded', 0)} ok / {summary.get('tasks_failed', 0)} fail / "
            f"{summary.get('tasks_error', 0)} err){act_note}"
        )
    except (OSError, ValueError):
        print(f"\nRun '{run_id}' completed.")

    # 4. ONE analysis → review → upload over the combined run.
    if skip_analysis:
        print("Skipping analysis (--skip-analysis)")
    else:
        try:
            from .analysis import generate_analysis

            generate_analysis(shared_run_dir)
            print("Analysis generated")
        except Exception:
            import traceback

            traceback.print_exc()
            print("WARNING: Analysis generation failed (see traceback above)")

    if skip_review:
        print("Skipping task reviews (--skip-review)")
    else:
        if skip_analysis:
            print(
                "WARNING: --skip-analysis is set but --skip-review is not; "
                "the review skill will run without analysis.md as a hint, "
                "which produces lower-quality summaries."
            )
        try:
            from .review import generate_reviews

            generate_reviews(shared_run_dir)
            print("Task reviews generated")
        except Exception:
            import traceback

            traceback.print_exc()
            print("WARNING: Task review generation failed (see traceback above)")

    try:
        upload_run(
            shared_run_dir,
            run_id,
            cfg.azure_storage_account,
            cfg.azure_blob_container,
            account_key=cfg.azure_storage_key,
        )
        print("Blob upload complete")
    except Exception:
        import traceback

        traceback.print_exc()
        print("WARNING: Blob upload failed — continuing without upload (see traceback above)")

    print(f"\n=== Run completed at {datetime.now(UTC).isoformat()} ===")


@cli.command()
@click.argument("run_dir", type=click.Path(exists=True))
@click.option("--title", default=None, help="Human-readable run title shown on the dashboard.")
@click.option("--description", default=None, help="Longer notes shown on the run page.")
@click.option(
    "--adhoc",
    is_flag=True,
    default=False,
    help="Mark as ad-hoc: excluded from the daily front-page metrics, listed in the Ad-hoc section.",
)
def upload(run_dir: str, title: str | None, description: str | None, adhoc: bool) -> None:
    """Upload a run directory to Azure Blob Storage.

    Pass --title / --description / --adhoc to attach run metadata, written as
    meta.json in the run dir before upload. With NO metadata flag, nothing
    extra is written — a daily-run re-upload (recovery) stays byte-for-byte
    as before, so the evalboard treats it exactly like any pipeline run.
    """
    import json
    from pathlib import Path

    from .blob import upload_run

    cfg = Config()
    run_path = Path(run_dir).resolve()
    run_id = run_path.name

    if title or description or adhoc:
        meta: dict[str, object] = {"adhoc": adhoc}
        if title:
            meta["title"] = title
        if description:
            meta["description"] = description
        (run_path / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        print(f"Wrote {run_path / 'meta.json'} ({', '.join(meta)})")

    upload_run(
        run_path,
        run_id,
        cfg.azure_storage_account,
        cfg.azure_blob_container,
        account_key=cfg.azure_storage_key,
    )
    print(f"Uploaded {run_id} to {cfg.azure_storage_account}/{cfg.azure_blob_container}")


@cli.command()
def config() -> None:
    """Show current configuration values."""
    cfg = Config()
    for field_name in cfg.model_fields:
        value = getattr(cfg, field_name)
        # Mask secrets
        if any(s in field_name for s in ("secret", "password", "token")):
            display = "***" if value not in (None, "") else "(not set)"
        else:
            display = "(not set)" if value in (None, "") else value
        print(f"  {field_name}: {display}")
