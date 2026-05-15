from __future__ import annotations

import threading

from core.llm.client import ModelRequestOptions
from core.query.reducers import apply_run_update, apply_session_update
from core.query.state import RunState
from core.shared.run_options import RunDisplayOptions
from core.tools.runtime import ToolCall
from core.tools.runtime import ToolExecutorRuntime

_MEMORY_REVIEW_SYSTEM_PROMPT = (
    "Review the conversation and save durable memory only when warranted.\n"
    "Use the memory tool for stable user preferences, durable project facts, "
    "design decisions, and conventions likely to matter in later sessions.\n"
    "Do not save transient tool failures, temporary environment problems, "
    "one-off requests, or speculative guesses.\n"
    "If nothing deserves durable memory, do not call any tool."
)

_MEMORY_REVIEW_WINDOW_MESSAGES = 12


def _parse_tool_calls(raw_calls: list[dict]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for i, tc in enumerate(raw_calls):
        args = tc.get("args", {})
        if not isinstance(args, dict):
            args = {}
        calls.append(
            ToolCall(
                idx=i,
                name=str(tc.get("name", "unknown")),
                call_id=str(tc.get("id", f"memory_review_call_{i}")),
                args=args,
            )
        )
    return calls


def run_memory_review(
    *,
    session_state,
    transcript: list[dict],
    model_gateway,
    tool_runtime,
    tools: list[dict] | None,
) -> bool:
    memory_tools = [schema for schema in (tools or []) if schema.get("name") == "memory"]
    if not memory_tools:
        return False

    review_messages = list(transcript[-_MEMORY_REVIEW_WINDOW_MESSAGES:])
    review_messages.append(
        {
            "role": "user",
            "content": (
                "Review the conversation above. Save only durable preferences, "
                "project facts, or conventions with the memory tool."
            ),
        }
    )

    try:
        response = model_gateway.call_once(
            review_messages,
            system=_MEMORY_REVIEW_SYSTEM_PROMPT,
            tools=memory_tools,
            request_options=ModelRequestOptions(
                query_source="memory_review",
                thinking_mode="disabled",
                max_output_tokens=600,
            ),
        )
    except Exception:
        return False

    raw_tool_calls = getattr(response, "tool_calls", []) or []
    if not raw_tool_calls:
        return False

    silent_runtime = ToolExecutorRuntime(
        tool_runtime._registry,
        tool_runtime._context,
        display=RunDisplayOptions(quiet=True),
        renderer=None,
        event_sink=None,
    )
    batch = silent_runtime.execute_batch(
        _parse_tool_calls(raw_tool_calls),
        run_state=RunState(allowed_tools_override={"memory"}),
        apply_session_update=lambda update: apply_session_update(session_state, update),
        apply_run_update=apply_run_update,
    )
    return bool(batch.messages)


def spawn_background_memory_review(
    *,
    session_state,
    transcript: list[dict],
    model_gateway,
    tool_runtime,
    tools: list[dict] | None,
) -> bool:
    memory_tools = [schema for schema in (tools or []) if schema.get("name") == "memory"]
    if not memory_tools:
        return False

    snapshot = [dict(message) for message in transcript]

    def _run() -> None:
        try:
            run_memory_review(
                session_state=session_state,
                transcript=snapshot,
                model_gateway=model_gateway,
                tool_runtime=tool_runtime,
                tools=memory_tools,
            )
        except Exception:
            return

    worker = threading.Thread(target=_run, daemon=True, name="memory-review")
    worker.start()
    return True
