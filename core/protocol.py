"""消息规范化层：将内部消息列表转换为 API 可接受的格式。

解决三类问题：
1. 内部字段泄漏（reasoning_content 等不应发送到 API）
2. tool_call / tool_result 配对缺失（如 MAX_TURNS 截断后缺少 result）
3. 角色交替违反（连续同角色消息）

参考：learn-claude-code s02 消息规范化。
"""
from __future__ import annotations

from typing import Any

# API 不接受的字段黑名单
_STRIP_FIELDS = {"reasoning_content", "refusal", "annotations"}


def normalize_messages(messages: list[dict[str, Any]], enable_thinking: bool = False) -> list[dict[str, Any]]:
    """将内部消息列表规范化为 OpenAI 兼容 API 可接受的格式。

    原则：messages 是系统的内部表示，API 看到的是规范化后的副本。
    两者不是同一个东西。

    Args:
        messages: 内部消息列表
        enable_thinking: 是否保留 reasoning_content（思考模型如 kimi 需要）

    Returns:
        全新的消息列表，不修改原始 messages。
    """
    result: list[dict[str, Any]] = []

    for msg in messages:
        clean = _clean_message(msg, enable_thinking)
        if clean:
            result.append(clean)

    # 确保 tool_call / tool_result 配对
    result = _pair_tool_results(result)

    # 合并连续同角色消息
    result = _merge_consecutive_roles(result)

    return result


def _clean_message(msg: dict[str, Any], enable_thinking: bool = False) -> dict[str, Any] | None:
    """清洗单条消息：只保留协议认可的字段。"""
    role = msg.get("role")
    if role not in ("system", "user", "assistant", "tool"):
        return None

    clean: dict[str, Any] = {"role": role}

    if role == "system":
        content = msg.get("content")
        if content:
            clean["content"] = content
        else:
            return None  # 空 system 消息无意义

    elif role == "user":
        content = msg.get("content")
        if content:
            clean["content"] = content
        else:
            return None  # 空 user 消息会破坏角色交替

    elif role == "assistant":
        content = msg.get("content")
        if content is not None:
            clean["content"] = content
        # 保留 tool_calls（即使 content 为空，有 tool_calls 就有意义）
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            clean["tool_calls"] = [
                {k: v for k, v in tc.items() if k not in _STRIP_FIELDS}
                for tc in tool_calls
            ]
        # 思考模型（kimi-k2-thinking 等）要求：
        # assistant 消息带 tool_calls 时必须有 reasoning_content 字段
        if enable_thinking:
            reasoning = msg.get("reasoning_content")
            if reasoning is not None:
                clean["reasoning_content"] = reasoning
            elif tool_calls:
                # kimi 要求非 null，用空字符串占位
                clean["reasoning_content"] = ""
        # 既没有 content 也没有 tool_calls → 空消息，跳过
        if "content" not in clean and "tool_calls" not in clean:
            return None

    elif role == "tool":
        tool_call_id = msg.get("tool_call_id")
        if not tool_call_id:
            return None  # 没有 tool_call_id 的 tool 消息无效
        clean["tool_call_id"] = tool_call_id
        clean["content"] = msg.get("content") or ""

    return clean


def _pair_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确保每个 tool_call 都有匹配的 tool_result。

    遍历所有 assistant 消息中的 tool_calls，收集已有 tool_result 的 ID，
    为缺失的 ID 插入占位 result。
    """
    # 收集已有 tool_result 的 ID
    paired_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id:
                paired_ids.add(tc_id)

    # 找出缺失的 tool_call，记录需要插入的位置
    insertions: list[tuple[int, dict[str, Any]]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            continue
        for tc in tool_calls:
            tc_id = tc.get("id")
            if tc_id and tc_id not in paired_ids:
                # 在 assistant 消息之后插入占位 result
                insertions.append((
                    i + 1 + len(insertions),  # 补偿之前插入的偏移
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": "(cancelled)",
                    },
                ))

    for idx, placeholder in insertions:
        messages.insert(idx, placeholder)

    return messages


def _merge_consecutive_roles(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并连续同角色消息。

    OpenAI 要求 user/assistant 严格交替（tool 消息可连续）。
    连续 user 消息合并内容；连续 assistant 消息合并内容和 tool_calls。
    """
    if not messages:
        return messages

    merged: list[dict[str, Any]] = [messages[0]]

    for msg in messages[1:]:
        prev = merged[-1]

        # tool 消息可连续出现（多个 tool_result）
        if msg["role"] == "tool":
            merged.append(msg)
            continue

        # 同角色合并
        if msg["role"] == prev["role"]:
            if msg["role"] == "user":
                prev["content"] = (prev.get("content") or "") + "\n" + (msg.get("content") or "")
            elif msg["role"] == "assistant":
                # 合并 content
                if msg.get("content"):
                    prev["content"] = (prev.get("content") or "") + (msg.get("content") or "")
                # 合并 tool_calls
                if msg.get("tool_calls"):
                    prev.setdefault("tool_calls", []).extend(msg["tool_calls"])
            # system 角色不应连续，保留第一个
        else:
            merged.append(msg)

    return merged
