"""Todo 管理工具。"""
from __future__ import annotations

from typing import Any

from core.session.state import TodoItem, TodoState

from ..context import ToolResult, ToolUseContext


# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "name": "todo",
    "description": (
        "Rewrite the current session plan for non-trivial multi-step work. "
        "Use this early for tasks that require multiple actions, especially after a skill was just expanded. "
        "Mirror the active workflow instead of collapsing it into 1-2 vague items. "
        "Keep exactly one in_progress item whenever active work exists. "
        "Update the plan as tasks complete or scope changes. "
        "If validation is required, include an explicit verification task. "
        "Preserve meaningful workflow labels such as 2.5 when they are real and relevant.\n\n"
        "## Granularity & Detail\n"
        "Each item should describe a concrete, verifiable step — not a vague phase. "
        "Good: 'Read CSV with pandas, identify columns and dtypes, print shape and null counts'. "
        "Bad: 'Read and analyze the data'. "
        "Include specific actions (what to do), scope (which files/fields/APIs), and expected outcome (what success looks like). "
        "When a skill provides numbered steps, break each step into its own item rather than grouping several together. "
        "Prefer 8-15 focused items over 4-5 broad ones."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
                "items": {
                    "type": "array",
                    "description": "完整任务列表（全量覆盖当前计划）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "具体的祈使句任务描述，包含做什么(动作)、对什么(范围/目标)、产出什么(预期结果)。避免笼统概括，要让读者无需追问就知道这一步具体做什么",
                            },
                            "active_form": {
                                "type": "string",
                                "description": "进行时形式的简短描述，用于显示当前聚焦工作，如 '正在解析 CSV 列结构和数据类型'",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "任务状态（pending|in_progress|completed）",
                            },
                            "workflow_ref": {
                                "type": "string",
                                "description": "可选的工作流标签，如 2.5",
                                "nullable": True,
                            },
                        },
                        "required": ["content", "active_form", "status"],
                    },
                },
            },
        "required": ["items"],
    },
}

# ─── 元信息（给框架看）───────────────────────────────

READONLY = False

ANNOTATIONS: dict[str, bool] = {
    "readonly": False,
    "destructive": False,
    "idempotent": True,
    "concurrency_safe": False,  # 修改内部状态，串行执行
}

# 兼容层：保留只读查询 API，指向最近一次成功写入的 session todo_state。
_latest_todo_state: TodoState | None = None


# ─── 内部逻辑 ───────────────────────────────────────

MAX_ITEMS = 20
VALID_STATUSES = {"pending", "in_progress", "completed"}


def _validate_items(items: Any) -> tuple[bool, str]:
    """验证任务列表。返回 (是否通过, 错误信息)。"""
    if not isinstance(items, list):
        return False, "items 必须是数组"
    if len(items) > MAX_ITEMS:
        return False, f"任务数量超过限制：最多 {MAX_ITEMS} 个任务"

    in_progress_count = 0
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return False, f"第 {i+1} 项必须是对象"

        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            return False, f"第 {i+1} 项缺少 content"

        active_form = item.get("active_form")
        if not isinstance(active_form, str) or not active_form.strip():
            return False, f"第 {i+1} 项缺少 active_form"

        status = item.get("status")
        if status not in VALID_STATUSES:
            return False, f"第 {i+1} 项 status 无效: {status}"

        workflow_ref = item.get("workflow_ref")
        if workflow_ref is not None and not isinstance(workflow_ref, str):
            return False, f"第 {i+1} 项 workflow_ref 必须是字符串或 null"

        if status == "in_progress":
            in_progress_count += 1

    if in_progress_count > 1:
        return False, f"最多只能有 1 个 in_progress 任务，当前有 {in_progress_count} 个"

    return True, ""


def _render_progress(items: list[TodoItem]) -> str:
    """渲染进度文本（给 LLM 看，精简版）。"""
    if not items:
        return "计划已清空。"

    completed = sum(1 for item in items if item.status == "completed")
    total = len(items)
    current = next((item.content for item in items if item.status == "in_progress"), None)

    msg = f"计划已更新 ({completed}/{total} 完成)。"
    if current:
        msg += f" 当前: {current}"
    return msg


# ─── Handler（执行逻辑）─────────────────────────────

def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    """处理 todo 调用，更新任务状态。"""
    state = context.session_state
    if state is None:
        return ToolResult(output="No session state available", success=False, error="no_state")

    if not isinstance(args, dict):
        return ToolResult(output="参数错误: args 必须是对象", success=False, error="validation_failed")

    items_data = args.get("items")

    # 验证
    valid, error = _validate_items(items_data)
    if not valid:
        return ToolResult(output=f"参数错误: {error}", success=False, error="validation_failed")

    # 写入 session state
    items = [
        TodoItem(
            content=item["content"].strip(),
            active_form=item["active_form"].strip(),
            status=item["status"],
            workflow_ref=(item.get("workflow_ref") or None),
        )
        for item in items_data
    ]

    if items and all(item.status == "completed" for item in items):
        state.todo_state.items = []
        state.todo_state.last_completed_items = items
    else:
        state.todo_state.items = items
        state.todo_state.last_completed_items = []
    state.todo_state.last_write_turn = context.turn_count

    # 兼容层记录最近一次会话级 todo 状态（只读快照由 get_state 提供）。
    global _latest_todo_state
    _latest_todo_state = state.todo_state

    # 返回渲染后的进度
    output = _render_progress(state.todo_state.items)
    return ToolResult(output=output, success=True)


# ─── 对外暴露的 API（供 AgentLoop 使用）──────────────

def get_state() -> TodoState:
    """获取当前规划状态（供 AgentLoop 查询）。"""
    source = _latest_todo_state
    if source is None:
        return TodoState()
    return TodoState(
        items=[
            TodoItem(
                content=item.content,
                active_form=item.active_form,
                status=item.status,
                workflow_ref=item.workflow_ref,
            )
            for item in source.items
        ],
        last_completed_items=[
            TodoItem(
                content=item.content,
                active_form=item.active_form,
                status=item.status,
                workflow_ref=item.workflow_ref,
            )
            for item in source.last_completed_items
        ],
        last_write_turn=source.last_write_turn,
        last_reminder_turn=source.last_reminder_turn,
    )
