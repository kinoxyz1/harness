from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.tasks.models import TaskExecutionMode, TaskState, TaskStatus
from ..context import SessionUpdate, SessionUpdateKind, ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext, make_tool_message


SCHEMA: dict[str, Any] = {
    "name": "task_execute",
    "description": (
        "Execute a planned TaskState task by task_id via a fresh subagent. "
        "ONLY use this for tasks with execution_mode='fresh_subagent'. "
        "For execution_mode='local' tasks, execute them inline with normal tools (bash, edit_file, etc.) "
        "and then call task_plan to update the task status."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
    },
}

READONLY = False
ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": False, "concurrency_safe": True}


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
            messages=[make_tool_message(context, "LOCAL tasks are executed inline with normal tools (bash, edit_file, etc.), not via task_execute. Use task_plan to update status after completion.")],
        )
    if task.execution_mode != TaskExecutionMode.FRESH_SUBAGENT:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="unsupported_execution_mode",
            messages=[make_tool_message(context, f"Unsupported execution_mode for task_execute: {task.execution_mode}")],
        )

    # Dependency checking
    for dep_id in task.depends_on:
        dep = state.task_state.tasks_by_id.get(dep_id)
        if dep is None:
            return ToolInvocationOutcome(
                status=ToolOutcomeStatus.FAILURE,
                error="missing_dependency",
                messages=[make_tool_message(context, f"Missing dependency: {dep_id}")],
            )
        if dep.status != TaskStatus.COMPLETED:
            return ToolInvocationOutcome(
                status=ToolOutcomeStatus.FAILURE,
                error="dependency_not_ready",
                messages=[make_tool_message(context, f"Dependency not completed: {dep_id}")],
            )

    # Compile packet and run subagent
    packet = compile_task_packet(task)
    agent_type = SubagentType(task.agent_type) if task.agent_type else SubagentType.GENERAL
    runtime = SubagentRuntime(parent_context=context)

    def emit(event: dict[str, Any]) -> None:
        renderer = context.renderer
        if renderer is None:
            return
        event_name = event.get("event", "")
        task_ref = event.get("task_id", task.task_id)
        if event_name == "subagent_start":
            renderer.show_status(
                f"subagent[{task_ref}] 已启动 ({event.get('agent_type', agent_type.value)})"
            )
            return
        if event_name == "subagent_tool_call":
            renderer.show_status(
                f"subagent[{task_ref}] 调用 {event.get('tool_name', 'unknown')}"
            )
            return
        if event_name == "subagent_done":
            renderer.show_status(
                f"subagent[{task_ref}] 已结束 ({event.get('stop_reason', 'unknown')}, turns={event.get('turns_used', 0)})"
            )
            return
        if event_name == "subagent_error":
            renderer.show_error(str(event.get("content", "subagent error")))

    if context.renderer is not None:
        skills = ", ".join(task.required_skill_ids) if task.required_skill_ids else "none"
        context.renderer.show_status(
            f"派发 fresh_subagent {task.task_id}: {task.subject} | skills={skills}"
        )
    sub_result = runtime.run(
        SubagentRequest(
            task_packet=packet,
            agent_type=agent_type,
            preloaded_skill_ids=list(task.required_skill_ids),
        ),
        emit=emit if context.renderer is not None else None,
    )
    normalized = normalize_subagent_result(task.task_id, sub_result)

    # Write back results to task
    next_task = replace(
        task,
        status=normalized.status,
        result_summary=normalized.summary,
        files_modified=list(normalized.files_modified),
        stop_reason=normalized.stop_reason,
        turns_used=normalized.turns_used,
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
