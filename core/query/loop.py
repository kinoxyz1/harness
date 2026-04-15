from __future__ import annotations

from core.query.result import QueryResult, StopReason
from core.query.state import RunState
from core.tools.runtime import ToolBatchResult, ToolCall


def _parse_tool_calls(raw_calls: list) -> list[ToolCall]:
    """将归一 tool_call dict 或 ToolCall 实例转为 ToolCall 列表。"""
    calls: list[ToolCall] = []
    for i, tc in enumerate(raw_calls):
        if isinstance(tc, ToolCall):
            calls.append(tc)
        elif isinstance(tc, dict):
            args = tc.get("args", {})
            if not isinstance(args, dict):
                args = {}
            calls.append(ToolCall(
                idx=i,
                name=tc.get("name", "unknown"),
                call_id=tc.get("id", f"call_{i}"),
                args=args,
            ))
    return calls


def _apply_batch_control_plane(state: RunState, batch: ToolBatchResult) -> None:
    """Apply context patches and barrier from tool batch to run state."""
    skill_expanded_barrier = False
    for patch in batch.context_patches:
        if patch.allowed_tools is not None:
            state.allowed_tools_override = (
                patch.allowed_tools
                if state.allowed_tools_override is None
                else state.allowed_tools_override & patch.allowed_tools
            )
        if patch.model_override is not None:
            state.model_override = patch.model_override
        if patch.effort_override is not None:
            state.effort_override = patch.effort_override
    if batch.barrier is not None:
        state.barrier_reason = batch.barrier.reason
        if batch.barrier.reason == "skill_expanded":
            skill_expanded_barrier = True
            state.todo_replan_required = True
            state.todo_replan_reason = "skill_expanded"

    todo_succeeded = False
    tool_successes = getattr(batch, "tool_successes", None) or []
    for idx, tool_name in enumerate(getattr(batch, "tool_names", [])):
        if tool_name == "todo" and idx < len(tool_successes) and tool_successes[idx]:
            todo_succeeded = True
            break

    if todo_succeeded and not skill_expanded_barrier:
        state.todo_replan_required = False
        state.todo_replan_reason = None
        state.assistant_turns_since_todo = 0


def _note_assistant_turn(state: RunState, model_resp) -> None:
    """Track assistant turns that did not produce a todo write opportunity."""
    tool_calls = getattr(model_resp, "tool_calls", [])
    if not any(
        (call.get("name") if isinstance(call, dict) else getattr(call, "name", None)) == "todo"
        for call in tool_calls
    ):
        state.assistant_turns_since_todo += 1


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
        renderer=None,
    ) -> QueryResult:
        state = RunState()

        while True:
            before_messages = policy_runner.before_model_call(session_state, state)
            if before_messages:
                store.extend(before_messages)

            view = view_builder.build(session_state, run_state=state)
            active_tools = None if state.stop_reason == "max_turns" else view.tools
            model_resp = model_gateway.call_once(view.messages, tools=active_tools)
            if renderer and getattr(model_resp, "reasoning", "").strip():
                renderer.show_thinking("思考过程", model_resp.reasoning)
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
                _note_assistant_turn(state, model_resp)
                parsed_calls = _parse_tool_calls(model_resp.tool_calls)
                batch = tool_runtime.execute_batch(parsed_calls)
                store.extend(batch.tool_results)
                state.turn_count += 1
                state.tool_calls_executed += len(parsed_calls)
                state.files_modified.extend(batch.files_modified)

                # Handle injected messages (from skill expansion)
                if batch.injected_messages:
                    store.extend(batch.injected_messages)
                    # Record skill events for model-initiated skill calls
                    skill_calls = [call for call in parsed_calls if call.name == "skill"]
                    for call in skill_calls:
                        from core.skills.models import SkillEvent
                        session_state.skill_events.append(
                            SkillEvent(
                                skill_id=call.args.get("skill", ""),
                                action="activated",
                                source="model_tool_call",
                                conversation_index=len(session_state.conversation_messages) - 1,
                            )
                        )

                # Apply context patches and barrier
                _apply_batch_control_plane(state, batch)

                after_messages = policy_runner.after_tool_batch(session_state, state, batch)
                if after_messages:
                    store.extend(after_messages)
                stop_reason = policy_runner.should_stop(session_state, state)
                if stop_reason == "max_turns" and state.stop_reason != "max_turns":
                    state.stop_reason = "max_turns"
                    store.append({"role": "user", "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"})
                    continue
                if batch.barrier is not None:
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
