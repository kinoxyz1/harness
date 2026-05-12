from __future__ import annotations

from core.session.subagent import SubagentRunResult, SubagentStopReason
from .models import TaskPacket, TaskRecord, TaskRunResult, TaskStatus


def compile_task_packet(task: TaskRecord) -> TaskPacket:
    description = (task.description or "").strip()
    directive = task.goal.strip()
    if description:
        directive = f"{directive}\n\nContext:\n{description}"
    return TaskPacket(
        task_id=task.task_id,
        title=task.subject,
        directive=directive,
        done_criteria=list(task.done_criteria),
        agent_type=task.agent_type,
    )


def normalize_subagent_result(task_id: str, result: SubagentRunResult) -> TaskRunResult:
    status_map = {
        SubagentStopReason.COMPLETED: TaskStatus.COMPLETED,
        SubagentStopReason.CANCELLED: TaskStatus.CANCELLED,
        SubagentStopReason.MAX_TURNS: TaskStatus.FAILED,
        SubagentStopReason.API_ERROR: TaskStatus.FAILED,
        SubagentStopReason.EMPTY_RESPONSE: TaskStatus.FAILED,
        SubagentStopReason.TOOL_ERROR: TaskStatus.FAILED,
    }
    return TaskRunResult(
        task_id=task_id,
        success=result.success,
        status=status_map[result.stop_reason],
        summary=result.output,
        files_modified=list(result.files_modified),
        stop_reason=result.stop_reason.value,
        turns_used=result.turns_used,
    )
