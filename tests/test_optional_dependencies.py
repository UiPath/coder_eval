"""Tests proving the framework does not depend on ``uipath_llmgw_client``.

The LLM Gateway judge transport and the LLMGW-backed rephrase path were removed;
``uipath-llmgw-client`` is no longer a dependency (the ``[uipath]`` extra now
ships only the ``uipath`` SDK). This test blocks ``uipath_llmgw_client`` at the
import layer in a fresh subprocess and asserts the framework still imports
end-to-end — including ``init_criteria(validate=True)`` — so nothing reaches for
the removed client at load or registration time.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def test_framework_imports_succeed_without_llmgw_client_subprocess(tmp_path) -> None:
    """A fresh Python process imports the framework + inits criteria with
    ``uipath_llmgw_client`` blocked.

    Uses a subprocess so we don't have to undo module-cache state from this
    pytest process (where the package may still be installed in the dev env).
    """
    script = textwrap.dedent(
        """
        import builtins
        import importlib.util
        import sys

        # Block uipath_llmgw_client at both find_spec and import layers.
        sys.modules.pop("uipath_llmgw_client", None)
        real_find_spec = importlib.util.find_spec

        def fake_find_spec(name, *a, **kw):
            if name == "uipath_llmgw_client":
                return None
            return real_find_spec(name, *a, **kw)

        importlib.util.find_spec = fake_find_spec

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "uipath_llmgw_client":
                raise ImportError("simulated missing package")
            return real_import(name, *a, **kw)

        builtins.__import__ = fake_import

        # The actual import-surface checks.
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
