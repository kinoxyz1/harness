from __future__ import annotations

from core.session.subagent import SubagentRunResult
from .models import TaskPacket, TaskRecord, TaskRunResult, TaskStatus


def compile_task_packet(task: TaskRecord) -> TaskPacket:
    expected_output = task.expected_output or [task.goal]
    return TaskPacket(
        task_id=task.task_id,
        mode=task.execution_mode,
        agent_type=task.agent_type,
        title=task.subject,
        directive=task.goal,
        task_context=list(task.inputs),
        known_facts=list(task.known_context),
        out_of_scope=list(task.out_of_scope),
        expected_output=expected_output,
        done_criteria=list(task.done_criteria),
        required_skills=list(task.required_skills),
        allowed_tools=list(task.allowed_tools),
        write_scope=list(task.write_scope),
        packet_revision=task.packet_revision + 1,
    )


def normalize_subagent_result(task_id: str, result: SubagentRunResult) -> TaskRunResult:
    return TaskRunResult(
        task_id=task_id,
        success=result.success,
        status=TaskStatus.COMPLETED if result.success else TaskStatus.FAILED,
        summary=result.output,
        files_modified=list(result.files_modified),
        failure_reason=None if result.success else result.stop_reason.value,
    )
