"""Upload run results to Azure Blob Storage."""

import subprocess
from pathlib import Path


def upload_run(
    run_path: Path,
    run_id: str,
    storage_account: str,
    container: str,
    account_key: str = "",
) -> None:
    cmd = [
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
        "--overwrite",
    ]
    if account_key:
        cmd.extend(["--auth-mode", "key", "--account-key", account_key])
    else:
        cmd.extend(["--auth-mode", "login"])
    subprocess.run(cmd, check=True)
