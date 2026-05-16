from __future__ import annotations

from core.tasks.models import TaskExecutionMode, TaskRecord, TaskState, TaskStatus

MAX_TASKS = 20

TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def normalize_task_payload(raw: dict, *, index: int, previous: TaskState | None) -> TaskRecord:
    task_id = raw.get("task_id") or f"task-{index + 1}"
    return TaskRecord(
        task_id=task_id,
        subject=str(raw["subject"]).strip(),
        goal=str(raw["goal"]).strip(),
        status=TaskStatus(raw.get("status", "pending")),
        execution_mode=TaskExecutionMode(raw.get("execution_mode", "local")),
        agent_type=raw.get("agent_type"),
        description=raw.get("description"),
        done_criteria=list(raw.get("done_criteria") or []),
        depends_on=list(raw.get("depends_on") or []),
        required_skill_ids=list(raw.get("required_skill_ids") or []),
    )


def validate_tasks(raw_tasks: list[dict], *, previous: TaskState | None) -> None:
    if len(raw_tasks) > MAX_TASKS:
        raise ValueError(f"task count exceeds limit: {MAX_TASKS}")
    normalized_statuses = [task.get("status", "pending") for task in raw_tasks]
    if sum(1 for status in normalized_statuses if status == "in_progress") > 1:
        raise ValueError("at most one in_progress task is allowed")
    subjects = [str(task.get("subject", "")).strip() for task in raw_tasks]
    seen: set[str] = set()
    for subject in subjects:
        if subject in seen:
            raise ValueError(f"duplicate subject not allowed: {subject!r}. Each task must have a unique subject.")
        if subject:
            seen.add(subject)


def build_task_state(raw_tasks: list[dict], *, previous: TaskState | None, turn_count: int) -> TaskState:
    validate_tasks(raw_tasks, previous=previous)
    previous = previous or TaskState()

    incoming: list[TaskRecord] = []
    for index, raw in enumerate(raw_tasks):
        task = normalize_task_payload(raw, index=index, previous=previous)
        prev = previous.tasks_by_id.get(task.task_id)
        if prev is not None and prev.status in TERMINAL_STATUSES and task.status != prev.status:
            raise ValueError(f"cannot reopen terminal task_id: {task.task_id}")
        incoming.append(task)

    tasks_by_id = {task.task_id: task for task in incoming}
    ordered_task_ids = [task.task_id for task in incoming]
    current_task_id = next((task.task_id for task in incoming if task.status == TaskStatus.IN_PROGRESS), None)
    return TaskState(
        tasks_by_id=tasks_by_id,
        ordered_task_ids=ordered_task_ids,
        current_task_id=current_task_id,
        last_planned_turn=turn_count,
    )
