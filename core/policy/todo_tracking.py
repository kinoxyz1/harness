from __future__ import annotations


class TodoTrackingPolicy:
    def __init__(self) -> None:
        self._rounds_without_todo = 0

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        return []

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        if "todo" in getattr(batch_result, "tool_names", []):
            self._rounds_without_todo = 0
            return []
        self._rounds_without_todo += 1
        if self._rounds_without_todo >= 3:
            self._rounds_without_todo = 0
            return [{"role": "user", "content": "<reminder>重新评估你的计划，更新进度后再继续。</reminder>"}]
        return []

    def should_stop(self, context, state) -> str | None:
        return None
