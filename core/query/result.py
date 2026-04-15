from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class StopReason(StrEnum):
    COMPLETED = "completed"
    EMPTY_RESPONSE = "empty_response"
    MAX_TURNS = "max_turns"
    API_ERROR = "api_error"
    ABORTED = "aborted"


@dataclass(slots=True)
class QueryResult:
    final_output: str
    stop_reason: StopReason
    success: bool = True
    turns_used: int = 0
    assistant_messages_added: int = 0
    tool_calls_executed: int = 0
    files_modified: list[str] = field(default_factory=list)
    usage_delta: dict[str, int] = field(default_factory=dict)
