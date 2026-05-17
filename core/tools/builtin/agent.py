from __future__ import annotations

from typing import Any

from ..context import ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext, make_tool_message


SCHEMA: dict[str, Any] = {
    "name": "agent",
    "description": (
        "Dispatch an ad-hoc subagent for exploration, analysis, or side tasks. "
        "This is the sidecar/ad-hoc path — NOT for formal task execution. "
        "For tasks managed by task_plan, use task_execute instead. "
        "Use agent when there is no active TaskState and you need a quick subagent investigation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short description of what this subagent should do (2-8 words).",
            },
            "prompt": {
                "type": "string",
                "description": "Full prompt to send to the subagent.",
            },
            "subagent_type": {
                "type": "string",
                "enum": ["explore", "plan", "general"],
                "description": "Type of subagent to dispatch.",
            },
        },
        "required": ["description", "prompt"],
    },
}

READONLY = False
ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": False, "concurrency_safe": True}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    from core.session.subagent import (
        SubagentRequest,
        SubagentType,
        dispatch_subagent,
        render_subagent_summary,
    )
    from core.tasks.models import TaskPacket

    description = args.get("description", "").strip()
    prompt = args.get("prompt", "").strip()
    subagent_type_str = args.get("subagent_type", "general")

    if not prompt:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="empty_prompt",
            messages=[make_tool_message(context, "agent tool requires a non-empty 'prompt'.")],
        )

    try:
        agent_type = SubagentType(subagent_type_str)
    except ValueError:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="invalid_subagent_type",
            messages=[make_tool_message(context, f"Invalid subagent_type: {subagent_type_str}. Use explore, plan, or general.")],
        )

    packet = TaskPacket(
        task_id="ad-hoc",
        title=description or prompt[:80],
        directive=prompt,
    )

    execution = dispatch_subagent(
        parent_context=context,
        source_tool="agent",
        task_id=None,
        task_subject=description or prompt[:80],
        prompt_text=prompt,
        request=SubagentRequest(
            task_packet=packet,
            agent_type=agent_type,
        ),
        emit=None,
    )

    result = execution.result
    summary = render_subagent_summary(result)

    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS if result.success else ToolOutcomeStatus.FAILURE,
        messages=[make_tool_message(context, summary)],
    )
