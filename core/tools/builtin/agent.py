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
        emit_to_renderer,
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

    # Reject when TaskState is active — agent is sidecar only
    state = context.session_state
    task_state = getattr(state, "task_state", None) if state else None
    if task_state is not None and task_state.current_task_id:
        current = task_state.tasks_by_id.get(task_state.current_task_id)
        if current is not None:
            if current.execution_mode.value == "fresh_subagent":
                return ToolInvocationOutcome(
                    status=ToolOutcomeStatus.FAILURE,
                    error="task_state_active_use_task_execute",
                    messages=[make_tool_message(context, "TaskState is active. Use `task_execute` for the current fresh_subagent task.")],
                )
            return ToolInvocationOutcome(
                status=ToolOutcomeStatus.FAILURE,
                error="task_state_active_use_local_tools",
                messages=[make_tool_message(context, "TaskState is active. Execute the current LOCAL task with normal tools, then update status via `task_plan`.")],
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
        emit=emit_to_renderer(context.renderer),
    )

    result = execution.result
    summary = render_subagent_summary(result)

    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS if result.success else ToolOutcomeStatus.FAILURE,
        messages=[make_tool_message(context, summary)],
    )
