"""Tests for orchestration/rephrase.py — LLM Gateway rephrase callback."""

from unittest.mock import MagicMock, patch

import pytest

from coder_eval.models import PromptRephrase


class TestCreateRephraseFn:
    """Tests for create_rephrase_fn() factory and the returned callback."""

    @patch("uipath_llmgw_client.get_langchain_chat_model")
    def test_returns_callable(self, mock_get_model):
        from coder_eval.orchestration.rephrase import create_rephrase_fn

        fn = create_rephrase_fn()
        assert callable(fn)

    @patch("uipath_llmgw_client.get_langchain_chat_model")
    def test_rephrase_calls_llm_and_returns_content(self, mock_get_model):
        from coder_eval.orchestration.rephrase import create_rephrase_fn

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="rephrased prompt text")
        mock_get_model.return_value = mock_llm

        fn = create_rephrase_fn()
        mutation = PromptRephrase(instructions="Make it concise")
        result = fn("original prompt", mutation)

        assert result == "rephrased prompt text"
        mock_llm.invoke.assert_called_once()
        # Verify the messages contain system and user roles
        call_args = mock_llm.invoke.call_args[0][0]
        assert len(call_args) == 2
        assert call_args[0]["role"] == "system"
        assert call_args[1]["role"] == "user"
        assert "Make it concise" in call_args[1]["content"]
        assert "original prompt" in call_args[1]["content"]

    @patch("uipath_llmgw_client.get_langchain_chat_model")
    def test_llm_client_cached_by_config(self, mock_get_model):
        from coder_eval.orchestration.rephrase import create_rephrase_fn

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="result")
        mock_get_model.return_value = mock_llm

        fn = create_rephrase_fn()
        mutation = PromptRephrase(instructions="a", model="model-a", temperature=0.5)

        fn("prompt1", mutation)
        fn("prompt2", mutation)

        # Same config → get_langchain_chat_model called only once
        assert mock_get_model.call_count == 1

    @patch("uipath_llmgw_client.get_langchain_chat_model")
    def test_different_configs_create_separate_clients(self, mock_get_model):
        from coder_eval.orchestration.rephrase import create_rephrase_fn

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="result")
        mock_get_model.return_value = mock_llm

        fn = create_rephrase_fn()
        fn("prompt", PromptRephrase(instructions="a", model="model-a"))
        fn("prompt", PromptRephrase(instructions="b", model="model-b"))

        # Different models → two separate clients
        assert mock_get_model.call_count == 2

    @patch("uipath_llmgw_client.get_langchain_chat_model")
    def test_llm_invoke_error_wrapped_with_context(self, mock_get_model):
        from coder_eval.orchestration.rephrase import create_rephrase_fn

        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = ConnectionError("gateway timeout")
        mock_get_model.return_value = mock_llm

        fn = create_rephrase_fn()
        mutation = PromptRephrase(instructions="rephrase", model="test-model")

        with pytest.raises(RuntimeError, match=r"Prompt rephrase failed.*test-model.*gateway timeout"):
            fn("prompt", mutation)

    @patch("uipath_llmgw_client.get_langchain_chat_model")
    def test_non_string_content_converted(self, mock_get_model):
        from coder_eval.orchestration.rephrase import create_rephrase_fn

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content=12345)  # non-string
        mock_get_model.return_value = mock_llm

        fn = create_rephrase_fn()
        result = fn("prompt", PromptRephrase(instructions="x"))
        assert result == "12345"
