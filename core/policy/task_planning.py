"""任务规划策略 — 记录 task_plan 的调用状态。

职责：
  当用户请求明显属于复杂多步骤任务时，自动启用 task_plan 前置门禁。
  当 task_plan 工具被调用后，清除 task_planning_required 标记并解除工具限制。
"""
from __future__ import annotations

import re

PREPLAN_ALLOWED_TOOLS = {"task_plan", "find", "read_file"}

_MULTI_GOAL_PATTERNS = (
    re.compile(r"然后"),
    re.compile(r"并且"),
    re.compile(r"还要"),
    re.compile(r"还需要"),
    re.compile(r"同时"),
    re.compile(r"结合"),
    re.compile(r"一边.*一边"),
    re.compile(r"先.+再"),
    re.compile(r"\band\b", re.IGNORECASE),
    re.compile(r"\bthen\b", re.IGNORECASE),
    re.compile(r"\balso\b", re.IGNORECASE),
)

_DELIVERABLE_PATTERNS = (
    re.compile(r"规划"),
    re.compile(r"方案"),
    re.compile(r"攻略"),
    re.compile(r"推荐"),
    re.compile(r"总结"),
    re.compile(r"分析"),
    re.compile(r"比较"),
    re.compile(r"报告"),
    re.compile(r"行程"),
    re.compile(r"\bplan\b", re.IGNORECASE),
    re.compile(r"\breport\b", re.IGNORECASE),
    re.compile(r"\banaly[sz]e\b", re.IGNORECASE),
    re.compile(r"\bcompare\b", re.IGNORECASE),
)

_RESEARCH_PATTERNS = (
    re.compile(r"天气"),
    re.compile(r"路线"),
    re.compile(r"交通"),
    re.compile(r"公交"),
    re.compile(r"地铁"),
    re.compile(r"价格"),
    re.compile(r"新闻"),
    re.compile(r"最新"),
    re.compile(r"明天"),
    re.compile(r"today|tomorrow|latest|price|weather|route|transit", re.IGNORECASE),
)


def _match_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def should_require_task_planning(user_intent: str) -> bool:
    text = (user_intent or "").strip()
    if len(text) < 20:
        return False

    score = 0
    if _match_any(text, _MULTI_GOAL_PATTERNS):
        score += 1
    if _match_any(text, _DELIVERABLE_PATTERNS):
        score += 1
    if _match_any(text, _RESEARCH_PATTERNS):
        score += 1
    if text.count("，") + text.count(",") + text.count("、") >= 2:
        score += 1
    if "\n" in text:
        score += 1
    return score >= 2


class TaskPlanningPolicy:
    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        if session_state.task_state.tasks_by_id:
            run_state.allowed_tools_override = None
            return []
        if (
            not run_state.task_planning_required
            and session_state.user_intents
            and should_require_task_planning(session_state.user_intents[-1])
        ):
            run_state.task_planning_required = True
            run_state.task_planning_reason = "complex_user_request"
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
