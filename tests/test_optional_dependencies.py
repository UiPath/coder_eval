"""Proves the framework imports and registers criteria without the optional ``[uipath]`` extra.

Run inside the `verify-noextra` venv (and the `no-uipath-extra` CI job), where the
`uipath` SDK is deliberately not installed: this asserts the import surface and the
criterion registry stay intact end-to-end — including ``init_criteria(validate=True)``
— so a base `pip install coder-eval` is functional on its own.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def test_framework_imports_and_registers_criteria_subprocess(tmp_path) -> None:
    """A fresh Python process imports the framework and inits the criteria registry.

    Uses a subprocess so the check is independent of this pytest process's
    module-cache state.
    """
    script = textwrap.dedent(
        """
        import coder_eval  # noqa: F401
        import coder_eval.models  # noqa: F401
        from coder_eval.criteria import init_criteria, CriterionRegistry

        init_criteria(validate=True)
        assert "llm_judge" in CriterionRegistry.list_types(), "llm_judge must stay registered"
        assert "uipath_eval" in CriterionRegistry.list_types(), "uipath_eval must stay registered"

        print("ok")
        """
    )
    script_path = tmp_path / "probe.py"
    script_path.write_text(script)

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    assert result.stdout.strip().endswith("ok")
