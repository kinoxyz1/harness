from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .token_budget import (
    calibrated_input_tokens,
    estimate_messages_tokens,
    should_trigger_summary_compact,
)


@dataclass(slots=True)
class PreparedContext:
    messages: list[dict[str, Any]]
    observability: dict[str, Any] = field(default_factory=dict)


class ContextManager:
    def __init__(
        self,
        *,
        compact_service,
        summary_gateway,
        context_window_tokens: int = 100_000,
        summary_breaker_cooldown_seconds: float = 60.0,
        time_fn=time.monotonic,
    ) -> None:
        self._compact_service = compact_service
        self._summary_gateway = summary_gateway
        self._context_window_tokens = context_window_tokens
        self._summary_breaker_cooldown_seconds = summary_breaker_cooldown_seconds
        self._time_fn = time_fn

    def _summary_breaker_open(self, session_state) -> bool:
        if session_state.compact_state["consecutive_summary_failures"] < 3:
            return False
        return self._time_fn() < session_state.compact_state["summary_compact_cooldown_until"]

    def _mark_summary_failure(self, session_state) -> None:
        session_state.compact_state["consecutive_summary_failures"] += 1
        if session_state.compact_state["consecutive_summary_failures"] >= 3:
            session_state.compact_state["summary_compact_cooldown_until"] = (
                self._time_fn() + self._summary_breaker_cooldown_seconds
            )

    def _mark_summary_success(self, session_state) -> None:
        session_state.compact_state["consecutive_summary_failures"] = 0
        session_state.compact_state["summary_compact_cooldown_until"] = 0.0

    def _summarize_with_breaker(
        self,
        *,
        messages: list[dict[str, Any]],
        session_state,
        keep_last_messages: int,
        observability: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self._summary_breaker_open(session_state):
            observability["steps"].append("summary_compact_skipped_breaker")
            return messages

        try:
            compacted = self._compact_service.summarize_and_compact(
                messages,
                state=session_state,
                summary_gateway=self._summary_gateway,
                keep_last_messages=keep_last_messages,
            )
        except Exception:
            self._mark_summary_failure(session_state)
            observability["steps"].append("summary_compact_failed")
            return messages

        self._mark_summary_success(session_state)
        observability["steps"].append("summary_compact")
        return compacted

    def reactive_recover(self, *, session_state, run_state, store) -> PreparedContext:
        messages = list(session_state.conversation_messages)
        before_tokens = estimate_messages_tokens(messages)
        observability = {
            "steps": ["reactive_recover"],
            "before_tokens": before_tokens,
            "after_tokens": before_tokens,
        }
        compacted = self._summarize_with_breaker(
            messages=messages,
            session_state=session_state,
            keep_last_messages=2,
            observability=observability,
        )
        if observability["steps"][-1] == "summary_compact" and store is not None:
            store.replace_working_transcript(compacted)

        observability["after_tokens"] = estimate_messages_tokens(compacted)
        session_state.compact_state["last_compact_observability"] = observability
        run_state.context_observability = observability
        return PreparedContext(messages=compacted, observability=observability)

    def prepare_for_query(
        self,
        *,
        session_state,
        run_state,
        store,
        query_source: str,
    ) -> PreparedContext:
        messages = list(session_state.conversation_messages)
        estimated_tokens = estimate_messages_tokens(messages)
        used_tokens = calibrated_input_tokens(
            estimated_tokens=estimated_tokens,
            observed_prompt_tokens=session_state.compact_state["last_prompt_tokens"],
        )
        observability = {
            "steps": ["estimate"],
            "before_tokens": used_tokens,
            "after_tokens": used_tokens,
        }

        messages = self._compact_service.apply_tool_result_budget(
            messages,
            state=session_state,
            per_message_token_limit=1200,
        )
        observability["steps"].append("tool_result_budget")

        messages = self._compact_service.apply_time_based_microcompact(
            messages,
            age_cutoff_seconds=1800,
            keep_recent_trajectories=2,
        )
        observability["steps"].append("microcompact")
        post_pruning_tokens = estimate_messages_tokens(messages)

        if query_source != "compact" and should_trigger_summary_compact(
            used_tokens=post_pruning_tokens,
            context_window_tokens=self._context_window_tokens,
            reserved_output_tokens=10_000,
            compact_buffer_tokens=1_000,
        ):
            messages = self._summarize_with_breaker(
                messages=messages,
                session_state=session_state,
                keep_last_messages=4,
                observability=observability,
            )
            if observability["steps"][-1] == "summary_compact":
                if store is not None:
                    store.replace_working_transcript(messages)

        observability["after_tokens"] = estimate_messages_tokens(messages)
        session_state.compact_state["last_compact_observability"] = observability
        run_state.context_observability = observability
        return PreparedContext(messages=messages, observability=observability)
