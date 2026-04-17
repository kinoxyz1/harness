from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.session.state import TodoItem


@dataclass(slots=True)
class RunState:
    turn_count: int = 0
    empty_retry_count: int = 0
    stop_reason: str | None = None
    last_model_response: Any | None = None
    tool_calls_executed: int = 0
    files_modified: list[str] = field(default_factory=list)
    usage_delta: dict[str, int] = field(default_factory=dict)
    allowed_tools_override: set[str] | None = None
    model_override: str | None = None
    effort_override: str | None = None
    barrier_reason: str | None = None
    todo_replan_required: bool = False
    todo_replan_reason: str | None = None
    assistant_turns_since_todo: int = 0
    # Snapshot used for display diffing. Callers should assign a copied list
    # when updating this field to avoid aliasing with mutable todo state.
    last_displayed_todo_items: list["TodoItem"] | None = None
