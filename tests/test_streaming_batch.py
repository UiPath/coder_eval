"""Tests for streaming callback integration in batch execution."""

import inspect

from coder_eval.orchestration.batch import run_batch
from coder_eval.orchestration.config import BatchRunConfig


def test_batch_run_accepts_stream_callback_factory():
    """run_batch accepts a stream_callback_factory parameter."""
    sig = inspect.signature(run_batch)
    assert "stream_callback_factory" in sig.parameters


def test_batch_config_no_stream_mode():
    """BatchRunConfig does not carry stream_mode (handled at CLI level)."""
    assert "stream_mode" not in BatchRunConfig.model_fields
