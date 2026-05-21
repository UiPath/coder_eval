"""Tests proving that ``uipath`` / ``uipath_llmgw_client`` are truly optional.

These tests simulate the package being absent from the interpreter (via
``monkeypatch`` on ``importlib.util.find_spec`` and ``builtins.__import__``)
and assert:

- ``is_llmgw_client_installed()`` returns ``False`` (and never raises).
- ``get_llmgw_chat_model()`` raises ``RuntimeError`` whose message contains the
  ``coder-eval[uipath]`` install hint.
- A subprocess can import the framework end-to-end (including
  ``init_criteria(validate=True)``) when ``uipath_llmgw_client`` is missing.

Together with the package-detection branch in
``models.routing._resolve_direct_judge_transport`` (covered in
``test_config_precedence.py``), these exercise the contract that the
optional ``[uipath]`` extra can be omitted without breaking framework
imports, the criterion registry, or non-LLMGW judge paths.
"""

from __future__ import annotations

import builtins
import importlib.util
import os
import subprocess
import sys
import textwrap

import pytest


INSTALL_HINT_SUBSTRING = "coder-eval[uipath]"


def _block_llmgw_client_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``uipath_llmgw_client`` look uninstalled to both find_spec and import."""
    monkeypatch.delitem(sys.modules, "uipath_llmgw_client", raising=False)

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "uipath_llmgw_client":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "uipath_llmgw_client":
            raise ImportError("simulated missing package")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_is_llmgw_client_installed_true_when_find_spec_returns_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When find_spec returns a non-None spec, the helper reports installed."""
    real_spec = importlib.util.spec_from_loader("uipath_llmgw_client_probe", loader=None)
    assert real_spec is not None  # sanity

    # Capture the real function BEFORE monkeypatching — otherwise the fallback
    # would call the patched closure itself and recurse on any non-target name.
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "uipath_llmgw_client":
            return real_spec
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    from coder_eval.evaluation.llmgw import is_llmgw_client_installed

    assert is_llmgw_client_installed() is True


def test_is_llmgw_client_installed_false_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_llmgw_client_imports(monkeypatch)

    from coder_eval.evaluation.llmgw import is_llmgw_client_installed

    assert is_llmgw_client_installed() is False


def test_is_llmgw_client_installed_false_on_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partially-installed package can make find_spec raise; treat as missing."""
    # Capture the real function BEFORE monkeypatching to avoid recursing into
    # the closure for any non-target name.
    real_find_spec = importlib.util.find_spec

    def raising_find_spec(name, *args, **kwargs):
        if name == "uipath_llmgw_client":
            raise ValueError("broken __spec__")
        return real_find_spec(name, *args, **kwargs)

    # We need to patch the real attribute, not a re-imported reference.
    monkeypatch.setattr(importlib.util, "find_spec", raising_find_spec)

    # Reload the module so the helper picks up the patched find_spec at call time.
    from coder_eval.evaluation.llmgw import is_llmgw_client_installed

    assert is_llmgw_client_installed() is False


def test_get_llmgw_chat_model_raises_install_hint_without_package(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_llmgw_client_imports(monkeypatch)

    from coder_eval.evaluation.llmgw import get_llmgw_chat_model

    with pytest.raises(RuntimeError) as exc_info:
        get_llmgw_chat_model(model="anthropic.foo", temperature=0.0, max_tokens=100)

    assert INSTALL_HINT_SUBSTRING in str(exc_info.value)


def test_framework_imports_succeed_without_llmgw_client_subprocess(tmp_path) -> None:
    """End-to-end check: a fresh Python process can import the framework + init criteria
    even when ``uipath_llmgw_client`` is unavailable.

    Uses a subprocess so we don't have to undo any module-cache state from this
    pytest process (where the package may legitimately be installed via the
    dev environment).
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
        import coder_eval.evaluation.llmgw as llmgw
        import coder_eval.orchestration.rephrase  # noqa: F401
        from coder_eval.criteria import init_criteria, CriterionRegistry

        assert llmgw.is_llmgw_client_installed() is False, "helper must report False when blocked"

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
