from __future__ import annotations

from pathlib import Path

from .content_replacement import ContentReplacementState

PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
STRUCTURED_TOOLS = {"todo", "skill"}
WORKING_CONTEXT_TOOLS = {"read_file"}


class ToolResultOffloader:
    def __init__(
        self,
        *,
        tool_result_dir: Path,
        replacement_state: ContentReplacementState,
        default_persist_threshold: int = 8_000,
        bash_persist_threshold: int = 3_000,
        aggregate_budget: int = 50_000,
        preview_size: int = 1_500,
    ) -> None:
        self._tool_result_dir = tool_result_dir
        self._state = replacement_state
        self._default_persist_threshold = default_persist_threshold
        self._bash_persist_threshold = bash_persist_threshold
        self._aggregate_budget = aggregate_budget
        self._preview_size = preview_size

    def maybe_persist(self, tool_use_id: str, content: str, *, tool_name: str) -> str:
        threshold = self._get_persistence_threshold(tool_name)
        if len(content) <= threshold:
            return content
        safe_id = tool_use_id.replace("/", "_").replace("\\", "_").replace("..", "_")
        self._tool_result_dir.mkdir(parents=True, exist_ok=True)
        filepath = self._tool_result_dir / f"{safe_id}.txt"
        filepath.write_text(content, encoding="utf-8")
        preview = self._truncate_preview(content)
        preview_len = len(preview)
        replacement = (
            f"{PERSISTED_OUTPUT_TAG}\n"
            f"Output too large ({len(content)} chars). Full output saved to: {filepath}\n\n"
            f"Preview (first {preview_len} bytes):\n"
            f"{preview}\n"
            f"{PERSISTED_OUTPUT_CLOSING_TAG}"
        )
        self._state.seen_ids.add(tool_use_id)
        self._state.replacements[tool_use_id] = replacement
        return replacement

    def enforce_per_message_budget(self, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        tool_name_by_id = self._build_tool_name_map(messages)
        total_chars = 0
        rewritten: list[dict[str, object]] = []
        candidates: list[tuple[int, str, str]] = []

        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                rewritten.append(dict(message))
                continue
            message_copy = dict(message)
            tool_use_id = str(message_copy.get("tool_call_id", ""))
            tool_name = tool_name_by_id.get(tool_use_id, "")
            frozen = self._state.replacements.get(tool_use_id)
            if frozen is not None:
                message_copy["content"] = frozen
            content = str(message_copy.get("content", ""))
            if tool_name not in WORKING_CONTEXT_TOOLS | STRUCTURED_TOOLS:
                total_chars += len(content)
                candidates.append((idx, tool_use_id, tool_name))
            rewritten.append(message_copy)

        if total_chars <= self._aggregate_budget:
            return rewritten

        for idx, tool_use_id, tool_name in sorted(
            candidates,
            key=lambda item: len(str(rewritten[item[0]].get("content", ""))),
            reverse=True,
        ):
            if total_chars <= self._aggregate_budget:
                break
            content = str(rewritten[idx]["content"])
            replacement = self._state.replacements.get(tool_use_id) or self.maybe_persist(
                tool_use_id,
                content,
                tool_name=tool_name,
            )
            total_chars -= len(content)
            total_chars += len(replacement)
            rewritten[idx]["content"] = replacement
        return rewritten

    def _get_persistence_threshold(self, tool_name: str) -> int:
        if tool_name in WORKING_CONTEXT_TOOLS | STRUCTURED_TOOLS:
            return 10**18
        if tool_name == "bash":
            return self._bash_persist_threshold
        return self._default_persist_threshold

    def _truncate_preview(self, content: str) -> str:
        preview = content[:self._preview_size]
        last_newline = preview.rfind("\n")
        if last_newline > self._preview_size * 0.5:
            preview = preview[:last_newline]
        return preview

    def _build_tool_name_map(self, messages: list[dict[str, object]]) -> dict[str, str]:
        result: dict[str, str] = {}
        for message in messages:
            if message.get("role") != "assistant":
                continue
            for tool_call in message.get("tool_calls") or []:
                if isinstance(tool_call, dict) and tool_call.get("id") and tool_call.get("name"):
                    result[str(tool_call["id"])] = str(tool_call["name"])
        return result
