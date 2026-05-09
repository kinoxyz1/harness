from __future__ import annotations

import time
from typing import Any

from .microcompact import apply_time_based_microcompact
from .preview_strip import strip_persisted_output_previews
from .query_context import ContextBlock, PreparedQueryContext
from .token_budget import calc_effective_context_window, calc_waterlines, calibrated_input_tokens, estimate_messages_tokens


AUTO_COMPACT_KEEP_LAST_MESSAGES = 8   # Normal path: at least 2 full tool cycles
BLOCKING_GATE_KEEP_LAST_MESSAGES = 4  # Emergency path: more aggressive but at least 1 full cycle
REASONING_KEEP_RECENT_TURNS = 2      # Keep reasoning for the last N assistant turns only


class ContextGovernor:
    def __init__(
        self,
        *,
        offloader,
        compact_service,
        summary_gateway,
        context_window_tokens: int = 100_000,
        max_output_tokens: int = 10_000,
        summary_breaker_cooldown_seconds: float = 60.0,
        time_fn=time.monotonic,
    ) -> None:
        self._offloader = offloader
        self._compact_service = compact_service
        self._summary_gateway = summary_gateway
        self._context_window_tokens = context_window_tokens
        self._max_output_tokens = max_output_tokens
        self._summary_breaker_cooldown_seconds = summary_breaker_cooldown_seconds
        self._time_fn = time_fn

    def assess(
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
    ) -> PreparedQueryContext:
        messages = list(session_state.conversation_messages)
        waterlines = calc_waterlines(
            context_window_tokens=self._context_window_tokens,
            max_output_tokens=self._max_output_tokens,
        )
        # Compute stable overhead once — stable_system/stable_tools may change each turn
        stable_system_tokens = max(1, len(stable_system) // 4)
        stable_tools_tokens = 0 if not stable_tools else max(1, len(str(stable_tools)) // 4)
        required_runtime_tokens = sum(block.token_estimate for block in runtime_blocks if block.required)
        stable_overhead = stable_system_tokens + stable_tools_tokens + required_runtime_tokens

        water_level = self._calc_water_level(session_state, stable_overhead)
        messages = self._offloader.enforce_per_message_budget(messages)
        strategies_run: list[str] = []

        # Strategy 1: PreviewStrip
        if water_level >= waterlines["preview_strip"]:
            messages, changed = strip_persisted_output_previews(
                messages,
                session_state.content_replacement_state.replacements,
            )
            if changed:
                strategies_run.append("preview_strip")

        # Strategy 2: Microcompact (always runs)
        messages = apply_time_based_microcompact(
            messages,
            age_cutoff_seconds=1800,
            keep_recent_trajectories=2,
        )
        strategies_run.append("microcompact")

        # Strategy 2.5: Strip old reasoning/thinking content
        # Reasoning text accumulates in every assistant message and can dominate the context.
        # Keep reasoning for the most recent turns, strip from older ones.
        messages = self._strip_old_reasoning(messages, keep_recent=REASONING_KEEP_RECENT_TURNS)

        # Recalc water level after light strategies — prevents unnecessary auto_compact
        water_level = self._recalc_water_level(messages, stable_overhead)

        # Strategy 3: Auto-compact
        if water_level >= waterlines["auto_compact"]:
            messages = self._summarize_with_breaker(
                messages=messages,
                session_state=session_state,
                keep_last_messages=AUTO_COMPACT_KEEP_LAST_MESSAGES,
            )
            strategies_run.append("auto_compact")

        # Strategy 4: Blocking gate (pre-send check)
        water_level = self._recalc_water_level(messages, stable_overhead)
        if water_level >= waterlines["blocking"]:
            messages = self._run_blocking_recover(
                messages=messages,
                session_state=session_state,
                store=store,
            )
            strategies_run.append("blocking_gate")

        observability = {
            "water_level": water_level,
            "water_line": self._water_line_name(water_level, waterlines),
            "strategies_run": strategies_run,
            "steps": ["estimate", "per_message_budget", *strategies_run],
            "before_tokens": water_level,
            "after_tokens": estimate_messages_tokens(messages),
        }
        session_state.compact_state["last_compact_observability"] = observability
        run_state.context_observability = observability
        all_blocks = list(runtime_blocks) + list(overlay_blocks)
        return PreparedQueryContext(
            stable_system=stable_system,
            stable_tools=stable_tools,
            runtime_blocks=all_blocks,
            working_transcript=messages,
            observability=observability,
            budget={
                "stable_system_tokens": stable_system_tokens,
                "stable_tools_tokens": stable_tools_tokens,
                "required_runtime_tokens": required_runtime_tokens,
            },
        )

    def _calc_water_level(self, session_state, stable_overhead: int) -> int:
        estimated = estimate_messages_tokens(session_state.conversation_messages)
        used = calibrated_input_tokens(
            estimated_tokens=estimated,
            observed_prompt_tokens=session_state.compact_state["last_prompt_tokens"],
        )
        return used + stable_overhead

    def _recalc_water_level(self, messages: list[dict[str, Any]], stable_overhead: int) -> int:
        """Re-estimate water level after strategy execution. Prevents unnecessary auto_compact
        when light strategies (preview_strip + microcompact) already reduced the message size."""
        estimated = estimate_messages_tokens(messages)
        used = calibrated_input_tokens(
            estimated_tokens=estimated,
            observed_prompt_tokens=0,
        )
        return used + stable_overhead

    def _strip_old_reasoning(self, messages: list[dict[str, Any]], keep_recent: int) -> list[dict[str, Any]]:
        """Strip reasoning (thinking) text from older assistant messages.

        Reasoning text accumulates across turns and can consume significant context budget.
        This strategy keeps reasoning for the most recent N assistant turns (where it's most
        relevant for continuity) and replaces older reasoning with a placeholder.
        """
        # Find the indices of the last N assistant messages with reasoning
        assistant_with_reasoning = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "assistant" and msg.get("reasoning"):
                assistant_with_reasoning.append(i)

        if len(assistant_with_reasoning) <= keep_recent:
            return messages

        # Indices to strip: all except the last `keep_recent`
        strip_indices = set(assistant_with_reasoning[:-keep_recent]) if keep_recent > 0 else set(assistant_with_reasoning)

        result = []
        for i, msg in enumerate(messages):
            if i in strip_indices:
                cleaned = dict(msg)
                cleaned["reasoning"] = "[reasoning stripped]"
                result.append(cleaned)
            else:
                result.append(msg)
        return result

    def _summarize_with_breaker(self, *, messages, session_state, keep_last_messages):
        if self._summary_breaker_open(session_state):
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
            return messages
        self._mark_summary_success(session_state)
        return compacted

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

    def _run_blocking_recover(self, *, messages, session_state, store):
        compacted = self._compact_service.summarize_and_compact(
            messages,
            state=session_state,
            summary_gateway=self._summary_gateway,
            keep_last_messages=BLOCKING_GATE_KEEP_LAST_MESSAGES,
        )
        if store is not None:
            store.replace_working_transcript(compacted)
        return compacted

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
        messages = list(session_state.conversation_messages)
        messages = self._offloader.enforce_per_message_budget(messages)
        messages = apply_time_based_microcompact(
            messages,
            age_cutoff_seconds=0,
            keep_recent_trajectories=0,
        )
        messages = self._run_blocking_recover(
            messages=messages,
            session_state=session_state,
            store=store,
        )
        observability = {
            "water_level": 0,
            "water_line": "reactive_recovery",
            "strategies_run": ["reactive_recovery"],
            "steps": ["reactive_recovery"],
            "before_tokens": 0,
            "after_tokens": estimate_messages_tokens(messages),
        }
        session_state.compact_state["last_compact_observability"] = observability
        run_state.context_observability = observability
        return PreparedQueryContext(
            stable_system=stable_system,
            stable_tools=stable_tools,
            runtime_blocks=list(runtime_blocks or []) + list(overlay_blocks or []),
            working_transcript=messages,
            observability=observability,
        )

    def _water_line_name(self, water_level: int, waterlines: dict[str, int]) -> str:
        if water_level >= waterlines["blocking"]:
            return "blocking"
        if water_level >= waterlines["auto_compact"]:
            return "autocompact"
        if water_level >= waterlines["microcompact"]:
            return "microcompact"
        if water_level >= waterlines["preview_strip"]:
            return "preview_strip"
        return "normal"
