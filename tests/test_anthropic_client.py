"""测试 Anthropic client：响应归一化、LLMResponse 属性。"""
import pytest
from unittest.mock import MagicMock, patch
from core.llm.anthropic_client import _parse_response, AnthropicClient, LLMResponse
from core.llm.client import ContextWindowExceededError
from core.llm.client import ModelRequestOptions


def _mock_anthropic_response(
    content_blocks=None,
    stop_reason="end_turn",
    input_tokens=100,
    output_tokens=50,
):
    """构建模拟 Anthropic API response 对象。"""
    if content_blocks is None:
        content_blocks = [MagicMock(type="text", text="Hello!")]

    response = MagicMock()
    response.content = content_blocks
    response.stop_reason = stop_reason
    response.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return response


class TestParseResponse:
    def test_text_only_response(self):
        resp = _mock_anthropic_response([
            MagicMock(type="text", text="Hello world"),
        ])
        llm = _parse_response(resp)
        assert llm.content == "Hello world"
        assert llm.tool_calls == []
        assert llm.finish_reason == "end_turn"
        assert llm.prompt_tokens == 100
        assert llm.completion_tokens == 50

    def test_multiple_text_blocks_joined(self):
        resp = _mock_anthropic_response([
            MagicMock(type="text", text="Part 1"),
            MagicMock(type="text", text="Part 2"),
        ])
        llm = _parse_response(resp)
        assert llm.content == "Part 1\nPart 2"

    def test_tool_use_blocks_normalized(self):
        tool_block = MagicMock(type="tool_use", id="tu_1", input={"command": "pwd"})
        tool_block.name = "bash"
        resp = _mock_anthropic_response([tool_block], stop_reason="tool_use")
        llm = _parse_response(resp)
        assert llm.content is None
        assert len(llm.tool_calls) == 1
        tc = llm.tool_calls[0]
        assert tc == {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}}

    def test_thinking_block_extracted(self):
        thinking_block = MagicMock(type="thinking", thinking="Let me reason...")
        text_block = MagicMock(type="text", text="Answer")
        resp = _mock_anthropic_response([thinking_block, text_block])
        llm = _parse_response(resp)
        assert llm.reasoning == "Let me reason..."
        assert llm.content == "Answer"

    def test_no_thinking_block_gives_none(self):
        resp = _mock_anthropic_response([MagicMock(type="text", text="Hi")])
        llm = _parse_response(resp)
        assert llm.reasoning is None


class TestLLMResponseProperties:
    def test_has_content_true(self):
        llm = LLMResponse(content="Hello")
        assert llm.has_content is True

    def test_has_content_false_when_empty(self):
        llm = LLMResponse(content="")
        assert llm.has_content is False

    def test_is_tool_call_by_finish_reason(self):
        llm = LLMResponse(finish_reason="tool_use", tool_calls=[{"id": "1", "name": "bash", "args": {}}])
        assert llm.is_tool_call is True

    def test_is_truncated(self):
        llm = LLMResponse(finish_reason="max_tokens")
        assert llm.is_truncated is True


class TestClientCall:
    def test_stream_raises_error(self):
        client = AnthropicClient.__new__(AnthropicClient)
        with pytest.raises(NotImplementedError, match="Streaming"):
            client.call([], stream=True)

    @patch("core.llm.anthropic_client.create_llm_client")
    @patch("core.llm.anthropic_client.normalize_messages")
    @patch("core.llm.anthropic_client._parse_response")
    def test_call_uses_request_max_tokens_override(
        self,
        mock_parse_response,
        mock_normalize_messages,
        mock_create_client,
    ):
        mock_normalize_messages.return_value = ("", [{"role": "user", "content": "hi"}])
        mock_response = MagicMock()
        mock_response.content = []
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_create_client.return_value = mock_client
        mock_parse_response.return_value = LLMResponse(content="answer")

        client = AnthropicClient()
        client.call(
            [{"role": "user", "content": "hi"}],
            request_options=ModelRequestOptions(max_output_tokens=321),
        )

        assert mock_client.messages.create.call_args.kwargs["max_tokens"] == 321

    @patch("core.llm.anthropic_client.create_llm_client")
    @patch("core.llm.anthropic_client.normalize_messages")
    @patch("core.llm.anthropic_client._parse_response")
    def test_call_disables_thinking_when_requested(
        self,
        mock_parse_response,
        mock_normalize_messages,
        mock_create_client,
    ):
        mock_normalize_messages.return_value = ("", [{"role": "user", "content": "hi"}])
        mock_response = MagicMock()
        mock_response.content = []
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_create_client.return_value = mock_client
        mock_parse_response.return_value = LLMResponse(content="answer")

        client = AnthropicClient()
        client.call(
            [{"role": "user", "content": "hi"}],
            request_options=ModelRequestOptions(thinking_mode="disabled"),
        )

        assert "thinking" not in mock_client.messages.create.call_args.kwargs

    @patch("core.llm.anthropic_client.create_llm_client")
    @patch("core.llm.anthropic_client.normalize_messages")
    @patch("core.llm.anthropic_client._parse_response")
    def test_disabled_thinking_call_does_not_poison_adaptive_support_cache(
        self,
        mock_parse_response,
        mock_normalize_messages,
        mock_create_client,
    ):
        mock_normalize_messages.return_value = ("", [{"role": "user", "content": "hi"}])
        mock_response = MagicMock()
        mock_response.content = []
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_create_client.return_value = mock_client
        mock_parse_response.return_value = LLMResponse(content="answer")

        client = AnthropicClient()
        client.call(
            [{"role": "user", "content": "hi"}],
            request_options=ModelRequestOptions(thinking_mode="disabled"),
        )

        assert client._adaptive_supported is None

        client.call([{"role": "user", "content": "hi"}])

        assert mock_client.messages.create.call_args.kwargs["thinking"] == {"type": "adaptive"}

    @patch("core.llm.anthropic_client.create_llm_client")
    @patch("core.llm.anthropic_client.normalize_messages")
    def test_call_reclassifies_prompt_too_long_errors(
        self,
        mock_normalize_messages,
        mock_create_client,
    ):
        mock_normalize_messages.return_value = ("", [{"role": "user", "content": "hi"}])
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError(
            "prompt is too long: 210000 tokens > 200000 maximum"
        )
        mock_create_client.return_value = mock_client

        client = AnthropicClient()

        with pytest.raises(ContextWindowExceededError):
            client.call([{"role": "user", "content": "hi"}])

    @patch("core.llm.anthropic_client.create_llm_client")
    @patch("core.llm.anthropic_client.normalize_messages")
    def test_call_reclassifies_prompt_too_long_errors_on_adaptive_fallback_retry(
        self,
        mock_normalize_messages,
        mock_create_client,
    ):
        mock_normalize_messages.return_value = ("", [{"role": "user", "content": "hi"}])
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            RuntimeError("adaptive thinking is not supported"),
            RuntimeError("prompt is too long: 210000 tokens > 200000 maximum"),
        ]
        mock_create_client.return_value = mock_client

        client = AnthropicClient()

        with pytest.raises(ContextWindowExceededError):
            client.call([{"role": "user", "content": "hi"}])

        assert mock_client.messages.create.call_count == 2

    @patch("core.llm.anthropic_client.create_llm_client")
    @patch("core.llm.anthropic_client.normalize_messages")
    def test_call_does_not_reclassify_non_overflow_errors_on_adaptive_fallback_retry(
        self,
        mock_normalize_messages,
        mock_create_client,
    ):
        mock_normalize_messages.return_value = ("", [{"role": "user", "content": "hi"}])
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            RuntimeError("adaptive thinking is not supported"),
            RuntimeError("boom"),
        ]
        mock_create_client.return_value = mock_client

        client = AnthropicClient()

        with pytest.raises(RuntimeError, match="boom"):
            client.call([{"role": "user", "content": "hi"}])

        assert mock_client.messages.create.call_count == 2
