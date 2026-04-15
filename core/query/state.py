from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RunState:
    turn_count: int = 0
    empty_retry_count: int = 0
    stop_reason: str | None = None
    last_model_response: Any | None = None
    tool_calls_executed: int = 0
    files_modified: list[str] = field(default_factory=list)
    usage_delta: dict[str, int] = field(default_factory=dict)
