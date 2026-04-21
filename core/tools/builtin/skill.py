from __future__ import annotations

from typing import Any

from core.skills.models import SkillEvent

from ..context import (
    SessionUpdate,
    SessionUpdateKind,
    ToolInvocationOutcome,
    ToolOutcomeStatus,
    ToolUseContext,
    make_tool_message,
)
from ...skills.runtime import build_invoked_skill_record


SCHEMA: dict[str, Any] = {
    "name": "skill",
    "description": (
        "Load a local skill into runtime state. The skill guidance will be available "
        "on the next model turn, so use this when you need additional workflow "
        "instructions before subsequent reasoning or tool use."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The skill ID to load (from <available-skills> catalog)",
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


UNKNOWN_CONVERSATION_INDEX = -1


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    """处理 skill 工具调用：激活指定的 skill。

    通过 ToolInvocationOutcome 返回会话更新：
    - INVOKE_SKILL: 写入 invoked_skills
    - APPEND_SKILL_EVENT: 记录 skill 激活事件

    Args:
        args: 工具参数，包含 "skill"（skill ID）。
        context: 工具执行上下文，提供 session_state 和 skill_registry。

    Returns:
        ToolInvocationOutcome:
        - SUCCESS: 返回消息 + session_updates
        - FAILURE: 返回失败消息 + error code
    """
    skill_id = args.get("skill", "").strip()
    if not skill_id:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="missing_params",
            messages=[make_tool_message(context, "Missing skill parameter")],
        )

    state = context.session_state
    registry = context.skill_registry
    if state is None or registry is None:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="runtime_unavailable",
            messages=[make_tool_message(context, "Skill runtime unavailable")],
        )

    if skill_id not in state.skill_catalog:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="not_found",
            messages=[make_tool_message(context, f"Skill not found: {skill_id}")],
        )

    try:
        content = registry.load(skill_id)
    except (ValueError, KeyError) as exc:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="load_failed",
            messages=[make_tool_message(context, f"Failed to load skill: {exc}")],
        )

    try:
        record = build_invoked_skill_record(
            state=state,
            skill_id=skill_id,
            content=content,
            turn=context.turn_count,
        )
    except ValueError as exc:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="budget_exceeded",
            messages=[make_tool_message(context, str(exc))],
        )

    event = SkillEvent(
        skill_id=skill_id,
        action="activated",
        source="model_tool_call",
        # QueryLoop 侧还没有稳定的 conversation index 传入；先用统一占位值。
        conversation_index=UNKNOWN_CONVERSATION_INDEX,
    )

    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        messages=[
            make_tool_message(
                context,
                f"Skill loaded: {skill_id}. The skill guidance will be available on the next model turn.",
            )
        ],
        session_updates=[
            SessionUpdate(
                kind=SessionUpdateKind.INVOKE_SKILL,
                payload={"invoked_skill": record},
            ),
            SessionUpdate(
                kind=SessionUpdateKind.APPEND_SKILL_EVENT,
                payload={"skill_event": event},
            ),
        ],
    )
