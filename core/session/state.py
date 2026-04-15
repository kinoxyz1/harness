from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.skills import ActiveSkillState, SkillEvent, SkillMeta
from core.skills.models import InvokedSkillRecord


@dataclass(slots=True)
class TodoItem:
    content: str
    active_form: str
    status: str
    workflow_ref: str | None = None


@dataclass(slots=True)
class TodoState:
    items: list[TodoItem] = field(default_factory=list)
    last_completed_items: list[TodoItem] = field(default_factory=list)
    last_write_turn: int | None = None
    last_reminder_turn: int | None = None


@dataclass(slots=True)
class SessionState:
    conversation_messages: list[dict[str, Any]]
    prompt_cache: dict[str, str] = field(default_factory=dict)
    discovered_tools: set[str] = field(default_factory=set)
    skill_catalog: dict[str, SkillMeta] = field(default_factory=dict)
    active_skills: dict[str, ActiveSkillState] = field(default_factory=dict)  # deprecated compatibility field; Phase 1 main path no longer reads/writes this
    skill_events: list[SkillEvent] = field(default_factory=list)
    invoked_skills: dict[str, InvokedSkillRecord] = field(default_factory=dict)
    skills_revision: str | None = None
    read_file_state: dict[str, Any] = field(default_factory=dict)
    session_metadata: dict[str, Any] = field(default_factory=dict)
    usage_totals: dict[str, int] = field(default_factory=dict)
    todo_state: TodoState = field(default_factory=TodoState)
