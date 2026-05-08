"""Build and install UiPath CLI from source."""

import subprocess
from pathlib import Path


def build_cli(cli_dir: Path) -> bool:
    """Pull & build UiPath CLI. Returns True on success, False on failure."""
    try:
        subprocess.run(["git", "checkout", "main"], cwd=cli_dir, check=True)
        subprocess.run(["git", "pull"], cwd=cli_dir, check=True)
        subprocess.run(["bun", "install"], cwd=cli_dir, check=True)
        subprocess.run(["bun", "run", "build"], cwd=cli_dir, check=True)
        # cli has flipped between these names (governance rollout PR #1525 →
        # revert #1883); try the canonical name first, fall back to legacy.
        if subprocess.run(["bun", "run", "dev:install-cli"], cwd=cli_dir).returncode != 0:
            subprocess.run(["bun", "run", "dev:cli:install"], cwd=cli_dir, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"WARNING: CLI build step failed: {e}")
        return False
