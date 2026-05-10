from __future__ import annotations

from typing import Any

from core.tasks.planner_runtime import build_task_state
from ..context import RunUpdate, RunUpdateKind, SessionUpdate, SessionUpdateKind, ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext, make_tool_message


SCHEMA: dict[str, Any] = {
    "name": "task_plan",
    "description": (
        "Rewrite the current TaskState for non-trivial multi-step work. "
        "Use this before execution when the request has multiple independent goals, "
        "requires research + implementation + verification, needs subagent dispatch, "
        "or spans multiple files/modules. Submit the full replacement task list each time; "
        "this tool does not do incremental patching. When TaskState is active, do not use todo "
        "as the source of truth. "
        "After completing a task with normal tools, call this again with updated status "
        "(e.g., completed/in_progress) to track progress."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "subject": {"type": "string"},
                        "goal": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "blocked", "completed", "failed", "cancelled"],
                        },
                        "execution_mode": {
                            "type": "string",
                            "enum": ["local", "fresh_subagent", "fork_subagent"],
                        },
                    },
                    "required": ["subject", "goal"],
                },
            }
        },
        "required": ["tasks"],
    },
}

READONLY = False
ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    state = context.session_state
    if state is None:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="no_state",
            messages=[make_tool_message(context, "No session state available")],
        )

    try:
        next_state = build_task_state(
            args.get("tasks", []),
            previous=state.task_state,
            turn_count=context.turn_count,
        )
    except (TypeError, ValueError, KeyError) as exc:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="validation_failed",
            messages=[make_tool_message(context, f"Task planning failed: {exc}")],
        )

    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        session_updates=[
            SessionUpdate(
                kind=SessionUpdateKind.SET_TASK_STATE,
                payload={"task_state": next_state},
            )
        ],
        run_updates=[
            RunUpdate(kind=RunUpdateKind.RESET_TODO_TURN_COUNTER),
        ],
        messages=[make_tool_message(context, f"TaskState rewritten with {len(next_state.ordered_task_ids)} tasks.")],
    )
