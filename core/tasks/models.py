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
    FORK_SUBAGENT = "fork_subagent"


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    subject: str
    goal: str
    status: TaskStatus
    execution_mode: TaskExecutionMode
    parent_todo_id: str | None = None
    active_form: str | None = None
    agent_type: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    known_context: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    expected_output: list[str] = field(default_factory=list)
    done_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    owner: str | None = None
    blocked_reason: str | None = None
    result_summary: str | None = None
    artifacts: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    packet_revision: int = 0
    created_at_turn: int = 0
    updated_at_turn: int = 0


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
    mode: TaskExecutionMode
    agent_type: str | None
    title: str
    directive: str
    task_context: list[str] = field(default_factory=list)
    known_facts: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    expected_output: list[str] = field(default_factory=list)
    done_criteria: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    packet_revision: int = 0


@dataclass(slots=True)
class TaskRunResult:
    task_id: str
    success: bool
    status: TaskStatus
    summary: str
    artifacts: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    recommended_next_steps: list[str] = field(default_factory=list)
    failure_reason: str | None = None
