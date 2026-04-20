from __future__ import annotations

from core.query.result import QueryResult, StopReason
from core.query.state import RunState
from core.session.state import TodoItem
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


def _tool_fallback_fragment(call: ToolCall) -> str | None:
    """从工具调用中生成包含具体信息的 fallback 文本。"""
    args = call.args or {}
    name = call.name

    if name == "bash":
        cmd = args.get("command", "")
        preview = cmd[:80] + ("..." if len(cmd) > 80 else "")
        return f"执行: {preview}" if preview else "执行命令"
    if name == "read_file":
        path = args.get("file_path", args.get("path", ""))
        return f"读取 {path}" if path else "读取文件"
    if name == "skill":
        skill_name = args.get("skill", "")
        return f"加载 {skill_name} skill，再重新评估下一步"
    if name == "todo":
        return "更新计划"
    if name == "edit_file":
        path = args.get("file_path", args.get("path", ""))
        return f"编辑 {path}" if path else "编辑文件"
    if name == "write_file":
        path = args.get("file_path", args.get("path", ""))
        return f"写入 {path}" if path else "写入文件"
    if name == "find":
        pattern = args.get("pattern", "")
        return f"搜索: {pattern}" if pattern else "搜索文件"
    return None


def _build_tool_fallback_status(tool_calls: list[ToolCall]) -> str | None:
    fragments: list[str] = []
    for call in tool_calls:
        fragment = _tool_fallback_fragment(call)
        if not fragment:
            continue
        normalized = fragment.strip().rstrip("。；")
        if normalized:
            fragments.append(normalized)
        if call.name == "skill":
            break

    if not fragments:
        return None

    parts = [f"先{fragments[0]}"]
    parts.extend(f"然后{fragment}" for fragment in fragments[1:])
    return "；".join(parts) + "。"


def _clone_todo_items(items: list[TodoItem]) -> list[TodoItem]:
    return [
        TodoItem(
            content=item.content,
            active_form=item.active_form,
            status=item.status,
            workflow_ref=item.workflow_ref,
        )
        for item in items
    ]


def _todo_write_succeeded(batch: ToolBatchResult) -> bool:
    successes = batch.tool_successes or []
    return any(
        name == "todo" and idx < len(successes) and successes[idx]
        for idx, name in enumerate(batch.tool_names)
    )


def _render_todo_state_update(renderer, session_state, state: RunState, batch: ToolBatchResult) -> None:
    if renderer is None or not _todo_write_succeeded(batch):
        return

    todo_state = session_state.todo_state
    if todo_state.items:
        if state.last_displayed_todo_items != todo_state.items:
            renderer.show_progress(todo_state.items)
            state.last_displayed_todo_items = _clone_todo_items(todo_state.items)
            return

        current = next((item for item in todo_state.items if item.status == "in_progress"), None)
        if current is not None:
            completed = sum(1 for item in todo_state.items if item.status == "completed")
            renderer.show_current_todo(current, completed, len(todo_state.items))
        return

    if todo_state.last_completed_items:
        renderer.show_completion_summary(
            completed=len(todo_state.last_completed_items),
            total=len(todo_state.last_completed_items),
            elapsed=0.0,
        )
        state.last_displayed_todo_items = []
        return

    state.last_displayed_todo_items = []


def _apply_batch_control_plane(state: RunState, batch: ToolBatchResult) -> None:
    """将工具批次中的控制面信号应用到 RunState。

    处理三种信号：
    - ContextPatch: 工具对 allowed_tools / model / effort 的覆盖请求
    - Barrier: 工具要求停止当前批次（如 skill_expanded）
    - Todo 写入成功: 重置 replan 计数器（除非同时有 skill_expanded barrier）
    """
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
    """管理一次 query run 的唯一主循环。

    循环流程：
    1. 调用 policy_runner 注入前置消息
    2. 通过 view_builder 构建 ModelInputView（组装 system + 截取 transcript）
    3. 调用 model_gateway 发送请求（system 和 messages 分通道传递）
    4. 若模型返回 tool_calls → 执行工具批次 → 应用控制面补丁 → 继续循环
    5. 若模型返回最终文本 → 返回 QueryResult
    6. 若模型返回空响应 → 交给 recovery 处理
    """

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
        """执行一次完整的查询循环。

        Args:
            session_state: 会话级状态（跨 query 持久化），包含对话历史、skill、todo 等。
            store: 消息存储，负责向 conversation_messages 追加消息。
            view_builder: 消息视图构建器，将 state 转为 ModelInputView。
            prompt_assembler: 提示词组装器，渲染 system prompt 的各部分。
            model_gateway: 模型网关，执行单次 API 调用。
            tool_runtime: 工具运行时，执行工具调用批次。
            tool_context: 工具上下文，提供工作目录、文件状态等。
            policy_runner: 策略运行器，注入前置/后置消息，控制循环终止。
            recovery: 恢复管理器，处理空响应等异常情况。
            renderer: UI 渲染器，展示思考过程、assistant 回复、工具状态等。

        Returns:
            QueryResult 包含最终输出、停止原因、使用的轮次等。
        """
        state = RunState()

        while True:
            before_messages = policy_runner.before_model_call(session_state, state)
            if before_messages:
                store.extend(before_messages)

            working_dir = getattr(tool_context, "working_dir", None) or "."
            view = view_builder.build(
                session_state,
                run_state=state,
                prompt_assembler=prompt_assembler,
                working_dir=working_dir,
                project_root=getattr(tool_context, "working_dir", None),
            )
            active_tools = None if state.stop_reason == "max_turns" else view.tools
            model_resp = model_gateway.call_once(view.messages, system=view.system, tools=active_tools)
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
                parsed_calls = _parse_tool_calls(model_resp.tool_calls)
                if renderer:
                    if model_resp.content.strip():
                        renderer.show_assistant(model_resp.content)
                    else:
                        fallback_status = _build_tool_fallback_status(parsed_calls)
                        if fallback_status:
                            renderer.show_status(fallback_status)
                _note_assistant_turn(state, model_resp)
                batch = tool_runtime.execute_batch(parsed_calls)
                store.extend(batch.tool_results)
                state.turn_count += 1
                state.tool_calls_executed += len(parsed_calls)
                state.files_modified.extend(batch.files_modified)

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
                _render_todo_state_update(renderer, session_state, state, batch)

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
