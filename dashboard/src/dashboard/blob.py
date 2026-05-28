"""Upload run results to Azure Blob Storage."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Patterns applied to the blob name (relative to the run dir).
# Files matching any pattern are skipped — reconstructible build artifacts,
# local state, or secrets that bloat upload without adding evalboard value.
# Keep in sync with ARTIFACT_EXCLUDE_PATTERNS in evalboard/lib/runs.ts — the
# upload filter and the display filter must agree on what counts as noise.
_EXCLUDE_PATTERNS = [
    "*/.venv/*",
    "*/__pycache__/*",
    "*.pyc",
    "*/bin/*",
    "*/obj/*",
    "*.dll",
    "*.nupkg",
    "*.pdb",
    "*/node_modules/*",
    "*/.npm-prefix/*",
    "*.lock",  # uv.lock / Cargo.lock / poetry.lock — large, reconstructible
    "*.db",  # local sqlite state, not a deliverable
    "*.db-wal",
    "*.db-shm",
    "*.env",  # zero display value + secret-leak risk in a shared dashboard
]

_MAX_WORKERS = 32


def _excluded(blob_name: str) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(blob_name, pat) for pat in _EXCLUDE_PATTERNS)


def upload_run(
    run_path: Path,
    run_id: str,
    storage_account: str,
    container: str,
    account_key: str = "",
) -> None:
    from azure.storage.blob import BlobServiceClient

    url = f"https://{storage_account}.blob.core.windows.net"
    if account_key:
        client = BlobServiceClient(url, credential=account_key)
    else:
        from azure.identity import DefaultAzureCredential

        client = BlobServiceClient(url, credential=DefaultAzureCredential())

    container_client = client.get_container_client(container)

    files = [f for f in run_path.rglob("*") if f.is_file()]

    def _upload(f: Path) -> bool:
        rel = f.relative_to(run_path).as_posix()
        if _excluded(rel):
            return False
        blob_name = f"{run_id}/{rel}"
        with f.open("rb") as data:
            container_client.upload_blob(blob_name, data, overwrite=True)
        return True

    uploaded = skipped = errors = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_upload, f): f for f in files}
        for future in as_completed(futures):
            try:
                if future.result():
                    uploaded += 1
                else:
                    skipped += 1
            except Exception as exc:
                errors += 1
                print(f"  WARNING: upload failed for {futures[future]}: {exc}")

    print(f"Uploaded {uploaded} blobs ({skipped} skipped, {errors} errors)")
