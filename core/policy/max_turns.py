from __future__ import annotations


class MaxTurnsPolicy:
    def __init__(self, max_turns: int):
        self._max_turns = max_turns

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        return []

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, context, state) -> str | None:
        if state.turn_count >= self._max_turns:
            return "max_turns"
        return None
