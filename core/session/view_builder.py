from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .state import SessionState


@dataclass(slots=True)
class MessageView:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None


class MessageViewBuilder:
    def __init__(self, tools: list[dict[str, Any]] | None = None):
        self._tools = tools

    def build(self, state: SessionState) -> MessageView:
        return MessageView(messages=list(state.conversation_messages), tools=self._tools)
