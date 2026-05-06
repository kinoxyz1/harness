from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ContextBlock:
    kind: str
    content: str
    required: bool
    token_estimate: int


@dataclass(slots=True)
class PreparedQueryContext:
    stable_system: str
    stable_tools: list[dict[str, Any]] | None
    runtime_blocks: list[ContextBlock]
    working_transcript: list[dict[str, Any]]
    observability: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, int] = field(default_factory=dict)
