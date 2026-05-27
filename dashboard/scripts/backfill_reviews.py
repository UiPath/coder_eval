"""Backfill per-task review.json across historical runs in blob storage.

For each run id in the configured blob container, the script:
  1. Pulls the run dir to a temp path (the files the review skill needs).
  2. If `review_index.json` doesn't already exist (or `--force`), invokes
     :func:`dashboard.review.generate_reviews`.
  3. Uploads the new review_index.json plus every per-task review.json blob.

Idempotent at the blob layer: skips runs that already have a
``review_index.json`` in blob unless ``--force`` is passed. ``--dry-run``
prints planned actions without subprocess or upload.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import click

from dashboard.config import Config
from dashboard.review import generate_reviews


def _auth_args(account_key: str) -> list[str]:
    """argv-only auth flags. The secret never lands here — when key auth is
    configured, the value is exported via AZURE_STORAGE_KEY (see ``_auth_env``)
    so it doesn't appear in /proc/<pid>/cmdline. Mirrors pull-run.sh hardening.
    """
    return ["--auth-mode", "key" if account_key else "login"]


def _auth_env(account_key: str) -> dict[str, str]:
    """Process env with AZURE_STORAGE_KEY set when key auth is configured.

    `az storage blob ...` auto-picks up AZURE_STORAGE_KEY when --auth-mode is
    `key`, so the secret stays out of argv.
    """
    env = dict(os.environ)
    if account_key:
        env["AZURE_STORAGE_KEY"] = account_key
    return env


def _run_az(cmd: list[str], account_key: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run an `az` command with the storage key passed via env, never argv."""
    return subprocess.run(cmd, env=_auth_env(account_key), **kwargs)  # type: ignore[arg-type]


def list_run_ids(account: str, container: str, account_key: str = "") -> list[str]:
    cmd = [
        "az",
        "storage",
        "blob",
        "list",
        "--container-name",
        container,
        "--account-name",
        account,
        "--num-results",
        "*",
        "--query",
        "[].name",
        "-o",
        "tsv",
        *_auth_args(account_key),
    ]
    proc = _run_az(cmd, account_key, capture_output=True, text=True, check=True)
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        if "/" not in line:
            continue
        prefix = line.split("/", 1)[0]
        # YYYY-MM-DD_HH-MM-SS — same shape pull-run.sh validates.
        if len(prefix) == 19 and prefix[10] == "_":
            seen.add(prefix)
    return sorted(seen)


def blob_has_review_index(run_id: str, account: str, container: str, account_key: str = "") -> bool:
    cmd = [
        "az",
        "storage",
        "blob",
        "exists",
        "--container-name",
        container,
        "--account-name",
        account,
        "--name",
        f"{run_id}/review_index.json",
        "--query",
        "exists",
        "-o",
        "tsv",
        *_auth_args(account_key),
    ]
    proc = _run_az(cmd, account_key, capture_output=True, text=True, check=True)
    return proc.stdout.strip().lower() == "true"


def download_run(
    run_id: str,
    dest: Path,
    account: str,
    container: str,
    account_key: str = "",
) -> None:
    """Download a run's metadata + per-task task.json files to ``dest``."""
    cmd = [
        "az",
        "storage",
        "blob",
        "download-batch",
        "--source",
        container,
        "--destination",
        str(dest),
        "--pattern",
        f"{run_id}/*",
        "--account-name",
        account,
        "--no-progress",
        *_auth_args(account_key),
    ]
    _run_az(cmd, account_key, check=True)


def upload_review_artifacts(
    run_id: str,
    run_path: Path,
    account: str,
    container: str,
    account_key: str = "",
) -> int:
    """Upload review_index.json + every per-task review.json blob. Returns file count."""
    files: list[Path] = []
    index = run_path / "review_index.json"
    if index.exists():
        files.append(index)
    files.extend(sorted(run_path.glob("*/*/*/review.json")))
    for f in files:
        rel = f.relative_to(run_path).as_posix()
        cmd = [
            "az",
            "storage",
            "blob",
            "upload",
            "--container-name",
            container,
            "--account-name",
            account,
            "--name",
            f"{run_id}/{rel}",
            "--file",
            str(f),
            "--overwrite",
            *_auth_args(account_key),
        ]
        _run_az(cmd, account_key, check=True)
    return len(files)


def backfill_run(
    run_id: str,
    cfg: Config,
    *,
    force: bool,
    dry_run: bool,
) -> str:
    """Backfill a single run. Returns a one-line status message."""
    if (
        not dry_run
        and not force
        and blob_has_review_index(run_id, cfg.azure_storage_account, cfg.azure_blob_container, cfg.azure_storage_key)
    ):
        return f"{run_id}: skip (already reviewed)"

    if dry_run:
        return f"{run_id}: would review + upload"

    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        download_run(
            run_id,
            stage,
            cfg.azure_storage_account,
            cfg.azure_blob_container,
            cfg.azure_storage_key,
        )
        run_path = stage / run_id
        if not run_path.exists():
            return f"{run_id}: skip (download produced no files)"
        try:
            generate_reviews(run_path)
        except Exception as exc:
            return f"{run_id}: error during review: {exc}"
        try:
            n = upload_review_artifacts(
                run_id,
                run_path,
                cfg.azure_storage_account,
                cfg.azure_blob_container,
                cfg.azure_storage_key,
            )
        except Exception as exc:
            return f"{run_id}: error during upload: {exc}"
        if n == 0:
            return f"{run_id}: skip (no review artifacts to upload)"
    return f"{run_id}: backfilled OK ({n} review file(s) uploaded)"


@click.command()
@click.option(
    "--force",
    is_flag=True,
    help="Regenerate review.json files even when review_index.json exists in blob.",
)
@click.option("--dry-run", is_flag=True, help="Print planned actions without writes.")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after this many runs (useful for cost-bounded test runs).",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt before kicking off classification.",
)
def main(force: bool, dry_run: bool, limit: int | None, yes: bool) -> None:
    cfg = Config()
    run_ids = list_run_ids(cfg.azure_storage_account, cfg.azure_blob_container, cfg.azure_storage_key)
    if limit is not None:
        # list_run_ids returns ascending (oldest first); the newest runs are
        # the actionable ones for cost-bounded test backfills.
        run_ids = run_ids[-limit:]

    print(f"Found {len(run_ids)} run(s) in container '{cfg.azure_blob_container}'.")
    if not run_ids:
        return

    if dry_run:
        print("(dry-run mode — no subprocess or upload will be invoked)")
    else:
        approx = len(run_ids)
        print(
            f"Each backfill spawns one Claude review subprocess (~$0.05-$0.20/run).\n"
            f"  Estimated upper bound: ${approx * 0.20:.2f}"
        )
        if not yes:
            click.confirm("Proceed?", abort=True)

    failures = 0
    for run_id in run_ids:
        msg = backfill_run(run_id, cfg, force=force, dry_run=dry_run)
        print(msg)
        if "error" in msg:
            failures += 1

    if failures:
        print(f"\nDone with {failures} failure(s).", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
