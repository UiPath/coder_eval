"""Shape guards for the LiteLLM proxy config (litellm/litellm-config.yaml).

A typo in this YAML (a mis-spelled key, a wrong callback path) fails silently at
proxy startup rather than in CI, so pin the structure the open-weight cost feature
relies on: `usage.include` + the vetted provider pins per model, and the callback
registration matching the real symbol in cost_logger.py.

Same rationale covers the sidecar dependency pins: `start-litellm.sh` is the SSOT
for `LITELLM_SPEC` / `LITELLM_FASTAPI_SPEC`, and the pins are restated in the
config's `# Run manually` comment and the cost_logger docstring. Nothing else
mechanically checks the shell launcher (no shellcheck hook, no CI job touches
`litellm/`), so a future pin bump could silently desync those copies and only fail
at proxy startup — exactly the failure mode the pins exist to fix. These guards
keep the copies in lockstep and syntax-check the launcher.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _REPO_ROOT / "litellm" / "litellm-config.yaml"
_COST_LOGGER = _REPO_ROOT / "litellm" / "cost_logger.py"
_START_SCRIPT = _REPO_ROOT / "litellm" / "start-litellm.sh"

_CALLBACK = "cost_logger.proxy_handler_instance"


def _load() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def _pin_defaults() -> dict[str, str]:
    """Extract the `${VAR:-<default>}` pin defaults from start-litellm.sh (the SSOT)."""
    text = _START_SCRIPT.read_text(encoding="utf-8")
    pins: dict[str, str] = {}
    for var in ("LITELLM_SPEC", "LITELLM_FASTAPI_SPEC"):
        m = re.search(rf'{var}="\$\{{{var}:-(?P<val>[^}}]+)\}}"', text)
        assert m, f"{var} default not found in {_START_SCRIPT.name}"
        pins[var] = m.group("val")
    return pins


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


class TestLitellmSidecarPins:
    def test_defaults_are_full_pip_specifiers(self):
        # A bare version (e.g. LITELLM_FASTAPI_SPEC=0.140.13) would become
        # `uvx --with 0.140.13` and fail with an opaque 'package not found' rather
        # than a pin error, so every default must carry a version comparator.
        for var, spec in _pin_defaults().items():
            assert var.endswith("_SPEC"), f"{var} holds a specifier; name it *_SPEC"
            assert re.search(r"[=<>~]=?", spec), f"{var}={spec!r} is not a pip specifier"

    def test_pins_are_restated_in_every_committed_copy(self):
        # start-litellm.sh is the SSOT; the config's `# Run manually` comment and the
        # cost_logger docstring each restate the invocation. Assert both exact pins
        # appear verbatim in each, so a future bump can't silently desync them.
        pins = _pin_defaults().values()
        config_text = _CONFIG.read_text(encoding="utf-8")
        cost_logger_text = _COST_LOGGER.read_text(encoding="utf-8")
        for pin in pins:
            assert pin in config_text, f"{pin!r} missing from litellm-config.yaml comment"
            assert pin in cost_logger_text, f"{pin!r} missing from cost_logger.py docstring"

    def test_start_script_has_valid_bash_syntax(self):
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash not available")
        result = subprocess.run(
            [bash, "-n", str(_START_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
