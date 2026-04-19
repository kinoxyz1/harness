from __future__ import annotations

from typing import Any

from ..context import ExecutionBarrier, ToolResult, ToolUseContext
from ...skills.runtime import apply_skill_invocation


SCHEMA: dict[str, Any] = {
    "name": "skill",
    "description": (
        "Load a local skill immediately. The skill instructions are injected into "
        "context now, and the current tool batch stops so you can re-evaluate the "
        "next action with the skill visible."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The skill ID to load (from <available-skills> catalog)",
            },
            "args": {
                "type": "string",
                "description": "Optional arguments to pass to the skill",
            },
        },
        "required": ["skill"],
    },
}

READONLY = False

ANNOTATIONS: dict[str, bool] = {
    "readonly": False,
    "destructive": False,
    "idempotent": True,
    "concurrency_safe": False,
}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    """处理 skill 工具调用：激活指定的 skill。

    激活后返回 barrier（stop_after_tool=True, reason="skill_expanded"），
    使 QueryLoop 停止当前工具批次，让模型在下一轮看到新激活的 skill 指令。

    不再向 transcript 注入消息，skill 内容通过 state.invoked_skills 渲染。

    Args:
        args: 工具参数，包含 "skill"（skill ID）和可选的 "args"。
        context: 工具执行上下文，提供 session_state 和 skill_registry。

    Returns:
        ToolResult:
        - success=True: skill 已激活，barrier 设为 skill_expanded
        - success=False: skill 未找到 / 预算超限 / 运行时不可用
    """
    skill_id = args.get("skill", "").strip()
    if not skill_id:
        return ToolResult(output="Missing skill parameter", success=False, error="missing_params")

    state = context.session_state
    registry = context.skill_registry
    if state is None or registry is None:
        return ToolResult(output="Skill runtime unavailable", success=False, error="runtime_unavailable")

    if skill_id not in state.skill_catalog:
        return ToolResult(output=f"Skill not found: {skill_id}", success=False, error="not_found")

    try:
        content = registry.load(skill_id)
    except (ValueError, KeyError) as exc:
        return ToolResult(output=f"Failed to load skill: {exc}", success=False, error="load_failed")

    try:
        apply_skill_invocation(
            state=state,
            skill_id=skill_id,
            content=content,
            turn=context.turn_count,
        )
    except ValueError as exc:
        return ToolResult(output=str(exc), success=False, error="budget_exceeded")

    return ToolResult(
        output=f"Skill loaded: {skill_id}. Re-evaluate your next action using the injected skill guidance.",
        success=True,
        injected_messages=[],
        barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
    )
