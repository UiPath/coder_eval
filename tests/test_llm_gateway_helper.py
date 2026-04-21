"""Tests for evaluation.llmgw shared helper."""

import builtins
import sys
from unittest.mock import MagicMock

import pytest


def test_helper_raises_when_package_missing(monkeypatch):
    """When uipath_llmgw_client cannot be imported, RuntimeError is raised with install instructions."""
    # Patch builtins.__import__ so the helper's nested import raises ImportError deterministically.
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "uipath_llmgw_client":
            raise ImportError("simulated missing package")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    from coder_eval.evaluation import llmgw

    with pytest.raises(RuntimeError) as exc_info:
        llmgw.get_llmgw_chat_model(model="anthropic.foo", temperature=0.0, max_tokens=100)

    assert "pip install uipath-llmgw-client" in str(exc_info.value)


def test_helper_passes_args_through(monkeypatch):
    """The helper forwards model/temperature/max_tokens plus llmgw_client_type='normalized'."""
    captured: dict[str, object] = {}

    def fake_get_langchain_chat_model(**kwargs):
        captured.update(kwargs)
        return MagicMock(name="chat_model")

    fake_module = MagicMock()
    fake_module.get_langchain_chat_model = fake_get_langchain_chat_model
    monkeypatch.setitem(sys.modules, "uipath_llmgw_client", fake_module)

    from coder_eval.evaluation.llmgw import get_llmgw_chat_model

    model = get_llmgw_chat_model(model="anthropic.claude-sonnet-4-6", temperature=0.3, max_tokens=500)

    assert model is not None
    assert captured == {
        "model": "anthropic.claude-sonnet-4-6",
        "llmgw_client_type": "normalized",
        "temperature": 0.3,
        "max_tokens": 500,
    }


def test_llmgw_available_returns_true_when_installed(monkeypatch):
    """llmgw_available() returns True when the package import succeeds."""
    fake_module = MagicMock()
    monkeypatch.setitem(sys.modules, "uipath_llmgw_client", fake_module)

    from coder_eval.evaluation.llmgw import llmgw_available

    assert llmgw_available() is True


def test_llmgw_available_returns_false_when_missing(monkeypatch):
    """llmgw_available() returns False when the package import raises."""
    monkeypatch.setitem(sys.modules, "uipath_llmgw_client", None)

    from coder_eval.evaluation.llmgw import llmgw_available

    assert llmgw_available() is False
