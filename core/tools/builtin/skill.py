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
    """Load a skill inline, injecting its content into the current context."""
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
        message = apply_skill_invocation(
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
        injected_messages=[message],
        barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
    )
