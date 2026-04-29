from __future__ import annotations

from typing import Any


def repair_tool_result_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair tool_use/tool_result pairing on provider-adjacent messages."""
    seen_tool_use_ids: set[str] = set()
    sanitized: list[tuple[dict[str, Any], list[str]]] = []

    for msg in messages:
        if msg.get("role") != "assistant":
            sanitized.append((dict(msg), []))
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            sanitized.append((dict(msg), []))
            continue

        cleaned_content: list[Any] = []
        tool_use_ids: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                cleaned_content.append(block)
                continue

            tool_use_id = block.get("id")
            if not tool_use_id or tool_use_id in seen_tool_use_ids:
                continue

            seen_tool_use_ids.add(tool_use_id)
            tool_use_ids.append(tool_use_id)
            cleaned_content.append(dict(block))

        if not cleaned_content:
            continue

        sanitized_msg = dict(msg)
        sanitized_msg["content"] = cleaned_content
        sanitized.append((sanitized_msg, tool_use_ids))

    matched_tool_result_ids: set[str] = set()
    for msg, _ in sanitized:
        if msg.get("role") != "tool":
            continue

        tool_call_id = msg.get("tool_call_id")
        if tool_call_id and tool_call_id in seen_tool_use_ids:
            matched_tool_result_ids.add(tool_call_id)

    repaired: list[dict[str, Any]] = []
    kept_tool_result_ids: set[str] = set()
    for msg, tool_use_ids in sanitized:
        if msg.get("role") != "tool":
            repaired.append(msg)
        else:
            tool_call_id = msg.get("tool_call_id")
            if not tool_call_id or tool_call_id not in seen_tool_use_ids:
                continue
            if tool_call_id in kept_tool_result_ids:
                continue
            kept_tool_result_ids.add(tool_call_id)
            repaired.append(msg)

        for tool_use_id in tool_use_ids:
            if tool_use_id in matched_tool_result_ids:
                continue
            repaired.append({
                "role": "tool",
                "tool_call_id": tool_use_id,
                "content": "(cancelled)",
            })

    return repaired
