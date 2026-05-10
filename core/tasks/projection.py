from __future__ import annotations

from core.session.state import TodoItem
from .models import TaskState, TaskStatus


def map_task_status_to_todo_status(status: TaskStatus) -> str:
    if status == TaskStatus.IN_PROGRESS:
        return "in_progress"
    if status == TaskStatus.COMPLETED:
        return "completed"
    return "pending"


def project_task_state_to_todo_items(task_state: TaskState) -> list[TodoItem]:
    items: list[TodoItem] = []
    for task_id in task_state.ordered_task_ids:
        task = task_state.tasks_by_id[task_id]
        items.append(
            TodoItem(
                content=task.subject,
                active_form=task.active_form or task.subject,
                status=map_task_status_to_todo_status(task.status),
                workflow_ref=None,
            )
        )
    return items
