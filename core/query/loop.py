from __future__ import annotations

import json

from core.query.result import QueryResult, StopReason
from core.query.state import RunState
from core.tools.runtime import ToolCall


def _parse_tool_calls(raw_calls: list) -> list[ToolCall]:
    """Convert raw tool_call dicts or API objects into ToolCall instances."""
    calls: list[ToolCall] = []
    for i, tc in enumerate(raw_calls):
        if isinstance(tc, ToolCall):
            calls.append(tc)
        elif isinstance(tc, dict):
            func = tc.get("function", {})
            raw_args = func.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    args = {"_parse_error": str(raw_args)}
            else:
                args = raw_args if isinstance(raw_args, dict) else {}
            calls.append(ToolCall(
                idx=i,
                name=tc.get("name", func.get("name", "unknown")),
                call_id=tc.get("id", f"call_{i}"),
                args=args,
            ))
        else:
            # API object (e.g. openai ChatCompletionMessageToolCall)
            raw_args = tc.function.arguments
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except (json.JSONDecodeError, TypeError):
                args = {"_parse_error": str(raw_args)}
            calls.append(ToolCall(
                idx=i,
                name=tc.function.name,
                call_id=tc.id,
                args=args if isinstance(args, dict) else {},
            ))
    return calls


class QueryLoop:
    """管理一次 query run 的唯一主循环。"""

    def run(
        self,
        *,
        session_state,
        store,
        view_builder,
        prompt_assembler,
        model_gateway,
        tool_runtime,
        tool_context,
        policy_runner,
        recovery,
    ) -> QueryResult:
        state = RunState()

        while True:
            before_messages = policy_runner.before_model_call(session_state, state)
            if before_messages:
                store.extend(before_messages)

            view = view_builder.build(session_state)
            active_tools = None if state.stop_reason == "max_turns" else view.tools
            model_resp = model_gateway.call_once(view.messages, tools=active_tools)
            state.last_model_response = model_resp
            store.append(model_resp.to_message())

            if model_resp.tool_calls and state.stop_reason == "max_turns":
                return QueryResult(
                    final_output="",
                    stop_reason=StopReason.MAX_TURNS,
                    success=False,
                    turns_used=state.turn_count,
                    tool_calls_executed=state.tool_calls_executed,
                    files_modified=state.files_modified,
                )

            if model_resp.tool_calls:
                parsed_calls = _parse_tool_calls(model_resp.tool_calls)
                batch = tool_runtime.execute_batch(parsed_calls)
                store.extend(batch.tool_results)
                state.turn_count += 1
                state.tool_calls_executed += len(parsed_calls)
                state.files_modified.extend(batch.files_modified)
                after_messages = policy_runner.after_tool_batch(session_state, state, batch)
                if after_messages:
                    store.extend(after_messages)
                stop_reason = policy_runner.should_stop(session_state, state)
                if stop_reason == "max_turns" and state.stop_reason != "max_turns":
                    state.stop_reason = "max_turns"
                    store.append({"role": "user", "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"})
                    continue
                continue

            if model_resp.has_final_text:
                return QueryResult(
                    final_output=model_resp.content,
                    stop_reason=StopReason.MAX_TURNS if state.stop_reason == "max_turns" else StopReason.COMPLETED,
                    turns_used=state.turn_count,
                    tool_calls_executed=state.tool_calls_executed,
                    files_modified=state.files_modified,
                )

            decision = recovery.handle(model_resp, state)
            if decision.should_continue:
                store.extend(decision.follow_up_messages)
                state.empty_retry_count += 1
                continue

            return QueryResult(
                final_output="",
                stop_reason=StopReason.EMPTY_RESPONSE,
                success=False,
                turns_used=state.turn_count,
                tool_calls_executed=state.tool_calls_executed,
                files_modified=state.files_modified,
            )
