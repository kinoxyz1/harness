"""测试 Anthropic client：响应归一化、LLMResponse 属性。"""
import pytest
from unittest.mock import MagicMock, patch
from core.llm.anthropic_client import _parse_response, LLMResponse


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
        from core.llm.anthropic_client import AnthropicClient
        client = AnthropicClient.__new__(AnthropicClient)
        with pytest.raises(NotImplementedError, match="Streaming"):
            client.call([], stream=True)
