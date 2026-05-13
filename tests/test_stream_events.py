from core.llm.response import ModelResponse
from core.shared.stream_events import StreamAccumulator, StreamEvent, make_event


def test_make_event_defaults_timestamp_and_payload() -> None:
    event = make_event(
        type="response_start",
        turn_id="turn-1",
        sequence=1,
        origin="model",
        source_mode="live",
    )

    assert isinstance(event, StreamEvent)
    assert event.type == "response_start"
    assert event.turn_id == "turn-1"
    assert event.sequence == 1
    assert event.payload == {}
    assert event.timestamp > 0


def test_stream_accumulator_reconstructs_model_response() -> None:
    acc = StreamAccumulator()
    acc.consume(make_event("response_start", "turn-1", 1, "model", "live"))
    acc.consume(make_event("thinking_delta", "turn-1", 2, "model", "live", {"text": "先分析"}))
    acc.consume(make_event("content_delta", "turn-1", 3, "model", "live", {"text": "最终回答"}))
    acc.consume(
        make_event(
            "tool_call_ready",
            "turn-1",
            4,
            "model",
            "live",
            {"tool_call": {"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}}},
        )
    )
    acc.consume(
        make_event(
            "response_completed",
            "turn-1",
            5,
            "model",
            "live",
            {
                "finish_reason": "tool_use",
                "prompt_tokens": 123,
                "completion_tokens": 45,
                "reasoning_signature": "sig_1",
            },
        )
    )

    resp = acc.finalize()

    assert isinstance(resp, ModelResponse)
    assert resp.reasoning == "先分析"
    assert resp.content == "最终回答"
    assert resp.tool_calls == [{"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}}]
    assert resp.finish_reason == "tool_use"
    assert resp.prompt_tokens == 123
    assert resp.completion_tokens == 45
    assert resp.reasoning_signature == "sig_1"


def test_stream_accumulator_requires_completed_event_before_finalize() -> None:
    acc = StreamAccumulator()
    acc.consume(make_event("response_start", "turn-1", 1, "model", "live"))

    try:
        acc.finalize()
        assert False, "expected finalize() to reject incomplete streams"
    except RuntimeError as exc:
        assert "response_completed" in str(exc)


def test_stream_accumulator_captures_partial_state() -> None:
    acc = StreamAccumulator()
    acc.consume(make_event("response_start", "turn-1", 1, "model", "live"))
    acc.consume(make_event("thinking_delta", "turn-1", 2, "model", "live", {"text": "想法"}))
    acc.consume(make_event("content_delta", "turn-1", 3, "model", "live", {"text": "半截"}))

    snapshot = acc.snapshot()

    assert snapshot["reasoning"] == "想法"
    assert snapshot["content"] == "半截"
    assert snapshot["completed"] is False
