from __future__ import annotations

from typing import Any

from core.llm.client import ModelRequestOptions

from .state import SessionState
from .token_budget import estimate_message_tokens
from .transcript_rewriter import (
    build_post_compact_messages,
    create_compact_boundary,
    create_compact_summary,
)

TOOL_RESULT_PLACEHOLDER = "[Tool result compacted to stay within budget]"
MICROCOMPACT_PLACEHOLDER = "[Old tool result content cleared]"
COMPACTABLE_TOOLS = {"read_file", "find", "grep", "glob"}
SUMMARY_SYSTEM_PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

Write the summary using exactly these 9 sections:
1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and Fixes
5. Problem Solving
6. All User Messages
7. Pending Tasks
8. Current Work
9. Optional Next Step

CRITICAL: Output plain text only. No tool calls, no XML, no JSON, no markdown code fences."""


def apply_tool_result_budget(
    messages: list[dict[str, Any]],
    *,
    state: SessionState,
    per_message_token_limit: int,
) -> list[dict[str, Any]]:
    replacements = state.compact_state["tool_result_replacements"]
    compacted: list[dict[str, Any]] = []

    for message in messages:
        if message.get("role") != "tool":
            compacted.append(dict(message))
            continue

        tool_call_id = message.get("tool_call_id")
        replacement = replacements.get(tool_call_id) if tool_call_id else None
        if replacement is None and estimate_message_tokens(message) > per_message_token_limit:
            replacement = TOOL_RESULT_PLACEHOLDER
            if tool_call_id:
                replacements[tool_call_id] = replacement

        if replacement is None:
            compacted.append(dict(message))
            continue

        rewritten = dict(message)
        rewritten["content"] = replacement
        compacted.append(rewritten)

    return compacted


def apply_time_based_microcompact(
    messages: list[dict[str, Any]],
    *,
    age_cutoff_seconds: float,
    keep_recent_trajectories: int,
) -> list[dict[str, Any]]:
    timestamps = [
        created_at
        for message in messages
        if (created_at := _message_created_at(message)) is not None
    ]
    newest_timestamp = max(timestamps, default=None)
    if newest_timestamp is None:
        return [dict(message) for message in messages]

    compactable_tool_ids: list[str] = []
    compactable_tool_id_set: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = tool_call.get("id")
            tool_name = tool_call.get("name")
            if not tool_call_id or tool_name not in COMPACTABLE_TOOLS:
                continue
            compactable_tool_ids.append(tool_call_id)
            compactable_tool_id_set.add(tool_call_id)

    if not compactable_tool_id_set:
        return [dict(message) for message in messages]

    keep_ids = set(compactable_tool_ids[-keep_recent_trajectories:]) if keep_recent_trajectories > 0 else set()
    compacted: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "tool":
            compacted.append(dict(message))
            continue

        tool_call_id = message.get("tool_call_id")
        created_at = _message_created_at(message)
        if (
            tool_call_id in compactable_tool_id_set
            and tool_call_id not in keep_ids
            and created_at is not None
            and newest_timestamp - created_at >= age_cutoff_seconds
        ):
            rewritten = dict(message)
            rewritten["content"] = MICROCOMPACT_PLACEHOLDER
            compacted.append(rewritten)
            continue

        compacted.append(dict(message))

    return compacted


def build_runtime_restore_messages(state: SessionState) -> list[dict[str, Any]]:
    restored: list[dict[str, Any]] = []

    if state.todo_state.items:
        todo_lines = [f"- [{item.status}] {item.active_form}" for item in state.todo_state.items]
        restored.append({
            "role": "meta_runtime_restore",
            "kind": "todo_restore",
            "content": "\n".join(todo_lines),
        })

    if state.invoked_skills:
        skill_lines = [
            f"- {skill_id} (turn {record.invoked_at_turn})"
            for skill_id, record in sorted(
                state.invoked_skills.items(),
                key=lambda pair: pair[1].invoked_at_turn,
            )
        ]
        restored.append({
            "role": "meta_runtime_restore",
            "kind": "skills_restore",
            "content": "\n".join(skill_lines),
        })

    for path, file_state in sorted(
        state.read_file_state.items(),
        key=lambda item: getattr(item[1], "timestamp", 0.0),
        reverse=True,
    )[:3]:
        excerpt = getattr(file_state, "content", "")[:200]
        attrs = [
            f"path={path}",
            f"full_read={str(getattr(file_state, 'is_full_read', True)).lower()}",
        ]
        start_line = getattr(file_state, "offset", None)
        line_limit = getattr(file_state, "limit", None)
        total_lines = getattr(file_state, "total_lines", None)
        if start_line is not None:
            attrs.append(f"start_line={start_line}")
        if start_line is not None and line_limit is not None:
            attrs.append(f"end_line={start_line + line_limit - 1}")
        if total_lines is not None:
            attrs.append(f"total_lines={total_lines}")
        restored.append({
            "role": "meta_runtime_restore",
            "kind": "file_runtime",
            "content": f"{';'.join(attrs)}\n{excerpt}",
        })

    return restored


def summarize_and_compact(
    messages: list[dict[str, Any]],
    *,
    state: SessionState,
    summary_gateway: Any,
    keep_last_messages: int,
) -> list[dict[str, Any]]:
    base_messages = _strip_trailing_runtime_restore(messages)
    keep_from_index = max(0, len(base_messages) - keep_last_messages)
    keep_from_index = _align_keep_start_to_complete_tool_batch(base_messages, keep_from_index)
    request_options = ModelRequestOptions(
        query_source="compact",
        max_output_tokens=1200,
        thinking_mode="disabled",
    )
    try:
        summary_response = summary_gateway.call_once(
            base_messages[:keep_from_index],
            system=SUMMARY_SYSTEM_PROMPT,
            tools=None,
            request_options=request_options,
        )
    except TypeError as exc:
        if "request_options" not in str(exc):
            raise
        summary_response = summary_gateway.call_once(
            base_messages[:keep_from_index],
            system=SUMMARY_SYSTEM_PROMPT,
            tools=None,
        )
    boundary = create_compact_boundary(
        reason="summary_compact",
        summarized_messages=keep_from_index,
    )
    summary = create_compact_summary(summary_response.content.strip())
    kept = base_messages[keep_from_index:]
    runtime_restore = build_runtime_restore_messages(state)
    return build_post_compact_messages(
        boundary=boundary,
        summary=summary,
        kept=kept,
        runtime_restore=runtime_restore,
    )


def _strip_trailing_runtime_restore(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    end = len(messages)
    while end > 0 and messages[end - 1].get("role") == "meta_runtime_restore":
        end -= 1
    return messages[:end]


def _align_keep_start_to_complete_tool_batch(messages: list[dict[str, Any]], keep_from_index: int) -> int:
    if keep_from_index <= 0 or keep_from_index >= len(messages):
        return keep_from_index
    if messages[keep_from_index].get("role") != "tool":
        return keep_from_index

    batch_start = keep_from_index
    while batch_start > 0 and messages[batch_start - 1].get("role") == "tool":
        batch_start -= 1

    if batch_start > 0:
        assistant = messages[batch_start - 1]
        if assistant.get("role") == "assistant" and assistant.get("tool_calls"):
            return batch_start - 1

    return keep_from_index


def _message_created_at(message: dict[str, Any]) -> float | None:
    meta = message.get("_meta")
    if not isinstance(meta, dict):
        return None
    created_at = meta.get("created_at")
    return created_at if isinstance(created_at, (int, float)) else None
