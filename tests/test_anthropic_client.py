"""测试 Anthropic client：响应归一化、LLMResponse 属性。"""
import pytest
import threading
from unittest.mock import MagicMock, patch
from core.llm.anthropic_client import _parse_response, AnthropicClient, LLMResponse
from core.llm.client import ContextWindowExceededError, RequestCancelledError
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
        with patch("core.llm.anthropic_client.THINKING_MODE", "auto"):
            client.call(
                [{"role": "user", "content": "hi"}],
                request_options=ModelRequestOptions(thinking_mode="disabled"),
            )

            assert client._adaptive_supported is None

            client.call([{"role": "user", "content": "hi"}])

        assert mock_client.messages.create.call_args.kwargs["thinking"] == {"type": "adaptive"}

    @patch("core.llm.anthropic_client.create_llm_client")
    @patch("core.llm.anthropic_client.normalize_messages")
    @patch("core.llm.anthropic_client._parse_response")
    def test_internal_memory_review_call_stays_silent(
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
        mock_parse_response.return_value = LLMResponse(
            content=None,
            tool_calls=[{"id": "toolu_1", "name": "memory", "args": {}}],
            finish_reason="tool_use",
            prompt_tokens=10,
            completion_tokens=5,
        )

        client = AnthropicClient()
        with (
            patch("core.llm.anthropic_client.sys.stdout.write") as mock_write,
            patch("core.llm.anthropic_client.sys.stdout.flush"),
            patch("core.llm.anthropic_client._console.print") as mock_print,
        ):
            client.call(
                [{"role": "user", "content": "hi"}],
                request_options=ModelRequestOptions(query_source="memory_review"),
            )

        mock_write.assert_not_called()
        mock_print.assert_not_called()

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

    @patch("core.llm.anthropic_client.create_llm_client")
    @patch("core.llm.anthropic_client.normalize_messages")
    @patch("core.llm.anthropic_client.time.sleep")
    def test_call_retries_transient_gateway_errors(
        self,
        mock_sleep,
        mock_normalize_messages,
        mock_create_client,
    ):
        mock_normalize_messages.return_value = ("", [{"role": "user", "content": "hi"}])
        mock_response = MagicMock()
        mock_response.content = []
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            RuntimeError("HTTP/1.1 504 Gateway Time-out"),
            mock_response,
        ]
        mock_create_client.return_value = mock_client

        with patch("core.llm.anthropic_client._parse_response", return_value=LLMResponse(content="ok")):
            client = AnthropicClient()
            resp = client.call([{"role": "user", "content": "hi"}])

        assert resp.content == "ok"
        assert mock_client.messages.create.call_count == 2
        mock_sleep.assert_called_once()

    @patch("core.llm.anthropic_client.create_llm_client")
    @patch("core.llm.anthropic_client.normalize_messages")
    def test_call_raises_request_cancelled_when_cancel_check_trips(
        self,
        mock_normalize_messages,
        mock_create_client,
    ):
        mock_normalize_messages.return_value = ("", [{"role": "user", "content": "hi"}])
        release = threading.Event()

        def slow_call(**kwargs):
            release.wait(timeout=2.0)
            return MagicMock(content=[], stop_reason="end_turn", usage=MagicMock(input_tokens=0, output_tokens=0))

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = slow_call
        mock_create_client.return_value = mock_client

        client = AnthropicClient()
        calls = {"count": 0}

        def cancel_check() -> bool:
            calls["count"] += 1
            return calls["count"] >= 2

        with pytest.raises(RequestCancelledError):
            client.call(
                [{"role": "user", "content": "hi"}],
                request_options=ModelRequestOptions(cancel_check=cancel_check),
                display=MagicMock(quiet=True),
            )

        release.set()
        assert mock_client.close.called


class TestClientStream:
    def test_stream_emits_tool_call_ready_for_tool_use_block(self):
        """tool_use content block with input already available at content_block_start."""
        stream_ctx = MagicMock()
        tool_block = MagicMock(type="tool_use", id="toolu_1", input={"skill": "weather"})
        tool_block.name = "skill"
        stream_ctx.__enter__.return_value = iter(
            [
                MagicMock(type="message_start", message=MagicMock(usage=MagicMock(input_tokens=10, output_tokens=0))),
                MagicMock(type="content_block_start", index=0, content_block=tool_block),
                MagicMock(type="content_block_stop", index=0),
                MagicMock(type="message_delta", delta=MagicMock(stop_reason="tool_use"), usage=MagicMock(output_tokens=5)),
                MagicMock(type="message_stop"),
            ]
        )
        stream_ctx.__exit__.return_value = False

        client = AnthropicClient.__new__(AnthropicClient)
        client._client = MagicMock()
        client._adaptive_supported = True
        client._client.messages.stream.return_value = stream_ctx

        with patch("core.llm.anthropic_client.normalize_messages", return_value=("", [{"role": "user", "content": "hi"}])):
            events = list(client.stream([{"role": "user", "content": "hi"}], turn_id="turn-1"))

        assert [event.type for event in events] == [
            "response_start",
            "tool_call_ready",
            "response_completed",
        ]
        assert events[1].payload["tool_call"] == {
            "id": "toolu_1",
            "name": "skill",
            "args": {"skill": "weather"},
        }
        assert events[-1].payload["finish_reason"] == "tool_use"

    def test_stream_assembles_tool_input_via_json_deltas(self):
        """tool_use input arrives incrementally via input_json_delta events."""
        stream_ctx = MagicMock()
        tool_block = MagicMock(type="tool_use", id="toolu_2", input="")
        tool_block.name = "bash"
        stream_ctx.__enter__.return_value = iter(
            [
                MagicMock(type="message_start", message=MagicMock(usage=MagicMock(input_tokens=10, output_tokens=0))),
                MagicMock(type="content_block_start", index=0, content_block=tool_block),
                MagicMock(
                    type="content_block_delta",
                    index=0,
                    delta=MagicMock(type="input_json_delta", partial_json='{"comma'),
                ),
                MagicMock(
                    type="content_block_delta",
                    index=0,
                    delta=MagicMock(type="input_json_delta", partial_json='nd": "pwd"}'),
                ),
                MagicMock(type="content_block_stop", index=0),
                MagicMock(type="message_delta", delta=MagicMock(stop_reason="tool_use"), usage=MagicMock(output_tokens=5)),
                MagicMock(type="message_stop"),
            ]
        )
        stream_ctx.__exit__.return_value = False

        client = AnthropicClient.__new__(AnthropicClient)
        client._client = MagicMock()
        client._adaptive_supported = True
        client._client.messages.stream.return_value = stream_ctx

        with patch("core.llm.anthropic_client.normalize_messages", return_value=("", [{"role": "user", "content": "hi"}])):
            events = list(client.stream([{"role": "user", "content": "hi"}], turn_id="turn-2"))

        assert [event.type for event in events] == [
            "response_start",
            "tool_input_delta",
            "tool_input_delta",
            "tool_call_ready",
            "response_completed",
        ]
        assert events[3].payload["tool_call"] == {
            "id": "toolu_2",
            "name": "bash",
            "args": {"command": "pwd"},
        }

    def test_stream_tool_use_with_empty_dict_input_uses_json_deltas(self):
        """SDK sends input={} at content_block_start but real args arrive via input_json_delta."""
        stream_ctx = MagicMock()
        tool_block = MagicMock(type="tool_use", id="toolu_3", input={})
        tool_block.name = "bash"
        stream_ctx.__enter__.return_value = iter(
            [
                MagicMock(type="message_start", message=MagicMock(usage=MagicMock(input_tokens=10, output_tokens=0))),
                MagicMock(type="content_block_start", index=0, content_block=tool_block),
                MagicMock(
                    type="content_block_delta",
                    index=0,
                    delta=MagicMock(type="input_json_delta", partial_json='{"command": "ls"}'),
                ),
                MagicMock(type="content_block_stop", index=0),
                MagicMock(type="message_delta", delta=MagicMock(stop_reason="tool_use"), usage=MagicMock(output_tokens=5)),
                MagicMock(type="message_stop"),
            ]
        )
        stream_ctx.__exit__.return_value = False

        client = AnthropicClient.__new__(AnthropicClient)
        client._client = MagicMock()
        client._adaptive_supported = True
        client._client.messages.stream.return_value = stream_ctx

        with patch("core.llm.anthropic_client.normalize_messages", return_value=("", [{"role": "user", "content": "hi"}])):
            events = list(client.stream([{"role": "user", "content": "hi"}], turn_id="turn-4"))

        assert events[1].type == "tool_input_delta"
        assert events[2].type == "tool_call_ready"
        assert events[2].payload["tool_call"]["args"] == {"command": "ls"}

    def test_stream_text_and_thinking_deltas(self):
        """Stream emits thinking_delta and content_delta for normal text blocks."""
        stream_ctx = MagicMock()
        stream_ctx.__enter__.return_value = iter(
            [
                MagicMock(type="message_start", message=MagicMock(usage=MagicMock(input_tokens=10, output_tokens=0))),
                MagicMock(type="content_block_start", index=0, content_block=MagicMock(type="thinking")),
                MagicMock(type="content_block_delta", index=0, delta=MagicMock(type="thinking_delta", thinking="hmm")),
                MagicMock(type="content_block_stop", index=0),
                MagicMock(type="content_block_start", index=1, content_block=MagicMock(type="text")),
                MagicMock(type="content_block_delta", index=1, delta=MagicMock(type="text_delta", text="hello")),
                MagicMock(type="content_block_stop", index=1),
                MagicMock(type="message_delta", delta=MagicMock(stop_reason="end_turn"), usage=MagicMock(output_tokens=5)),
                MagicMock(type="message_stop"),
            ]
        )
        stream_ctx.__exit__.return_value = False

        client = AnthropicClient.__new__(AnthropicClient)
        client._client = MagicMock()
        client._adaptive_supported = True
        client._client.messages.stream.return_value = stream_ctx

        with patch("core.llm.anthropic_client.normalize_messages", return_value=("", [{"role": "user", "content": "hi"}])):
            events = list(client.stream([{"role": "user", "content": "hi"}], turn_id="turn-3"))

        assert [event.type for event in events] == [
            "response_start",
            "thinking_delta",
            "content_delta",
            "response_completed",
        ]
        assert events[1].payload["text"] == "hmm"
        assert events[2].payload["text"] == "hello"
        assert events[-1].payload["finish_reason"] == "end_turn"
