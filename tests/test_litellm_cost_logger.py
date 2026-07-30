"""Tests for the proxy-side per-call cost/cache callback (litellm/cost_logger.py).

The module lives outside the package (the proxy may run in its own env), so it's
loaded by file path. Its litellm import is guarded, so these run without litellm.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_PATH = Path(__file__).resolve().parent.parent / "litellm" / "cost_logger.py"


@pytest.fixture(scope="module")
def cl():
    spec = importlib.util.spec_from_file_location("cost_logger", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Real OpenRouter warm-call usage observed in the 2026-07-29 probe (DeepSeek V4 Pro).
WARM_USAGE = {
    "prompt_tokens": 4811,
    "completion_tokens": 8,
    "total_tokens": 4819,
    "cost": 0.0006692671,
    "prompt_tokens_details": {"cached_tokens": 4096, "cache_write_tokens": 0},
}
TAGS = {"x-ce-run-id": "abc123", "x-ce-task-id": "calc/v1", "x-ce-iteration": "2"}


class TestBuildCostRecord:
    def test_reads_real_usage_cost_and_cache(self, cl):
        rec = cl.build_cost_record(WARM_USAGE, TAGS, model="deepseek/deepseek-v4-pro", call_id="gen-1")
        assert rec is not None
        assert rec["cost"] == 0.0006692671  # OpenRouter usage.cost, the REAL price
        assert rec["input"] == 4811
        assert rec["cache_read"] == 4096
        assert rec["cache_write"] == 0
        assert rec["output"] == 8
        assert rec["run_id"] == "abc123"
        assert rec["task_id"] == "calc/v1"
        assert rec["iteration"] == "2"
        assert rec["model"] == "deepseek/deepseek-v4-pro"

    def test_reads_anthropic_shaped_usage(self, cl):
        # On the Anthropic-inbound path a call's usage can come back Anthropic-shaped
        # (input_tokens = UNCACHED slice, cache_read_input_tokens separate). The record's
        # `input` must normalize to the FULL prompt so uncached = input - cache_read holds.
        anthropic_usage = {
            "input_tokens": 715,  # uncached slice
            "output_tokens": 8,
            "cache_read_input_tokens": 4096,
            "cache_creation_input_tokens": 0,
            "cost": 0.0006692671,
        }
        rec = cl.build_cost_record(anthropic_usage, TAGS, model="deepseek/deepseek-v4-pro", call_id="g")
        assert rec["input"] == 715 + 4096 + 0  # normalized to total prompt (4811)
        assert rec["cache_read"] == 4096
        assert rec["cache_write"] == 0
        assert rec["output"] == 8
        assert rec["cost"] == 0.0006692671

    def test_none_when_no_tag_and_no_cost(self, cl):
        # Nothing joinable and no cost signal → skip (harness falls back to static).
        assert cl.build_cost_record({}, {}, model=None, call_id=None) is None
        assert cl.build_cost_record({"prompt_tokens": 10}, {}, model="m", call_id=None) is None

    def test_cost_alone_is_enough(self, cl):
        # A cost with no correlation tag is still worth recording.
        rec = cl.build_cost_record({"cost": 0.01}, {}, model="m", call_id=None)
        assert rec is not None and rec["cost"] == 0.01 and rec["run_id"] is None

    def test_tag_alone_is_enough(self, cl):
        # A correlation tag with no cost yet (e.g. cost absent) is still recorded.
        rec = cl.build_cost_record({}, {"x-ce-run-id": "r"}, model="m", call_id=None)
        assert rec is not None and rec["run_id"] == "r" and rec["cost"] is None

    def test_rejects_bool_and_nonnumeric_costs(self, cl):
        rec = cl.build_cost_record(
            {"cost": True, "prompt_tokens": "oops"}, {"x-ce-run-id": "r"}, model="m", call_id=None
        )
        assert rec["cost"] is None  # True is not a real number
        assert rec["input"] is None


class TestExtractTags:
    def test_pulls_and_lowercases_only_ce_tags(self, cl):
        kwargs = {
            "litellm_params": {
                "metadata": {
                    "headers": {
                        "X-CE-Run-Id": "abc",
                        "x-ce-iteration": "3",
                        "authorization": "Bearer secret",
                        "content-type": "application/json",
                    }
                }
            }
        }
        assert cl.extract_tags(kwargs) == {"x-ce-run-id": "abc", "x-ce-iteration": "3"}

    def test_merges_alternate_header_locations(self, cl):
        kwargs = {"proxy_server_request": {"headers": {"x-ce-task-id": "t1"}}}
        assert cl.extract_tags(kwargs) == {"x-ce-task-id": "t1"}

    def test_empty_when_no_headers(self, cl):
        assert cl.extract_tags({}) == {}


class TestAppendAndEmit:
    def test_append_writes_jsonl(self, cl, tmp_path, monkeypatch):
        path = tmp_path / "costs.jsonl"
        monkeypatch.setenv("LITELLM_COST_LOG", str(path))
        cl.append_record({"run_id": "a", "cost": 0.01})
        cl.append_record({"run_id": "b", "cost": 0.02})
        cl.append_record(None)  # no-op
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["run_id"] == "a"
        assert json.loads(lines[1])["cost"] == 0.02

    def test_append_noop_without_env(self, cl, monkeypatch):
        monkeypatch.delenv("LITELLM_COST_LOG", raising=False)
        cl.append_record({"run_id": "a"})  # must not raise

    def test_emit_end_to_end(self, cl, tmp_path, monkeypatch):
        path = tmp_path / "costs.jsonl"
        monkeypatch.setenv("LITELLM_COST_LOG", str(path))
        kwargs = {"litellm_params": {"metadata": {"headers": dict(TAGS)}}, "model": "deepseek/deepseek-v4-pro"}
        response_obj = {"id": "gen-9", "provider": "Baidu", "model": "deepseek/deepseek-v4-pro", "usage": WARM_USAGE}
        cl.proxy_handler_instance._emit(kwargs, response_obj)
        rec = json.loads(path.read_text().splitlines()[0])
        assert rec["run_id"] == "abc123" and rec["cost"] == 0.0006692671 and rec["cache_read"] == 4096

    def test_emit_never_raises_on_garbage(self, cl, monkeypatch):
        monkeypatch.delenv("LITELLM_COST_LOG", raising=False)
        cl.proxy_handler_instance._emit({}, None)
        cl.proxy_handler_instance._emit({"litellm_params": None}, object())


class TestDebugCapture:
    """The env-gated diagnostic that reveals which id fields the callback sees."""

    def test_debug_ids_captured_when_env_set(self, cl, tmp_path, monkeypatch):
        path = tmp_path / "costs.jsonl"
        monkeypatch.setenv("LITELLM_COST_LOG", str(path))
        monkeypatch.setenv("CODER_EVAL_COST_DEBUG", "1")
        kwargs = {"litellm_params": {"metadata": {"headers": dict(TAGS)}}, "model": "deepseek/deepseek-v4-pro"}
        # An Anthropic-shaped response id (msg_...) is exactly the join key we're probing for.
        response_obj = {
            "id": "msg_abc123",
            "provider": "Baidu",
            "model": "deepseek/deepseek-v4-pro",
            "usage": WARM_USAGE,
        }
        cl.proxy_handler_instance._emit(kwargs, response_obj)
        rec = json.loads(path.read_text().splitlines()[0])
        assert "_debug" in rec
        assert rec["_debug"]["response_id"] == "msg_abc123"
        # msg_-prefixed strings are surfaced as id candidates for inspection.
        assert rec["_debug"]["id_candidates"].get("response.id") == "msg_abc123"

    def test_no_debug_field_by_default(self, cl, tmp_path, monkeypatch):
        path = tmp_path / "costs.jsonl"
        monkeypatch.setenv("LITELLM_COST_LOG", str(path))
        monkeypatch.delenv("CODER_EVAL_COST_DEBUG", raising=False)
        cl.proxy_handler_instance._emit(
            {"litellm_params": {"metadata": {"headers": dict(TAGS)}}}, {"id": "gen-1", "usage": WARM_USAGE}
        )
        rec = json.loads(path.read_text().splitlines()[0])
        assert "_debug" not in rec
