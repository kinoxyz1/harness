"""background_run 工具 — 将长命令提交到后台执行。"""
from __future__ import annotations

from typing import Any

from core.shared.config import BG_TASK_DEFAULT_TIMEOUT
from ..context import ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext, make_tool_message


SCHEMA: dict[str, Any] = {
    "name": "background_run",
    "description": (
        "在后台执行一个长运行 Shell 命令。立刻返回 task_id，不阻塞主循环。"
        "\n\n适用场景：命令预计运行超过 30 秒时使用，例如："
        "\n- pytest / npm test / cargo test"
        "\n- npm install / pip install"
        "\n- docker build / docker compose up"
        "\n- 大型编译任务"
        "\n\n不适用：秒级完成的命令（直接用 bash）、文件操作（用专用工具）。"
        "\n\n用 background_check 查询结果。完成后会自动收到通知。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 Shell 命令",
            },
            "timeout": {
                "type": "integer",
                "description": f"超时秒数，默认 {BG_TASK_DEFAULT_TIMEOUT}",
                "default": BG_TASK_DEFAULT_TIMEOUT,
            },
        },
        "required": ["command"],
    },
}

READONLY = False

ANNOTATIONS: dict[str, bool] = {
    "readonly": False,
    "destructive": False,
    "idempotent": False,
    "concurrency_safe": False,
}

PROMPT: str = """\
## background_run — 后台执行长命令

将命令提交到后台执行，立刻返回 task_id，不阻塞主循环。

### 何时使用 background_run 而不是 bash
- 命令预计运行 **超过 30 秒** → 用 background_run
- 命令预计运行 **不到 30 秒** → 用 bash
- 需要看到实时输出 → 用 bash
- 需要等待结果才能继续 → 用 bash

### 安全说明
后台执行走和 bash 相同的安全检查（黑名单拦截、危险命令确认），不会绕过任何安全机制。

### 使用方式
1. 调用 background_run 提交命令，获得 task_id
2. 继续做其他工作
3. 任务完成后会自动收到通知
4. 需要查看详情时，调用 background_check(task_id)
"""


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    bg_manager = getattr(context.session_state, "background_manager", None)
    if bg_manager is None:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="not_available",
            messages=[make_tool_message(context, "后台任务功能未启用")],
        )

    task_id, error = bg_manager.submit(
        command=args["command"],
        timeout=args.get("timeout", BG_TASK_DEFAULT_TIMEOUT),
    )
    if error:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="rejected",
            messages=[make_tool_message(context, error)],
        )

    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        messages=[make_tool_message(
            context,
            f"后台任务已提交 (task_id={task_id})。你可以继续别的工作，完成后会收到通知。",
        )],
    )
