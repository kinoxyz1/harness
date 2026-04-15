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

    def build(self, state: SessionState, run_state=None) -> MessageView:
        messages = list(state.conversation_messages)
        tools = self._tools
        if run_state is not None and run_state.allowed_tools_override is not None and tools is not None:
            tools = [tool for tool in tools if tool.get("name") in run_state.allowed_tools_override]
        return MessageView(messages=messages, tools=tools)
