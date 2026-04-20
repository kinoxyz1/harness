from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..shared.config import MAX_REASONING_CHARS, PERSIST_THINKING


@dataclass(slots=True)
class ModelResponse:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning: str = ""
    reasoning_signature: str = ""

    @property
    def has_final_text(self) -> bool:
        return bool(self.content.strip())

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        if PERSIST_THINKING and self.reasoning and self.reasoning.strip():
            truncated = self.reasoning[:MAX_REASONING_CHARS]
            message["reasoning"] = truncated
            if self.reasoning_signature:
                message["reasoning_signature"] = self.reasoning_signature
        return message
