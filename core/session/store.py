"""消息存储 — conversation_messages 的唯一写入入口。

你在数据流中的位置：
    QueryLoop.run()
      → store.append(model_resp.to_message())   ← 你在这里（追加 assistant 消息）
      → store.extend(batch.tool_results)        ← 追加工具执行结果
      → store.extend(before_messages)            ← 追加策略注入的消息

设计约束：conversation_messages 是 append-only。
所有对 conversation_messages 的写入都通过 SessionStore，
确保只有一个入口，防止组件直接操作列表导致状态不一致。

注意：MessageViewBuilder 只读取 conversation_messages，不写入。
它通过 _select_transcript_slice 创建副本（slice），不修改原始数据。
"""
from __future__ import annotations

from typing import Any

from .state import SessionState


class SessionStore:
    """conversation_messages 的写入门面。"""

    def __init__(self, state: SessionState):
        self._state = state

    def prepend(self, message: dict[str, Any]) -> None:
        """在对话开头插入消息（用于子代理恢复等场景）。"""
        self._state.conversation_messages.insert(0, message)

    def append(self, message: dict[str, Any]) -> None:
        """在对话末尾追加一条消息。"""
        self._state.conversation_messages.append(message)

    def extend(self, messages: list[dict[str, Any]]) -> None:
        """在对话末尾追加多条消息。"""
        self._state.conversation_messages.extend(messages)

    def snapshot(self) -> list[dict[str, Any]]:
        """返回对话历史的浅拷贝。"""
        return list(self._state.conversation_messages)
