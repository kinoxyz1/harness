from __future__ import annotations

from typing import Any

from core.tools.context import (
    SessionUpdate,
    SessionUpdateKind,
    ToolInvocationOutcome,
    ToolOutcomeStatus,
    ToolUseContext,
    make_tool_message,
)


SCHEMA: dict[str, Any] = {
    "name": "memory",
    "description": (
        "Save durable information to persistent memory. "
        "WHEN TO SAVE: user shares a preference, you discover an environment fact, "
        "you learn a convention. "
        "TWO TARGETS: 'user' (who the user is — preferences, habits, role), "
        "'memory' (your notes — project facts, design decisions, pitfalls). "
        "ACTIONS: add, replace, remove. "
        "Each entry should be self-contained and under 200 chars."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "replace", "remove"]},
            "target": {"type": "string", "enum": ["memory", "user"]},
            "content": {"type": "string"},
            "old_text": {"type": "string"},
        },
        "required": ["action", "target"],
    },
}

READONLY = False
ANNOTATIONS = {
    "readonly": False,
    "destructive": False,
    "idempotent": False,
    "concurrency_safe": False,
}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolInvocationOutcome:
    state = context.session_state
    store = getattr(state, "memory_store", None) if state is not None else None
    if store is None:
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="memory_store_unavailable",
            messages=[make_tool_message(context, "Memory store unavailable.")],
        )

    action = str(args.get("action", ""))
    target = str(args.get("target", ""))
    content = str(args.get("content", ""))
    old_text = str(args.get("old_text", ""))

    if action == "add":
        result = store.add(target, content)
    elif action == "replace":
        result = store.replace(target, old_text, content)
    elif action == "remove":
        result = store.remove(target, old_text)
    else:
        result = {"ok": False, "error": f"unknown action: {action}"}

    if not result.get("ok"):
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="memory_write_failed",
            messages=[make_tool_message(context, f"Memory update failed: {result['error']}")],
        )

    payload_content = content if action != "remove" else old_text
    return ToolInvocationOutcome(
        status=ToolOutcomeStatus.SUCCESS,
        messages=[make_tool_message(context, f"Memory updated. Usage: {result['usage']}")],
        session_updates=[
            SessionUpdate(
                kind=SessionUpdateKind.MEMORY_WRITE,
                payload={
                    "action": action,
                    "target": target,
                    "content": payload_content,
                },
            )
        ],
    )
