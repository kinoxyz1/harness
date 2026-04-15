from __future__ import annotations

from typing import Any

from .state import SessionState


class SessionStore:
    def __init__(self, state: SessionState):
        self._state = state

    def prepend(self, message: dict[str, Any]) -> None:
        self._state.conversation_messages.insert(0, message)

    def append(self, message: dict[str, Any]) -> None:
        self._state.conversation_messages.append(message)

    def extend(self, messages: list[dict[str, Any]]) -> None:
        self._state.conversation_messages.extend(messages)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._state.conversation_messages)
