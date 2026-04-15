from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SessionState:
    conversation_messages: list[dict[str, Any]]
    prompt_cache: dict[str, str] = field(default_factory=dict)
    discovered_tools: set[str] = field(default_factory=set)
    discovered_skills: set[str] = field(default_factory=set)
    read_file_state: dict[str, Any] = field(default_factory=dict)
    session_metadata: dict[str, Any] = field(default_factory=dict)
    usage_totals: dict[str, int] = field(default_factory=dict)
