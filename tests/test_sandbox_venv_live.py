"""Live docker check that the sandbox venv does not shadow a task image's packages.

Gated: needs a real docker daemon and the `coder-eval-agent` base image.

This is the one thing the unit tests cannot prove. `tests/test_sandbox.py` asserts
the venv is created with system site packages by reading `pyvenv.cfg`, which is a
property of the flag, not of the outcome. The outcome only exists inside an image
that provisions packages GLOBALLY — the shape every task image has (the framework
image installs with `uv pip install --system`; skillsbench task images do
`RUN pip install ...`). There, an isolated sandbox venv split the toolchain:
`python` resolved to the venv and could not import the image's packages, while
`pip` fell through to the image's global pip and reported them present.

Measured against this test's own scenario:

    main (isolated venv)             python -c "import pydantic"  ->  exit 1
    with --system-site-packages      python -c "import pydantic"  ->  exit 0

`pydantic` is a coder_eval runtime dependency, so the base image already has it
installed globally — no build and no network are needed to reproduce the shape.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


BASE_IMAGE = "coder-eval-agent:latest"
REPO_SRC = Path(__file__).resolve().parent.parent / "src"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]


def _docker_daemon_up() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _image_present(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30).returncode == 0


PROBE = textwrap.dedent(
    """
    from coder_eval.models import SandboxConfig
    from coder_eval.sandbox import Sandbox

    sandbox = Sandbox(SandboxConfig(driver="tempdir"), task_id="venv_probe")
    try:
        sandbox_dir = sandbox.setup()
        import_rc, _, _ = sandbox.run_command('python -c "import pydantic"')
        _, prefix, _ = sandbox.run_command('python -c "import sys; print(sys.prefix)"')
        print(f"IMPORT_RC={import_rc}")
        print(f"PREFIX={prefix.strip()}")
        print(f"VENV={sandbox.venv_dir}")
    finally:
        sandbox.cleanup()
    """
)


def test_criteria_can_import_the_images_global_packages() -> None:
    """A `run_command` criterion must see what the task image installed globally.

    Mounts this checkout's `src/` over the image's copy so the assertion is about
    the code under test, not whatever coder_eval version the image was built with.
    """
    if not _docker_daemon_up():
        pytest.skip("docker daemon not running")
    if not _image_present(BASE_IMAGE):
        pytest.skip(f"{BASE_IMAGE} not built locally")

    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{REPO_SRC}:/opt/coder_eval/src:ro",
            "--entrypoint",
            "python3",
            BASE_IMAGE,
            "-c",
            PROBE,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)

    assert out["IMPORT_RC"] == "0", (
        "a criterion could not import a package the image installed globally -- "
        f"the sandbox venv is shadowing the image interpreter again:\n{proc.stdout}"
    )
    # Isolation still holds: installs land in the sandbox, not the image.
    assert out["PREFIX"] == out["VENV"], f"criterion did not run under the sandbox venv:\n{proc.stdout}"
