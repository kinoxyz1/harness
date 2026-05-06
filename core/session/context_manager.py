from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .query_context import ContextBlock, PreparedQueryContext
from .token_budget import (
    calibrated_input_tokens,
    estimate_messages_tokens,
    should_trigger_summary_compact,
)


def _estimate_tools_tokens(tools: list[dict[str, Any]] | None) -> int:
    if not tools:
        return 0
    return max(1, len(str(tools)) // 4)


def _prune_optional_runtime_blocks(
    blocks: list[ContextBlock],
    *,
    optional_budget: int,
) -> list[ContextBlock]:
    kept: list[ContextBlock] = []
    optional_used = 0
    for block in blocks:
        if block.required:
            kept.append(block)
            continue
        if optional_used + block.token_estimate > optional_budget:
            continue
        kept.append(block)
        optional_used += block.token_estimate
    return kept


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

    def _prepare_core(
        self,
        *,
        session_state,
        run_state,
        store,
        stable_system: str,
        stable_tools: list[dict[str, Any]] | None,
        runtime_blocks: list[ContextBlock],
        overlay_blocks: list[ContextBlock],
        query_source: str | None = None,
        is_reactive: bool = False,
    ) -> PreparedQueryContext:
        messages = list(session_state.conversation_messages)
        estimated_tokens = estimate_messages_tokens(messages)
        used_tokens = calibrated_input_tokens(
            estimated_tokens=estimated_tokens,
            observed_prompt_tokens=session_state.compact_state["last_prompt_tokens"],
        )

        stable_system_tokens = max(1, len(stable_system) // 4)
        stable_tools_tokens = _estimate_tools_tokens(stable_tools)
        required_runtime_tokens = sum(
            block.token_estimate for block in runtime_blocks if block.required
        )

        optional_budget = 12_000
        all_blocks = list(runtime_blocks) + list(overlay_blocks)
        kept_runtime_blocks = _prune_optional_runtime_blocks(
            all_blocks, optional_budget=optional_budget
        )

        steps = ["reactive_recover"] if is_reactive else ["estimate"]
        observability = {
            "steps": steps,
            "before_tokens": used_tokens,
            "after_tokens": used_tokens,
        }

        if not is_reactive:
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

            total_used_tokens = used_tokens + stable_system_tokens + stable_tools_tokens + required_runtime_tokens
            if query_source != "compact" and should_trigger_summary_compact(
                used_tokens=total_used_tokens,
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
        else:
            compacted = self._summarize_with_breaker(
                messages=messages,
                session_state=session_state,
                keep_last_messages=2,
                observability=observability,
            )
            if observability["steps"][-1] == "summary_compact" and store is not None:
                store.replace_working_transcript(compacted)
            messages = compacted

        observability["after_tokens"] = estimate_messages_tokens(messages)
        session_state.compact_state["last_compact_observability"] = observability
        run_state.context_observability = observability

        return PreparedQueryContext(
            stable_system=stable_system,
            stable_tools=stable_tools,
            runtime_blocks=kept_runtime_blocks,
            working_transcript=messages,
            observability=observability,
            budget={
                "stable_system_tokens": stable_system_tokens,
                "stable_tools_tokens": stable_tools_tokens,
                "required_runtime_tokens": required_runtime_tokens,
            },
        )

    def reactive_recover(
        self,
        *,
        session_state,
        run_state,
        store,
        stable_system: str = "",
        stable_tools: list[dict[str, Any]] | None = None,
        runtime_blocks: list[ContextBlock] | None = None,
        overlay_blocks: list[ContextBlock] | None = None,
    ) -> PreparedQueryContext:
        return self._prepare_core(
            session_state=session_state,
            run_state=run_state,
            store=store,
            stable_system=stable_system,
            stable_tools=stable_tools,
            runtime_blocks=runtime_blocks or [],
            overlay_blocks=overlay_blocks or [],
            is_reactive=True,
        )

    def prepare_for_query(
        self,
        *,
        session_state,
        run_state,
        store,
        query_source: str,
        stable_system: str = "",
        stable_tools: list[dict[str, Any]] | None = None,
        runtime_blocks: list[ContextBlock] | None = None,
        overlay_blocks: list[ContextBlock] | None = None,
    ) -> PreparedQueryContext:
        return self._prepare_core(
            session_state=session_state,
            run_state=run_state,
            store=store,
            stable_system=stable_system,
            stable_tools=stable_tools,
            runtime_blocks=runtime_blocks or [],
            overlay_blocks=overlay_blocks or [],
            query_source=query_source,
            is_reactive=False,
        )
