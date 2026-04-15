from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from ..context import ToolResult, ToolUseContext
from .todo import clear_state, restore_snapshot, save_snapshot

if TYPE_CHECKING:
    from ...session.subagent import SubagentRequest


SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": (
            "Delegate a substantial subtask to an isolated sub-agent. "
            "Use for codebase exploration, implementation planning, "
            "or isolated multi-step work that would otherwise bloat the main context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "A self-contained task prompt for the sub-agent. "
                        "Include all necessary context, constraints, and expected output format."
                    ),
                },
                "agent_type": {
                    "type": "string",
                    "enum": ["explore", "plan", "general"],
                    "description": "Sub-agent type. Default is general.",
                },
                "description": {
                    "type": "string",
                    "description": "A short label for status display, ideally 3-8 words.",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Optional per-subagent turn limit.",
                },
            },
            "required": ["task"],
        },
    },
}

READONLY = False

ANNOTATIONS: dict[str, bool] = {
    "readonly": False,
    "destructive": False,
    "idempotent": False,
    "concurrency_safe": False,
}

_RUNTIME_CLS = None


def _default_runtime_cls():
    from ...session.subagent import SubagentRuntime

    return SubagentRuntime


def parse_subagent_request(args: dict[str, Any]) -> "SubagentRequest":
    """解析并校验 subagent tool 参数。"""
    from ...session.subagent import SubagentRequest, SubagentType

    task = args["task"]
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task 不能为空")

    raw_agent_type = args.get("agent_type", SubagentType.GENERAL.value)
    try:
        agent_type = SubagentType(raw_agent_type)
    except ValueError as e:
        raise ValueError(f"不支持的 agent_type: {raw_agent_type}") from e

    if agent_type is SubagentType.FORK:
        raise ValueError("fork 暂未实现")

    max_turns = args.get("max_turns")
    if max_turns is not None:
        if not isinstance(max_turns, int):
            raise ValueError("max_turns 必须是整数")
        if max_turns <= 0:
            raise ValueError("max_turns 必须大于 0")

    description = args.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("description 必须是字符串")

    return SubagentRequest(
        task=task.strip(),
        agent_type=agent_type,
        description=description,
        max_turns=max_turns,
    )


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    """执行 subagent tool。"""
    try:
        request = parse_subagent_request(args)
    except ValueError as e:
        return ToolResult(
            output=f"参数错误: {e}",
            success=False,
            error="validation_failed",
        )

    snapshot = save_snapshot()
    try:
        clear_state()
        runtime_cls = _RUNTIME_CLS or _default_runtime_cls()
        runtime = runtime_cls(parent_context=context)
        from ...session.subagent import render_subagent_summary
        result = runtime.run(request)
        return ToolResult(
            output=render_subagent_summary(result),
            success=result.success,
        )
    except Exception as e:
        return ToolResult(
            output=f"子代理执行失败: {e}",
            success=False,
            error="subagent_error",
        )
    finally:
        restore_snapshot(snapshot)
