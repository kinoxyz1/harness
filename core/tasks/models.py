from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskExecutionMode(str, Enum):
    LOCAL = "local"
    FRESH_SUBAGENT = "fresh_subagent"


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    subject: str
    goal: str
    status: TaskStatus
    execution_mode: TaskExecutionMode
    agent_type: str | None = None
    description: str | None = None
    done_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    required_skill_ids: list[str] = field(default_factory=list)
    result_summary: str | None = None
    files_modified: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    created_at_turn: int = 0
    turns_used: int = 0


@dataclass(slots=True)
class TaskState:
    tasks_by_id: dict[str, TaskRecord] = field(default_factory=dict)
    ordered_task_ids: list[str] = field(default_factory=list)
    last_planned_turn: int | None = None
    last_projection_turn: int | None = None
    current_task_id: str | None = None


@dataclass(slots=True)
class TaskPacket:
    task_id: str
    title: str
    directive: str
    done_criteria: list[str] = field(default_factory=list)
    agent_type: str | None = None
    required_skill_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TaskRunResult:
    task_id: str
    success: bool
    status: TaskStatus
    summary: str
    files_modified: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    turns_used: int = 0
