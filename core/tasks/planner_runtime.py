from __future__ import annotations

from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus

MAX_TASKS = 20


def normalize_task_payload(raw: dict, *, index: int, previous: TaskState) -> TaskRecord:
    task_id = raw.get("task_id") or f"task-{index + 1}"
    subject = str(raw["subject"]).strip()
    goal = str(raw["goal"]).strip()
    status = TaskStatus(raw.get("status", "pending"))
    execution_mode = TaskExecutionMode(raw.get("execution_mode", "local"))
    return TaskRecord(
        task_id=task_id,
        subject=subject,
        goal=goal,
        status=status,
        execution_mode=execution_mode,
        parent_todo_id=raw.get("parent_todo_id"),
        active_form=raw.get("active_form"),
        agent_type=raw.get("agent_type"),
        allowed_tools=list(raw.get("allowed_tools") or []),
        write_scope=list(raw.get("write_scope") or []),
        inputs=list(raw.get("inputs") or []),
        known_context=list(raw.get("known_context") or []),
        out_of_scope=list(raw.get("out_of_scope") or []),
        required_skills=list(raw.get("required_skills") or []),
        expected_output=list(raw.get("expected_output") or []),
        done_criteria=list(raw.get("done_criteria") or []),
        depends_on=list(raw.get("depends_on") or []),
        owner=raw.get("owner"),
        blocked_reason=raw.get("blocked_reason"),
    )


def validate_tasks(raw_tasks: list[dict], *, previous: TaskState) -> None:
    if len(raw_tasks) > MAX_TASKS:
        raise ValueError(f"task count exceeds limit: {MAX_TASKS}")
    normalized_statuses = [task.get("status", "pending") for task in raw_tasks]
    if sum(1 for status in normalized_statuses if status == "in_progress") > 1:
        raise ValueError("at most one in_progress task is allowed")


def build_task_state(raw_tasks: list[dict], *, previous: TaskState, turn_count: int) -> TaskState:
    validate_tasks(raw_tasks, previous=previous)
    tasks = [normalize_task_payload(raw, index=i, previous=previous) for i, raw in enumerate(raw_tasks)]
    tasks_by_id = {task.task_id: task for task in tasks}
    ordered_task_ids = [task.task_id for task in tasks]
    current_task_id = next((task.task_id for task in tasks if task.status == TaskStatus.IN_PROGRESS), None)
    return TaskState(
        tasks_by_id=tasks_by_id,
        ordered_task_ids=ordered_task_ids,
        current_task_id=current_task_id,
        last_planned_turn=turn_count,
    )
