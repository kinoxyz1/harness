"""消息存储 — conversation_messages 的唯一写入入口。

你在数据流中的位置：
    QueryLoop.run()
      → store.append(model_resp.to_message())   ← 你在这里（追加 assistant 消息）
      → store.extend(batch.messages)            ← 追加工具执行结果
      → store.extend(before_messages)            ← 追加策略注入的消息

设计约束：conversation_messages 的变更都通过 SessionStore。
常规路径是 prepend / append / extend，compact rewrite 等场景可以通过
replace_working_transcript() 原位替换 working transcript，
确保只有一个入口，防止组件直接操作列表导致状态不一致。

注意：MessageViewBuilder 只读取 conversation_messages，不写入。
它通过 _select_transcript_slice 创建副本（slice），不修改原始数据。
"""
from __future__ import annotations

import time
from typing import Any

from .state import SessionState


class SessionStore:
    """conversation_messages 的写入门面。"""

    def __init__(self, state: SessionState):
        self._state = state

    def _stamp(self, message: dict[str, Any]) -> dict[str, Any]:
        meta = dict(message.get("_meta", {}))
        meta.setdefault("created_at", time.time())
        return {**message, "_meta": meta}

    def prepend(self, message: dict[str, Any]) -> None:
        """在对话开头插入消息（用于子代理恢复等场景）。"""
        self._state.conversation_messages.insert(0, self._stamp(message))

    def append(self, message: dict[str, Any]) -> None:
        """在对话末尾追加一条消息。"""
        self._state.conversation_messages.append(self._stamp(message))

    def extend(self, messages: list[dict[str, Any]]) -> None:
        """在对话末尾追加多条消息。"""
        self._state.conversation_messages.extend(self._stamp(message) for message in messages)

    def replace_working_transcript(self, messages: list[dict[str, Any]]) -> None:
        """用新的 working transcript 替换当前内存中的对话消息。"""
        self._state.conversation_messages[:] = [self._stamp(message) for message in messages]

    def snapshot(self) -> list[dict[str, Any]]:
        """返回对话历史的浅拷贝。"""
        return list(self._state.conversation_messages)
