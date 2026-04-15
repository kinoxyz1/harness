"""消息规范化层：将内部消息列表转换为 Anthropic messages 协议格式。

解决三类问题：
1. system 独立抽离为顶层参数
2. 内部 tool_calls / tool role 转换为 Anthropic tool_use / tool_result block
3. 角色交替、未闭合工具调用的修正

内部消息格式（方案 A）：
- assistant: {"role": "assistant", "content": "...", "tool_calls": [...]}
- tool: {"role": "tool", "tool_call_id": "...", "content": "..."}

Anthropic API 格式：
- system: 顶层参数
- messages: 只有 user/assistant，tool_result 嵌入 user.content[]
"""
from __future__ import annotations

from typing import Any


def normalize_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """将内部消息列表规范化为 Anthropic API 格式。

    Returns:
        (system, messages) 二元组。
        system: 合并后的系统提示词（可能为空字符串）。
        messages: 仅包含 user/assistant 角色的 Anthropic 格式消息列表。
    """
    # 1. 分离 system
    system_parts: list[str] = []
    non_system: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if content:
                system_parts.append(content)
        else:
            non_system.append(msg)

    system = "\n\n".join(system_parts)

    # 2. 转换内部消息为 Anthropic 格式
    converted: list[dict[str, Any]] = []
    for msg in non_system:
        role = msg.get("role")
        if role == "user":
            converted.append(_convert_user(msg))
        elif role == "assistant":
            converted.append(_convert_assistant(msg))
        elif role == "tool":
            converted.append(msg)  # 保留原始，后续处理

    # 3. 补齐未闭合的 tool_use
    converted = _pair_tool_results(converted)

    # 4. 把连续 tool 消息合并为 user + tool_result blocks
    converted = _merge_tool_results(converted)

    # 5. 合并连续同角色消息
    converted = _merge_consecutive_roles(converted)

    return system, converted


def _convert_user(msg: dict[str, Any]) -> dict[str, Any]:
    """转换内部 user 消息。"""
    return {"role": "user", "content": msg.get("content", "")}


def _convert_assistant(msg: dict[str, Any]) -> dict[str, Any]:
    """转换内部 assistant 消息，把 tool_calls 转为 tool_use blocks。"""
    content_blocks: list[dict[str, Any]] = []

    text = msg.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    tool_calls = msg.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc.get("args", {}),
            })

    return {
        "role": "assistant",
        "content": content_blocks if content_blocks else "",
    }


def _pair_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确保每个 tool_use block 都有匹配的 tool_result。

    为缺失的 tool_use 插入 (cancelled) 占位 tool 消息。
    仅在列表中存在至少一条 tool 消息时才补齐（否则视为纯转换场景）。
    """
    # 收集已有 tool_result 的 ID，同时判断是否存在 tool 消息
    has_tool_messages = False
    paired_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            has_tool_messages = True
            tc_id = msg.get("tool_call_id")
            if tc_id:
                paired_ids.add(tc_id)

    if not has_tool_messages:
        return messages

    insertions: list[tuple[int, dict[str, Any]]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tc_id = block.get("id")
                if tc_id and tc_id not in paired_ids:
                    insertions.append((
                        i + 1 + len(insertions),
                        {"role": "tool", "tool_call_id": tc_id, "content": "(cancelled)"},
                    ))

    for idx, placeholder in insertions:
        messages.insert(idx, placeholder)

    return messages


def _merge_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把连续的内部 tool 消息聚合为一条 user 消息中的多个 tool_result block。"""
    result: list[dict[str, Any]] = []
    tool_buffer: list[dict[str, Any]] = []

    def flush_buffer():
        if tool_buffer:
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg.get("content", ""),
                }
                for msg in tool_buffer
            ]
            result.append({"role": "user", "content": blocks})
            tool_buffer.clear()

    for msg in messages:
        if msg.get("role") == "tool":
            tool_buffer.append(msg)
        else:
            flush_buffer()
            result.append(msg)

    flush_buffer()
    return result


def _merge_consecutive_roles(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并连续同角色消息（Anthropic 要求 user/assistant 交替）。"""
    if not messages:
        return messages

    merged: list[dict[str, Any]] = [messages[0]]

    for msg in messages[1:]:
        prev = merged[-1]
        if msg["role"] == prev["role"] == "user":
            prev_content = prev.get("content", "")
            msg_content = msg.get("content", "")
            if isinstance(prev_content, str) and isinstance(msg_content, str):
                prev["content"] = prev_content + "\n" + msg_content
            else:
                prev["content"] = _to_blocks(prev_content) + _to_blocks(msg_content)
        elif msg["role"] == prev["role"] == "assistant":
            prev_content = prev.get("content", "")
            msg_content = msg.get("content", "")
            if isinstance(prev_content, list) and isinstance(msg_content, list):
                prev["content"] = prev_content + msg_content
            elif isinstance(prev_content, list):
                prev["content"] = prev_content + [{"type": "text", "text": msg_content or ""}]
            elif isinstance(msg_content, list):
                prev["content"] = [{"type": "text", "text": prev_content or ""}] + msg_content
            else:
                prev["content"] = (prev_content or "") + (msg_content or "")
        else:
            merged.append(msg)

    return merged


def _to_blocks(content: Any) -> list[dict[str, Any]]:
    """将 content 转为 block 列表。"""
    if isinstance(content, list):
        return content
    if isinstance(content, str) and content:
        return [{"type": "text", "text": content}]
    return []
