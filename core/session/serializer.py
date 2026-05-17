from __future__ import annotations

from dataclasses import asdict
from typing import Any

from core.session.content_replacement import ContentReplacementState
from core.session.state import DispatchRunRecord, DispatchState, SessionState, TodoItem, TodoState
from core.skills.models import InvokedSkillRecord, SkillEvent
from core.tasks.models import TaskRecord, TaskState
from core.tools.context import FileState


def _normalize_dispatch_state(raw: dict[str, Any]) -> DispatchState:
    runs_by_id = {
        run_id: DispatchRunRecord(**payload)
        for run_id, payload in raw.get("runs_by_id", {}).items()
    }
    active_run_id = raw.get("active_run_id")
    for record in runs_by_id.values():
        if record.status == "running":
            record.status = "cancelled"
            record.stop_reason = "cancelled"
            record.error_detail = "cancelled during session restore"
    if active_run_id and active_run_id in runs_by_id:
        active_run_id = None
    return DispatchState(
        runs_by_id=runs_by_id,
        ordered_run_ids=list(raw.get("ordered_run_ids", [])),
        active_run_id=active_run_id,
    )


class SessionSerializer:
    SKIP_FIELDS = {
        "prompt_cache",
        "memory_store",
        "session_db",
        "memory_provider",
        "skill_catalog",
        "skills_revision",
        "discovered_tools",
        "background_manager",
    }

    @classmethod
    def serialize(cls, state: SessionState) -> dict[str, Any]:
        data = asdict(state)
        for key in cls.SKIP_FIELDS:
            data.pop(key, None)
        return data

    @classmethod
    def deserialize(cls, data: dict[str, Any]) -> SessionState:
        state = SessionState(
            conversation_messages=list(data.get("conversation_messages", [])),
            session_id=str(data.get("session_id", "")) or None,
        )
        if not state.session_id:
            state.session_id = SessionState(conversation_messages=[]).session_id

        state.system_prompt_override = data.get("system_prompt_override")
        state.session_metadata = dict(data.get("session_metadata", {}))
        state.usage_totals = dict(data.get("usage_totals", {}))
        state.user_intents = list(data.get("user_intents", []))
        state.compact_state = dict(data.get("compact_state", state.compact_state))
        state.queries_since_skill_activation = int(data.get("queries_since_skill_activation", 0))
        state.last_known_skill_keys = set(data.get("last_known_skill_keys", []))
        state.skill_relevance_cooldown = dict(data.get("skill_relevance_cooldown", {}))
        state.user_turn_count = int(data.get("user_turn_count", 0))
        state.turns_since_memory_review = int(data.get("turns_since_memory_review", 0))
        state.memory_review_interval = int(data.get("memory_review_interval", state.memory_review_interval))
        state._last_flushed_idx = int(data.get("_last_flushed_idx", 0))

        todo_raw = data.get("todo_state", {})
        state.todo_state = TodoState(
            items=[TodoItem(**item) for item in todo_raw.get("items", [])],
            last_completed_items=[TodoItem(**item) for item in todo_raw.get("last_completed_items", [])],
            last_write_turn=todo_raw.get("last_write_turn"),
            last_reminder_turn=todo_raw.get("last_reminder_turn"),
        )

        task_raw = data.get("task_state", {})
        tasks_by_id = {
            task_id: TaskRecord(**task_dict)
            for task_id, task_dict in task_raw.get("tasks_by_id", {}).items()
        }
        state.task_state = TaskState(
            tasks_by_id=tasks_by_id,
            ordered_task_ids=list(task_raw.get("ordered_task_ids", [])),
            last_planned_turn=task_raw.get("last_planned_turn"),
            last_projection_turn=task_raw.get("last_projection_turn"),
            current_task_id=task_raw.get("current_task_id"),
        )

        state.invoked_skills = {
            skill_id: InvokedSkillRecord(**record)
            for skill_id, record in data.get("invoked_skills", {}).items()
        }
        state.skill_events = [SkillEvent(**event) for event in data.get("skill_events", [])]
        state.read_file_state = {
            path: FileState(**raw)
            for path, raw in data.get("read_file_state", {}).items()
        }

        replacement_raw = data.get("content_replacement_state", {})
        state.content_replacement_state = ContentReplacementState(
            seen_ids=set(replacement_raw.get("seen_ids", [])),
            replacements=dict(replacement_raw.get("replacements", {})),
        )

        dispatch_raw = data.get("dispatch_state", {})
        state.dispatch_state = _normalize_dispatch_state(dispatch_raw)
        return state
