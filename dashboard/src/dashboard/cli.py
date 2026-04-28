"""CLI entry point: `dashboard <command>`."""

from dataclasses import dataclass
from datetime import UTC, datetime

import click

from .config import Config


@dataclass(frozen=True)
class Suite:
    name: str
    task_patterns: list[str]
    tags: str | None = None
    concurrency: int | None = None
    experiment: str | None = None
    uip_login: bool = False
    uip_tenant: str | None = None
    env: dict[str, str] | None = None


SUITES: list[Suite] = [
    Suite(name="smoke", task_patterns=["tasks/*.yaml"], tags="smoke"),
    Suite(name="flow-init", task_patterns=["tasks/uipath_flow/init_*.yaml"], tags="flow"),
    Suite(
        name="flow",
        task_patterns=["tasks/uipath_flow/*.yaml"],
        concurrency=2,
        experiment="experiments/flow-folder-hint.yaml",
        uip_login=True,
    ),
]


def _build_skills_suite(skills_dir: str) -> Suite:
    """Build a skills suite with absolute paths resolved from skills_dir."""
    return Suite(
        name="skills",
        task_patterns=[f"{skills_dir}/tests/tasks/**/*.yaml"],
        experiment=f"{skills_dir}/tests/experiments/e2e.yaml",
        uip_login=True,
        env={"SKILLS_REPO_PATH": skills_dir},
    )


@click.group()
def cli() -> None:
    """Coder-eval dashboard: run tests, upload results, ingest into ADX."""


@cli.command()
@click.option("--model", default="claude-sonnet-4-6", help="Model to evaluate.")
@click.option("--tags", default=None, help="Task tag filter (overrides suite defaults).")
@click.option("--suite", default=None, help="Run only the named suite (e.g. 'skills', 'smoke', 'flow-init', 'flow').")
@click.option("--skip-build", is_flag=True, help="Skip UiPath CLI build step.")
@click.option("--skip-pull", is_flag=True, help="Skip git pull steps.")
@click.option("--skip-analysis", is_flag=True, help="Skip AI analysis generation.")
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
    skip_build: bool,
    skip_pull: bool,
    skip_analysis: bool,
    skip_login: bool,
    verbose: bool,
    backend: str | None,
    concurrency: int | None,
) -> None:
    """Full pipeline: pull repos, build CLI, run tests, upload, ingest."""
    from .blob import upload_run
    from .build import build_cli
    from .ingest import ingest_run
    from .run import pull_coder_eval, run_tests, uip_login

    cfg = Config()
    print(f"=== Run started at {datetime.now(UTC).isoformat()} ===")

    # 1. Build UiPath CLI
    if skip_build:
        print("Skipping CLI build (--skip-build)")
    elif build_cli(cfg.cli_dir):
        print("CLI build succeeded")
    else:
        print("WARNING: CLI build failed, continuing with existing binary")

    # 2. Pull coder_eval
    if skip_pull:
        print("Skipping git pull (--skip-pull)")
    else:
        pull_coder_eval()

    # 3. Determine which suites to run
    all_suites = [_build_skills_suite(str(cfg.skills_dir)), *SUITES]
    if suite:
        suites_to_run = [s for s in all_suites if s.name == suite]
        if not suites_to_run:
            names = ", ".join(s.name for s in all_suites)
            raise click.BadParameter(f"Unknown suite '{suite}'. Available: {names}")
    else:
        suites_to_run = all_suites

    # 3b. Validate UiPath credentials and tenant eagerly (fail-fast before any suite runs)
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

    # 4. Run each suite → login (if needed) → run → analyze → upload → ingest
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
        # Tell coder_eval where the sibling repos actually live so it captures their SHAs in env_info.
        component_env = {
            "CODER_EVAL_SKILLS_DIR": str(cfg.skills_dir),
            "CODER_EVAL_CLI_DIR": str(cfg.cli_dir),
        }
        suite_env = {**component_env, **(s.env or {})}
        latest_run = run_tests(
            model=model,
            tags=suite_tags,
            task_patterns=s.task_patterns,
            concurrency=suite_concurrency,
            experiment=s.experiment,
            extra_env=suite_env,
            verbose=verbose,
            backend=backend,
        )
        run_id = latest_run.name
        print(f"Run completed: {run_id}")

        # Generate analysis before upload so analysis.md is included in blob
        if skip_analysis:
            print("Skipping analysis (--skip-analysis)")
        else:
            try:
                from .analysis import generate_analysis

                generate_analysis(latest_run)
                print("Analysis generated")
            except Exception:
                import traceback

                traceback.print_exc()
                print("WARNING: Analysis generation failed (see traceback above)")

        upload_run(latest_run, run_id, cfg.azure_storage_account, cfg.azure_blob_container)
        print("Blob upload complete")

        ingest_run(str(latest_run), cfg.adx_cluster_uri, cfg.adx_database)

    print(f"\n=== Run completed at {datetime.now(UTC).isoformat()} ===")


@cli.command()
@click.argument("run_dir", type=click.Path(exists=True))
def ingest(run_dir: str) -> None:
    """Ingest a run directory into ADX."""
    from .ingest import ingest_run

    cfg = Config()
    ingest_run(run_dir, cfg.adx_cluster_uri, cfg.adx_database)


@cli.command()
@click.argument("run_dir", type=click.Path(exists=True))
def upload(run_dir: str) -> None:
    """Upload a run directory to Azure Blob Storage."""
    from pathlib import Path

    from .blob import upload_run

    cfg = Config()
    run_path = Path(run_dir).resolve()
    run_id = run_path.name
    upload_run(run_path, run_id, cfg.azure_storage_account, cfg.azure_blob_container)
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


@cli.command()
@click.option("--drop", is_flag=True, help="Drop tables only (no recreate).")
def schema(drop: bool) -> None:
    """Drop and recreate ADX tables."""
    from .schema import create_tables, drop_tables

    cfg = Config()
    print("Dropping existing tables...")
    drop_tables(cfg.adx_cluster_uri, cfg.adx_database)
    if not drop:
        print("Creating tables with new schema...")
        create_tables(cfg.adx_cluster_uri, cfg.adx_database)
        print("Done. Schema is ready.")
    else:
        print("Done. Tables dropped (--drop mode).")
