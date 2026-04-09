"""Upload run results to Azure Blob Storage."""

import subprocess
from pathlib import Path


def upload_run(
    run_path: Path,
    run_id: str,
    storage_account: str,
    container: str,
) -> None:
    subprocess.run(
        [
            "az",
            "storage",
            "blob",
            "upload-batch",
            "--source",
            str(run_path),
            "--destination",
            container,
            "--destination-path",
            run_id,
            "--account-name",
            storage_account,
            "--auth-mode",
            "login",
            "--overwrite",
        ],
        check=True,
    )
