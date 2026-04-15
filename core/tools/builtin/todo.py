"""Todo 管理工具。

LLM 主动调用 todo_manage 来更新任务计划。
传入完整任务列表替换旧列表（非增量更新）。
"""

# 注意：此模块使用模块级单例状态。
# 当前设计假设工具调用是串行执行的（由 ToolExecutorRuntime 保证）。
# 如果未来需要并行执行，需要添加线程锁保护。
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..context import ToolResult, ToolUseContext


# ─── Tool 定义（给模型看）───────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": "Rewrite the current session plan for multi-step work.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "完整的任务列表（替换旧列表）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "任务描述",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "failed"],
                                "description": "任务状态",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["items"],
        },
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

# ─── 内部状态（模块级单例）───────────────────────────

@dataclass
class PlanItem:
    content: str
    status: str = "pending"


@dataclass
class PlanningState:
    items: list[PlanItem] = field(default_factory=list)
    rounds_since_update: int = 0


_state = PlanningState()


# ─── 内部逻辑 ───────────────────────────────────────

MAX_ITEMS = 12
VALID_STATUSES = {"pending", "in_progress", "completed", "failed"}


def _validate_items(items: list[dict]) -> tuple[bool, str]:
    """验证任务列表。返回 (是否通过, 错误信息)。"""
    if len(items) > MAX_ITEMS:
        return False, f"任务数量超过限制：最多 {MAX_ITEMS} 个任务"

    in_progress_count = 0
    for i, item in enumerate(items):
        if "content" not in item or not item["content"].strip():
            return False, f"第 {i+1} 项缺少 content"

        status = item.get("status")
        if status not in VALID_STATUSES:
            return False, f"第 {i+1} 项 status 无效: {status}"

        if status == "in_progress":
            in_progress_count += 1

    if in_progress_count > 1:
        return False, f"最多只能有 1 个 in_progress 任务，当前有 {in_progress_count} 个"

    return True, ""


def _render_progress(items: list[PlanItem]) -> str:
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
    """处理 todo_manage 调用，更新任务状态。"""
    items_data = args.get("items", [])

    # 验证
    valid, error = _validate_items(items_data)
    if not valid:
        return ToolResult(output=f"参数错误: {error}", success=False, error="validation_failed")

    # 替换内部状态
    _state.items = [PlanItem(content=item["content"], status=item["status"]) for item in items_data]
    _state.rounds_since_update = 0

    # 返回渲染后的进度
    output = _render_progress(_state.items)
    return ToolResult(output=output, success=True)


# ─── 对外暴露的 API（供 AgentLoop 使用）──────────────

def get_state() -> PlanningState:
    """获取当前规划状态（供 AgentLoop 查询）。"""
    return _state


def save_snapshot() -> PlanningState:
    """保存当前 todo 状态快照。"""
    return PlanningState(
        items=[PlanItem(content=item.content, status=item.status) for item in _state.items],
        rounds_since_update=_state.rounds_since_update,
    )


def restore_snapshot(snapshot: PlanningState) -> None:
    """恢复 todo 状态快照。"""
    _state.items = [PlanItem(content=item.content, status=item.status) for item in snapshot.items]
    _state.rounds_since_update = snapshot.rounds_since_update


def clear_state() -> None:
    """清空当前 todo 状态。"""
    _state.items = []
    _state.rounds_since_update = 0


def increment_rounds() -> int:
    """递增 rounds_since_update，返回新值。"""
    _state.rounds_since_update += 1
    return _state.rounds_since_update


def reset_rounds() -> None:
    """重置 rounds_since_update 为 0。"""
    _state.rounds_since_update = 0
