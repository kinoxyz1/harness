"""任务规划策略 — 记录 task_plan 的调用状态。

职责：
  当 task_plan 工具被调用后，清除 task_planning_required 标记并解除工具限制。
  不再在 skill 加载后强制触发 task_planning——由系统提示词引导 LLM 自行判断复杂度。
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
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None
