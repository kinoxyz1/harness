from __future__ import annotations

import os
from enum import Enum

from core.tools.context import RunUpdate, RunUpdateKind, SessionUpdate, SessionUpdateKind


class TransitionReason(str, Enum):
    NEXT_TURN = "next_turn"
    MAX_TURNS_RECOVERY = "max_turns_recovery"
    EMPTY_RESPONSE_RETRY = "empty_response_retry"
    MAX_TOKENS_RECOVERY = "max_tokens_recovery"


def apply_session_update(session_state, update: SessionUpdate) -> None:
    payload = update.payload

    if update.kind == SessionUpdateKind.INVOKE_SKILL:
        invoked_skill = payload.get("invoked_skill")
        if invoked_skill is not None:
            session_state.invoked_skills[invoked_skill.skill_id] = invoked_skill
        return
    if update.kind == SessionUpdateKind.SET_TODO_ITEMS:
        items = list(payload.get("items") or [])
        all_completed = bool(items) and all(item.status == "completed" for item in items)
        session_state.todo_state.items = [] if all_completed else items
        session_state.todo_state.last_completed_items = items if all_completed else []
        session_state.todo_state.last_write_turn = payload.get("last_write_turn")
        return
    if update.kind == SessionUpdateKind.UPSERT_FILE_STATE:
        path = payload.get("path")
        file_state = payload.get("file_state")
        if path and file_state is not None:
            session_state.read_file_state[path] = file_state
        return
    if update.kind == SessionUpdateKind.INVALIDATE_FILE_STATE:
        path = payload.get("path")
        if path:
            session_state.read_file_state.pop(path, None)
        return
    if update.kind == SessionUpdateKind.APPEND_SKILL_EVENT:
        skill_event = payload.get("skill_event")
        if skill_event is not None:
            session_state.skill_events.append(skill_event)
        return

    raise ValueError(f"Unsupported session update kind: {update.kind}")


def apply_run_update(run_state, update: RunUpdate) -> None:
    payload = update.payload

    if update.kind == RunUpdateKind.MARK_FILE_MODIFIED:
        path = payload.get("path")
        if path and path not in run_state.files_modified:
            run_state.files_modified.append(path)
        return
    if update.kind == RunUpdateKind.NARROW_ALLOWED_TOOLS:
        allowed_tools = payload.get("allowed_tools")
        if allowed_tools is None:
            return
        normalized_allowed_tools = set(allowed_tools)
        run_state.allowed_tools_override = (
            normalized_allowed_tools
            if run_state.allowed_tools_override is None
            else run_state.allowed_tools_override & normalized_allowed_tools
        )
        return
    if update.kind == RunUpdateKind.SET_MODEL_OVERRIDE:
        run_state.model_override = payload.get("model_override")
        return
    if update.kind == RunUpdateKind.SET_EFFORT_OVERRIDE:
        run_state.effort_override = payload.get("effort_override")
        return
    if update.kind == RunUpdateKind.RESET_TODO_TURN_COUNTER:
        run_state.assistant_turns_since_todo = 0
        return

    raise ValueError(f"Unsupported run update kind: {update.kind}")


def apply_transition(run_state, reason: TransitionReason) -> None:
    run_state.transition = reason
    if reason == TransitionReason.NEXT_TURN:
        run_state.empty_retry_count = 0
    elif reason == TransitionReason.EMPTY_RESPONSE_RETRY:
        run_state.empty_retry_count += 1
    elif reason == TransitionReason.MAX_TOKENS_RECOVERY:
        run_state.empty_retry_count = 0


def collect_runtime_maintenance_updates(session_state) -> list[SessionUpdate]:
    updates: list[SessionUpdate] = []
    for path, cached in session_state.read_file_state.items():
        cached_mtime = getattr(cached, "timestamp", None)
        try:
            actual_mtime = os.path.getmtime(path)
        except OSError:
            actual_mtime = None

        if cached_mtime != actual_mtime:
            updates.append(
                SessionUpdate(
                    kind=SessionUpdateKind.INVALIDATE_FILE_STATE,
                    payload={"path": path},
                )
            )
    return updates
