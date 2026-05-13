from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Literal


EventOrigin = Literal["model", "tool", "subagent", "system"]
SourceMode = Literal["live", "replayed"]


@dataclass(slots=True)
class StreamEvent:
    type: str
    turn_id: str
    sequence: int
    origin: EventOrigin
    source_mode: SourceMode
    timestamp: float
    payload: dict[str, Any] = field(default_factory=dict)


def make_event(
    type: str,
    turn_id: str,
    sequence: int,
    origin: EventOrigin,
    source_mode: SourceMode,
    payload: dict[str, Any] | None = None,
) -> StreamEvent:
    return StreamEvent(
        type=type,
        turn_id=turn_id,
        sequence=sequence,
        origin=origin,
        source_mode=source_mode,
        timestamp=time.time(),
        payload=dict(payload or {}),
    )


class StreamAccumulator:
    def __init__(self) -> None:
        self._reasoning_parts: list[str] = []
        self._content_parts: list[str] = []
        self._tool_calls: list[dict[str, Any]] = []
        self._finish_reason = "end_turn"
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._reasoning_signature = ""
        self._completed = False

    def consume(self, event: StreamEvent) -> None:
        if event.type == "thinking_delta":
            self._reasoning_parts.append(str(event.payload.get("text", "")))
        elif event.type == "content_delta":
            self._content_parts.append(str(event.payload.get("text", "")))
        elif event.type == "tool_call_ready":
            tool_call = event.payload.get("tool_call")
            if tool_call is not None:
                self._tool_calls.append(dict(tool_call))
        elif event.type == "response_completed":
            self._finish_reason = str(event.payload.get("finish_reason", "end_turn"))
            self._prompt_tokens = int(event.payload.get("prompt_tokens", 0))
            self._completion_tokens = int(event.payload.get("completion_tokens", 0))
            self._reasoning_signature = str(event.payload.get("reasoning_signature", ""))
            self._completed = True

    def snapshot(self) -> dict[str, Any]:
        return {
            "reasoning": "".join(self._reasoning_parts),
            "content": "".join(self._content_parts),
            "tool_calls": list(self._tool_calls),
            "completed": self._completed,
        }

    def finalize(self):
        from core.llm.response import ModelResponse

        if not self._completed:
            raise RuntimeError("response_completed event is required before finalize()")
        return ModelResponse(
            content="".join(self._content_parts),
            tool_calls=list(self._tool_calls),
            finish_reason=self._finish_reason,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            reasoning="".join(self._reasoning_parts),
            reasoning_signature=self._reasoning_signature,
        )
