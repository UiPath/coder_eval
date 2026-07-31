"""Tests for the proxy-side per-call cost/cache callback (litellm/cost_logger.py).

The module lives outside the package (the proxy may run in its own env), so it's
loaded by file path. Its litellm import is guarded, so these run without litellm.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
from datetime import datetime
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
TAGS = {"x-ce-run-id": "abc123", "x-ce-task-id": "calc/v1", "x-ce-iteration": "2", "x-ce-attempt": "att9"}


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
        assert rec["attempt"] == "att9"  # per-attempt nonce for rerun de-dup
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

    def test_rejects_non_finite_cost(self, cl):
        # A NaN/Inf cost would serialize as a bare NaN token (invalid JSON for the
        # whole task.json) and make the max_usd gate's cost>limit silently never fire.
        assert (
            cl.build_cost_record({"cost": float("nan")}, {"x-ce-run-id": "r"}, model="m", call_id=None)["cost"] is None
        )
        assert (
            cl.build_cost_record({"cost": float("inf")}, {"x-ce-run-id": "r"}, model="m", call_id=None)["cost"] is None
        )


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
        response_obj = {"id": "gen-9", "model": "deepseek/deepseek-v4-pro", "usage": WARM_USAGE}
        cl.proxy_handler_instance._emit(kwargs, response_obj)
        rec = json.loads(path.read_text().splitlines()[0])
        assert rec["run_id"] == "abc123" and rec["cost"] == 0.0006692671 and rec["cache_read"] == 4096

    def test_emit_never_raises_on_garbage(self, cl, monkeypatch):
        monkeypatch.delenv("LITELLM_COST_LOG", raising=False)
        cl.proxy_handler_instance._emit({}, None)
        cl.proxy_handler_instance._emit({"litellm_params": None}, object())


class TestHookInvocation:
    """LiteLLM only ever calls log_success_event / async_log_success_event (the
    config registers proxy_handler_instance); every other test calls _emit directly,
    so cover the real entry points and the never-break-the-proxy guard here."""

    def _kwargs(self):
        return {"litellm_params": {"metadata": {"headers": dict(TAGS)}}, "model": "m"}

    def test_sync_hook_writes_a_record(self, cl, tmp_path, monkeypatch):
        path = tmp_path / "costs.jsonl"
        monkeypatch.setenv("LITELLM_COST_LOG", str(path))
        cl.proxy_handler_instance.log_success_event(self._kwargs(), {"id": "g", "usage": WARM_USAGE}, 0.0, 1.0)
        assert json.loads(path.read_text().splitlines()[0])["cost"] == 0.0006692671

    async def test_async_hook_writes_a_record(self, cl, tmp_path, monkeypatch):
        path = tmp_path / "costs.jsonl"
        monkeypatch.setenv("LITELLM_COST_LOG", str(path))
        await cl.proxy_handler_instance.async_log_success_event(
            self._kwargs(), {"id": "g", "usage": WARM_USAGE}, 0.0, 1.0
        )
        assert json.loads(path.read_text().splitlines()[0])["cost"] == 0.0006692671

    def test_a_failing_writer_never_propagates(self, cl, tmp_path, monkeypatch):
        # The never-break-the-proxy guard (the bare except in _emit): even if
        # append_record raises (read-only path, ENOSPC, …) the hook must not propagate.
        monkeypatch.setenv("LITELLM_COST_LOG", str(tmp_path / "c.jsonl"))

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(cl, "append_record", _boom)
        cl.proxy_handler_instance.log_success_event(self._kwargs(), {"id": "g", "usage": WARM_USAGE}, 0.0, 1.0)

    def test_conforms_to_real_litellm_customlogger_signature(self):
        # Under test the repo's own litellm/ dir shadows the pip package, so
        # cost_logger falls back to a stub CustomLogger. When the real package IS
        # installed, assert the hook names/signatures the proxy will call exist on the
        # ABC — so an upstream rename fails loudly instead of silently capturing nothing.
        mod = pytest.importorskip("litellm.integrations.custom_logger")
        for name in ("log_success_event", "async_log_success_event"):
            assert hasattr(mod.CustomLogger, name)
            params = list(inspect.signature(getattr(mod.CustomLogger, name)).parameters)
            assert params[:5] == ["self", "kwargs", "response_obj", "start_time", "end_time"]


class TestWireContractRoundTrip:
    """Producer (build_cost_record) → JSONL → consumer (load_cost_records →
    apply_actual_cost). The record keys are hand-declared on both sides, so a rename
    would silently make cost fail to land; this crosses the boundary to pin them."""

    def test_cost_survives_the_json_round_trip(self, cl, tmp_path):
        from coder_eval.litellm_cost import apply_actual_cost, load_cost_records
        from coder_eval.models import AgentKind, EvaluationResult, TokenUsage, TurnRecord

        tags = {"x-ce-run-id": "R", "x-ce-task-id": "T", "x-ce-iteration": "0", "x-ce-attempt": "att1"}
        rec = cl.build_cost_record(WARM_USAGE, tags, model="m", call_id="gen-1")
        path = tmp_path / "c.jsonl"
        path.write_text(json.dumps(rec) + "\n", encoding="utf-8")

        turn = TurnRecord(
            iteration=0,
            user_input="u",
            agent_output="a",
            duration_seconds=1.0,
            token_usage=TokenUsage(total_cost_usd=0.99),
        )
        result = EvaluationResult(
            task_id="T",
            task_description="d",
            variant_id="v",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime(2026, 7, 30),
            final_status="SUCCESS",
            iteration_count=1,
            environment_info={},
            iterations=[turn],
        )
        applied = apply_actual_cost(result, run_id="R", task_id="T", attempt="att1", records=load_cost_records(path))
        assert applied == 1
        assert turn.token_usage.total_cost_usd == WARM_USAGE["cost"]  # the real cost round-tripped and landed


class TestToDict:
    """`_to_dict` coerces a dict / pydantic model / litellm object to a plain dict."""

    def test_uses_model_dump_and_survives_a_raising_dumper(self, cl):
        class Ok:
            def model_dump(self):
                return {"k": "v"}

        class Boom:
            def model_dump(self):
                raise RuntimeError("nope")

        assert cl._to_dict(Ok()) == {"k": "v"}  # happy path
        # A raising dumper falls through to {} — never propagates (the callback must
        # not break the proxy). This is the branch the bare except guards.
        assert cl._to_dict(Boom()) == {}
