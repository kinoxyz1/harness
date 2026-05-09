from __future__ import annotations

MICROCOMPACT_PLACEHOLDER = "[Old tool result content cleared]"
COMPACTABLE_TOOLS = {"bash", "find", "grep", "glob", "web_fetch", "web_search", "write_file"}


def apply_time_based_microcompact(
    messages: list[dict[str, object]],
    *,
    age_cutoff_seconds: float,
    keep_recent_trajectories: int,
) -> list[dict[str, object]]:
    newest_timestamp = max(
        (
            message["_meta"]["created_at"]
            for message in messages
            if isinstance(message.get("_meta"), dict) and isinstance(message["_meta"].get("created_at"), (int, float))
        ),
        default=None,
    )
    if newest_timestamp is None:
        return [dict(message) for message in messages]

    tool_name_by_id = _build_tool_name_map(messages)
    compactable_ids = [tool_id for tool_id, tool_name in tool_name_by_id.items() if tool_name in COMPACTABLE_TOOLS]
    keep_ids = set(compactable_ids[-keep_recent_trajectories:]) if keep_recent_trajectories > 0 else set()

    compacted: list[dict[str, object]] = []
    for message in messages:
        message_copy = dict(message)
        if message_copy.get("role") != "tool":
            compacted.append(message_copy)
            continue
        tool_use_id = str(message_copy.get("tool_call_id", ""))
        created_at = _message_created_at(message_copy)
        if (
            tool_use_id in compactable_ids
            and tool_use_id not in keep_ids
            and created_at is not None
            and newest_timestamp - created_at >= age_cutoff_seconds
        ):
            message_copy["content"] = MICROCOMPACT_PLACEHOLDER
        compacted.append(message_copy)
    return compacted


def _build_tool_name_map(messages: list[dict[str, object]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict) and tool_call.get("id") and tool_call.get("name"):
                mapping[str(tool_call["id"])] = str(tool_call["name"])
    return mapping


def _message_created_at(message: dict[str, object]) -> float | None:
    meta = message.get("_meta")
    if not isinstance(meta, dict):
        return None
    created_at = meta.get("created_at")
    return created_at if isinstance(created_at, (int, float)) else None
