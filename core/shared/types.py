from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class UsageDelta:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class MessageBatch:
    messages: list[dict[str, str]] = field(default_factory=list)
