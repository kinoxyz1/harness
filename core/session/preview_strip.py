from __future__ import annotations

PERSISTED_OUTPUT_PREFIX = "Full output saved to: "


def strip_persisted_output_previews(
    messages: list[dict[str, object]],
    replacements: dict[str, str],
) -> tuple[list[dict[str, object]], bool]:
    changed = False
    rewritten: list[dict[str, object]] = []
    for message in messages:
        message_copy = dict(message)
        content = message_copy.get("content")
        if not isinstance(content, str) or "<persisted-output>" not in content:
            rewritten.append(message_copy)
            continue
        filepath = _extract_filepath(content)
        collapsed = f"[Tool result offloaded to: {filepath}]"
        message_copy["content"] = collapsed
        tool_use_id = message_copy.get("tool_call_id")
        if isinstance(tool_use_id, str):
            replacements[tool_use_id] = collapsed
        rewritten.append(message_copy)
        changed = True
    return rewritten, changed


def _extract_filepath(content: str) -> str:
    for line in content.splitlines():
        if "Full output saved to:" in line:
            return line.split("Full output saved to:", 1)[1].strip()
    raise ValueError("persisted-output message missing saved path")
