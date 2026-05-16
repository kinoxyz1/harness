"""background_check 工具 — 查询后台任务状态。"""
from __future__ import annotations

import time
from typing import Any

from ..context import ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext, make_tool_message


SCHEMA: dict[str, Any] = {
    "name": "background_check",
    "description": (
        "查询后台任务状态。返回运行状态和结果摘要。"
        "\n\n状态：running（运行中）、completed（成功）、failed（失败）、timeout（超时）。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "background_run 返回的任务 ID",
            },
        },
        "required": ["task_id"],
    },
}

READONLY = True

ANNOTATIONS: dict[str, bool] = {
    "readonly": True,
    "destructive": False,
    "idempotent": True,
    "concurrency_safe": True,
}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    bg_manager = getattr(context.session_state, "background_manager", None)
    if bg_manager is None:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="not_available",
            messages=[make_tool_message(context, "后台任务功能未启用")],
        )

    record = bg_manager.check(args["task_id"])
    if record is None:
        content = f"未找到任务 {args['task_id']}"
    elif record.status == "running":
        elapsed = int(time.time() - record.started_at)
        content = f"任务 {record.id} 仍在运行（{elapsed}秒）"
    else:
        content = f"任务 {record.id} 状态: {record.status}\n{record.result_preview}"
    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        messages=[make_tool_message(context, content)],
    )
