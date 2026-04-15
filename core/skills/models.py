from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(slots=True)
class SkillReference:
    path: str
    purpose: str | None
    abs_path: Path
    prompt_path: str


@dataclass(slots=True)
class SkillMeta:
    skill_id: str
    name: str
    description: str
    when_to_use: str | None
    skill_dir: Path
    skill_file: Path
    references: list[SkillReference] = field(default_factory=list)


@dataclass(slots=True)
class SkillContent:
    meta: SkillMeta
    body: str
    content_digest: str
    reference_bodies: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ActiveSkillState:
    skill_id: str
    activated_at_message_index: int
    source: str
    content_digest: str


@dataclass(slots=True)
class InvokedSkillRecord:
    skill_id: str
    skill_path: str
    content_digest: str
    content: str
    invoked_at_turn: int


@dataclass(slots=True)
class SkillEvent:
    skill_id: str
    action: Literal["activated", "deactivated", "reload"]
    source: str
    conversation_index: int
