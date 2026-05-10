from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.tasks.models import TaskExecutionMode, TaskState, TaskStatus
from ..context import SessionUpdate, SessionUpdateKind, ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext, make_tool_message


SCHEMA: dict[str, Any] = {
    "name": "task_execute",
    "description": "Execute a planned TaskState task by task_id. Use this only after task_plan has created TaskState.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
    },
}

READONLY = False
ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": False, "concurrency_safe": False}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    from core.session.subagent import SubagentRequest, SubagentRuntime, SubagentType
    from core.tasks.dispatcher import compile_task_packet, normalize_subagent_result

    state = context.session_state
    if state is None or not state.task_state.tasks_by_id:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="missing_task_state",
            messages=[make_tool_message(context, "No TaskState available. Call `task_plan` first.")],
        )

    task_id = args["task_id"]
    task = state.task_state.tasks_by_id.get(task_id)
    if task is None:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="not_found",
            messages=[make_tool_message(context, f"Unknown task: {task_id}")],
        )

    if task.execution_mode == TaskExecutionMode.LOCAL:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="local_task",
            messages=[make_tool_message(context, "This task is marked local. Execute it in the main thread with normal tools.")],
        )
    if task.execution_mode != TaskExecutionMode.FRESH_SUBAGENT:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="unsupported_execution_mode",
            messages=[make_tool_message(context, f"Unsupported execution_mode for task_execute: {task.execution_mode}")],
        )

    packet = compile_task_packet(task)
    runtime = SubagentRuntime(parent_context=context)
    sub_result = runtime.run(
        SubagentRequest(
            task_packet=packet,
            agent_type=SubagentType.GENERAL,
            preloaded_skill_ids=list(task.required_skills),
        )
    )
    normalized = normalize_subagent_result(task.task_id, sub_result)

    next_task = replace(
        task,
        packet_revision=packet.packet_revision,
        status=normalized.status,
        result_summary=normalized.summary,
        files_modified=list(normalized.files_modified),
        failure_reason=normalized.failure_reason,
    )

    next_state = TaskState(
        tasks_by_id={**state.task_state.tasks_by_id, task.task_id: next_task},
        ordered_task_ids=list(state.task_state.ordered_task_ids),
        current_task_id=task.task_id,
        last_planned_turn=state.task_state.last_planned_turn,
    )

    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        session_updates=[SessionUpdate(kind=SessionUpdateKind.SET_TASK_STATE, payload={"task_state": next_state})],
        messages=[make_tool_message(context, normalized.summary)],
    )
