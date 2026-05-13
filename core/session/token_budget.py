from __future__ import annotations

from typing import Any


def _rough_text_tokens(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        # CJK text averages ~1.5-2 chars/token, mixed content ~2-3.
        # Using 2 as a safe middle ground that doesn't over-count ASCII
        # but properly estimates CJK-heavy content.
        return max(1, len(value) // 2)
    return max(1, len(str(value)) // 2)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    total = 0
    total += _rough_text_tokens(message.get("content"))
    total += _rough_text_tokens(message.get("reasoning"))
    total += _rough_text_tokens(message.get("tool_calls"))
    return max(1, total)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def calibrated_input_tokens(
    estimated_tokens: int,
    observed_prompt_tokens: int,
) -> int:
    if observed_prompt_tokens <= 0:
        return estimated_tokens
    return max(estimated_tokens, observed_prompt_tokens)


def should_trigger_summary_compact(
    used_tokens: int,
    context_window_tokens: int,
    reserved_output_tokens: int,
    compact_buffer_tokens: int,
) -> bool:
    threshold = context_window_tokens - reserved_output_tokens - compact_buffer_tokens
    return used_tokens >= threshold


def calc_effective_context_window(
    *,
    context_window_tokens: int,
    max_output_tokens: int,
) -> int:
    return context_window_tokens - max_output_tokens


def calc_waterlines(
    *,
    context_window_tokens: int,
    max_output_tokens: int,
) -> dict[str, int]:
    effective = calc_effective_context_window(
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
    )
    return {
        "preview_strip": effective - 40_000,
        "microcompact": effective - 20_000,
        "auto_compact": effective - 13_000,
        "blocking": effective - 3_000,
    }
