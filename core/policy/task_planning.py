"""任务规划策略 — 在执行前强制先完成 task_plan。

职责：
  1. 当 task_planning_required=True 且 TaskState 为空时，限制可用工具到 pre-plan 白名单，
     并注入提醒消息要求模型先调用 task_plan。
  2. 当 task_plan 工具被调用后，解除限制。
  3. 当 skill 工具被调用但 TaskState 仍为空时，重新设置 task_planning_required 标记。
"""
from __future__ import annotations

PREPLAN_ALLOWED_TOOLS = {"task_plan", "skill", "find", "read_file"}


class TaskPlanningPolicy:
    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        if session_state.task_state.tasks_by_id:
            run_state.allowed_tools_override = None
            return []
        if not run_state.task_planning_required:
            return []

        run_state.allowed_tools_override = set(PREPLAN_ALLOWED_TOOLS)
        return [{
            "role": "user",
            "content": (
                "<system-reminder type=\"task_planning\">\n"
                "Current request requires task planning before execution.\n"
                "Call `task_plan` with the full task list first.\n"
                "You may still use lightweight read-only discovery tools if needed, "
                "but do not edit files, dispatch subagents, or start execution until TaskState exists.\n"
                "</system-reminder>"
            ),
        }]

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        if any(name == "task_plan" for name in batch_result.tool_names):
            run_state.task_planning_required = False
            run_state.task_planning_reason = None
            run_state.allowed_tools_override = None
        elif any(name == "skill" for name in batch_result.tool_names) and not session_state.task_state.tasks_by_id:
            run_state.task_planning_required = True
            run_state.task_planning_reason = "post_skill"
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None
