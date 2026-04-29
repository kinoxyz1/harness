from core.query.state import RunState
from core.session.state import SessionState
from core.session.token_budget import (
    calibrated_input_tokens,
    estimate_message_tokens,
    estimate_messages_tokens,
    should_trigger_summary_compact,
)


def test_session_and_run_state_expose_compact_defaults() -> None:
    session = SessionState(conversation_messages=[])
    run = RunState()

    assert session.compact_state["tool_result_replacements"] == {}
    assert session.compact_state["consecutive_summary_failures"] == 0
    assert session.compact_state["summary_compact_cooldown_until"] == 0.0
    assert session.compact_state["last_prompt_tokens"] == 0
    assert session.compact_state["last_compact_observability"] == {}
    assert run.reactive_recovery_attempted is False
    assert run.context_observability == {}


def test_estimate_message_tokens_counts_reasoning_and_tool_calls() -> None:
    message = {
        "role": "assistant",
        "content": "done",
        "reasoning": "step " * 40,
        "tool_calls": [
            {"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}},
        ],
    }

    assert estimate_message_tokens(message) >= 25


def test_estimate_messages_tokens_sums_multiple_messages() -> None:
    messages = [
        {"role": "user", "content": "a" * 80},
        {"role": "assistant", "content": "b" * 40},
    ]

    assert estimate_messages_tokens(messages) >= 30


def test_should_trigger_summary_compact_reserves_output_headroom() -> None:
    should_compact = should_trigger_summary_compact(
        used_tokens=89_000,
        context_window_tokens=100_000,
        reserved_output_tokens=10_000,
        compact_buffer_tokens=1_000,
    )

    assert should_compact is True


def test_should_trigger_summary_compact_threshold_boundary() -> None:
    assert (
        should_trigger_summary_compact(
            used_tokens=88_999,
            context_window_tokens=100_000,
            reserved_output_tokens=10_000,
            compact_buffer_tokens=1_000,
        )
        is False
    )
    assert (
        should_trigger_summary_compact(
            used_tokens=89_000,
            context_window_tokens=100_000,
            reserved_output_tokens=10_000,
            compact_buffer_tokens=1_000,
        )
        is True
    )


def test_calibrated_input_tokens_prefers_observed_prompt_usage() -> None:
    assert calibrated_input_tokens(estimated_tokens=120, observed_prompt_tokens=0) == 120
    assert calibrated_input_tokens(estimated_tokens=120, observed_prompt_tokens=80) == 120
    assert calibrated_input_tokens(estimated_tokens=120, observed_prompt_tokens=180) == 180
