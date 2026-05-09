from __future__ import annotations


def read_tool_ids_to_protect(
    messages: list[dict[str, object]],
    *,
    keep_last_reads: int,
) -> set[str]:
    read_ids: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict) and tool_call.get("name") == "read_file":
                read_ids.append(str(tool_call["id"]))
    if keep_last_reads <= 0:
        return set()
    return set(read_ids[-keep_last_reads:])


def collect_recent_read_restore_messages(
    state,
    *,
    kept_messages: list[dict[str, object]],
    limit: int,
) -> list[dict[str, str]]:
    kept_contents = {
        str(message.get("content", ""))
        for message in kept_messages
        if message.get("role") == "tool"
    }
    restored: list[dict[str, str]] = []
    for path, file_state in sorted(
        state.read_file_state.items(),
        key=lambda item: getattr(item[1], "timestamp", 0.0),
        reverse=True,
    ):
        excerpt = getattr(file_state, "content", "")[:200]
        if excerpt in kept_contents:
            continue
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
        restored.append(
            {
                "role": "meta_runtime_restore",
                "kind": "file_runtime",
                "content": f"{';'.join(attrs)}\n{excerpt}",
            }
        )
        if len(restored) >= limit:
            break
    return restored
