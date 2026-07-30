"""Shape guards for the LiteLLM proxy config (litellm/litellm-config.yaml).

A typo in this YAML (a mis-spelled key, a wrong callback path) fails silently at
proxy startup rather than in CI, so pin the structure the open-weight cost feature
relies on: `usage.include` + the vetted provider pins per model, and the callback
registration matching the real symbol in cost_logger.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _REPO_ROOT / "litellm" / "litellm-config.yaml"
_COST_LOGGER = _REPO_ROOT / "litellm" / "cost_logger.py"

_CALLBACK = "cost_logger.proxy_handler_instance"


def _load() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


class TestLitellmConfigShape:
    def test_cost_callback_registered(self):
        assert _load()["litellm_settings"]["callbacks"] == _CALLBACK

    def test_openrouter_models_capture_usage_and_pin_providers(self):
        # Derive the OpenRouter set from the config (not a hardcoded literal) so a
        # future openrouter/* entry missing usage.include is caught automatically —
        # otherwise it silently regresses to static pricing.
        models = [m for m in _load()["model_list"] if str(m["litellm_params"]["model"]).startswith("openrouter/")]
        assert models, "no openrouter/* models in litellm-config model_list"
        for model in models:
            extra = model["litellm_params"]["extra_body"]
            # usage.include drives the actual-cost capture (cost_logger reads usage.cost).
            assert extra["usage"]["include"] is True, model["model_name"]
            provider = extra["provider"]
            assert provider["sort"] == "price"
            # allow_fallbacks: true, but bounded to the vetted `only` set (fall over
            # WITHIN the set on saturation; never outside it).
            assert provider["allow_fallbacks"] is True
            assert isinstance(provider["only"], list) and provider["only"]

    def test_callback_symbol_exists_in_cost_logger(self):
        # The callbacks string above names `cost_logger.proxy_handler_instance`;
        # prove that symbol actually exists so the config and module can't drift.
        module_name, attr = _CALLBACK.split(".")
        spec = importlib.util.spec_from_file_location(module_name, _COST_LOGGER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert hasattr(module, attr)
