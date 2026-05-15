"""QueryLoop — Agent 核心主循环。

每次用户输入触发一次 QueryLoop.run()，内部是 think-act 循环：
模型思考 → 决定调用工具 → 执行工具 → 把结果喂回模型 → 再思考 → ... → 给出最终回复。

退出循环的三条路径：
1. 模型输出纯文本（无 tool_calls）→ 正常完成
2. 工具调用轮次达到上限 → 强制终止
3. 模型返回空响应且 recovery 也无法挽救 → 失败
"""
from __future__ import annotations

from core.prompt.assembler import build_memory_context_block
from core.query.reducers import (
    TransitionReason,
    apply_run_update,
    apply_session_update,
    apply_transition,
    collect_runtime_maintenance_updates,
)
from core.query.result import QueryResult, StopReason
from core.query.state import RunState
from core.memory.review import spawn_background_memory_review
from core.session.state import TodoItem
from core.tools.context import SessionUpdateKind
from core.tools.runtime import ToolBatchResult, ToolCall
from uuid import uuid4

from core.llm.client import ContextWindowExceededError, ModelRequestOptions, RequestCancelledError
from core.shared.config import PERSIST_PARTIAL_STREAM_OUTPUT, STREAMING_ENABLED
from core.shared.stream_events import StreamAccumulator


# ─── 工具调用解析 ────────────────────────────────────────────────────────────


def _parse_tool_calls(raw_calls: list) -> list[ToolCall]:
    """将模型返回的 tool_call（dict 或 ToolCall 实例）统一为 ToolCall 列表。"""
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


# ─── UI 展示辅助 ──────────────────────────────────────────────────────────────


def _tool_fallback_fragment(call: ToolCall) -> str | None:
    """从工具调用参数中提取一行可读的操作摘要，用于在模型无文字输出时显示状态。

    例如："读取 style-system.md"、"执行: python analyze.py"。
    """
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
        return f"加载 {skill_name} skill" if skill_name else "加载 skill"
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
    if name == "task_plan":
        return "更新任务计划"
    if name == "task_execute":
        task_id = args.get("task_id", "")
        return f"执行任务 {task_id}" if task_id else "执行任务"
    return None


def _build_tool_fallback_status(tool_calls: list[ToolCall]) -> str | None:
    """将一批工具调用拼成一段连贯的操作描述。

    例如："先读取 data.csv；然后执行: python analyze.py。"
    """
    fragments: list[str] = []
    for call in tool_calls:
        fragment = _tool_fallback_fragment(call)
        if not fragment:
            continue
        normalized = fragment.strip().rstrip("。；")
        if normalized:
            fragments.append(normalized)

    if not fragments:
        return None

    parts = [f"先{fragments[0]}"]
    parts.extend(f"然后{fragment}" for fragment in fragments[1:])
    return "；".join(parts) + "。"


def _inject_ephemeral_memory_context(messages: list[dict], recalled: str) -> list[dict]:
    """将 memory-context 注入 API 消息副本，不回写 session transcript。"""
    block = build_memory_context_block(recalled)
    copied = [dict(message) for message in messages]
    if not block:
        return copied

    for index in range(len(copied) - 1, -1, -1):
        if copied[index].get("role") != "user":
            continue
        content = copied[index].get("content", "")
        if not isinstance(content, str):
            content = str(content)
        suffix = f"\n{block}" if content else block
        copied[index]["content"] = content + suffix
        return copied

    copied.append({"role": "user", "content": block})
    return copied


# ─── Todo 展示辅助 ────────────────────────────────────────────────────────────


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
    """检查工具批次中是否有 todo 或 task_state 写入成功。"""
    return any(
        update.kind in {SessionUpdateKind.SET_TODO_ITEMS, SessionUpdateKind.SET_TASK_STATE}
        for update in batch.session_updates
    )


def _render_todo_state_update(renderer, session_state, state: RunState, batch: ToolBatchResult) -> None:
    """工具批次执行后，根据 todo 变化更新 UI 展示。

    三个分支：
    1. todo 列表有变化（与上次展示不同）→ 刷新整个进度条
    2. todo 列表没变但有 in_progress 项 → 显示当前项的简要状态
    3. todo 列表已清空（全部完成）→ 显示完成摘要
    """
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


def _note_assistant_turn(state: RunState, model_resp) -> None:
    """如果本轮模型没有调用 todo 工具，递增计数器。

    当计数器达到阈值（默认 4），TodoPlanningPolicy 会注入
    "计划可能已过时" 的 system-reminder，提醒模型刷新 todo。
    """
    tool_calls = getattr(model_resp, "tool_calls", [])
    if not any(
        (call.get("name") if isinstance(call, dict) else getattr(call, "name", None)) == "todo"
        for call in tool_calls
    ):
        state.assistant_turns_since_todo += 1


# ─── 主循环 ──────────────────────────────────────────────────────────────────


class QueryLoop:
    """Agent 核心主循环：think → act → observe → think → ...

    每次 run() 调用处理一个用户输入，内部可能经历多轮模型调用：
    - 用户说"帮我分析数据" → 模型调用 read_csv → 工具返回数据 → 模型调用 python → ...
    - 直到模型输出最终文本回复，或达到轮次上限。

    外部组件职责：
    - view_builder: 组装发送给模型的输入（system prompt + transcript slice）
    - model_gateway: 执行 API 调用
    - tool_runtime: 执行工具（并行/串行调度）
    - policy_runner: 注入控制消息（todo 提醒、max_turns 强制终止等）
    - recovery: 处理模型空响应等异常
    """

    def _persist_state(self, session_state, store, *, usage: dict[str, int] | None = None) -> None:
        db = getattr(session_state, "session_db", None)
        if db is None:
            return

        all_messages = store.snapshot()
        flushed = int(getattr(session_state, "_last_flushed_idx", 0))
        if len(all_messages) > flushed:
            new_messages = all_messages[flushed:]
            try:
                db.append_messages(session_state.session_id, new_messages)
                session_state._last_flushed_idx = len(all_messages)
            except Exception:
                pass

        if usage:
            try:
                db.update_token_counts(
                    session_state.session_id,
                    int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)),
                )
            except Exception:
                pass

        try:
            from core.session.serializer import SessionSerializer

            state_dict = SessionSerializer.serialize(session_state)
            db.save_session_snapshot(session_state.session_id, state_dict)
        except Exception:
            pass

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
        governor,
        offloader,
        user_message_content: str | None = None,
        tools=None,
        renderer=None,
    ) -> QueryResult:
        """执行一次完整的查询循环。

        Args:
            session_state: 会话级状态（跨 query 持久化），包含对话历史、skill、todo 等。
            store: 消息存储，负责向 conversation_messages 追加消息。
            view_builder: 消息视图构建器，将 PreparedQueryContext 转为 ModelInputView。
            prompt_assembler: 提示词组装器，渲染 system prompt 的各部分。
            model_gateway: 模型网关，执行单次 API 调用。
            tool_runtime: 工具运行时，执行工具调用批次。
            tool_context: 工具上下文，提供工作目录、文件状态等。
            policy_runner: 策略运行器，注入前置/后置消息，控制循环终止。
            recovery: 恢复管理器，处理空响应等异常情况。
            governor: 上下文治理器，水位线评估并产出 PreparedQueryContext。
            offloader: 工具结果 offloader，大工具结果持久化到磁盘。
            tools: 可用工具 schema 列表。
            renderer: UI 渲染器，展示思考过程、assistant 回复、工具状态等。

        Returns:
            QueryResult 包含最终输出、停止原因、使用的轮次等。
        """
        state = RunState()

        prior_user_turn_count = len(session_state.user_intents)
        interval = int(getattr(session_state, "memory_review_interval", 0))
        if prior_user_turn_count > 0 and int(getattr(session_state, "user_turn_count", 0)) == 0:
            session_state.user_turn_count = prior_user_turn_count
            if interval > 0 and int(getattr(session_state, "turns_since_memory_review", 0)) == 0:
                session_state.turns_since_memory_review = prior_user_turn_count % interval

        should_review_memory = False
        if user_message_content:
            session_state.user_turn_count += 1
            if interval > 0 and getattr(session_state, "memory_store", None) is not None:
                session_state.turns_since_memory_review += 1
                if session_state.turns_since_memory_review >= interval:
                    should_review_memory = True
                    session_state.turns_since_memory_review = 0

        # Store user original intent before entering the loop
        if user_message_content:
            session_state.user_intents.append(user_message_content)

        recalled_memory = ""
        provider = getattr(session_state, "memory_provider", None)
        if provider is not None and user_message_content:
            try:
                recalled_memory = provider.prefetch(user_message_content)
            except Exception:
                recalled_memory = ""

        while True:
            for update in collect_runtime_maintenance_updates(session_state):
                apply_session_update(session_state, update)

            # ── 步骤 1：策略注入 ──────────────────────────────────────
            before_messages = policy_runner.before_model_call(session_state, state)
            if before_messages:
                store.extend(before_messages)

            # ── 步骤 2：构建模型输入 ──────────────────────────────────
            working_dir = getattr(tool_context, "working_dir", None) or "."

            # 组织不变的系统提示词+skill meta info
            stable_system = prompt_assembler.build_stable_context(
                session_state,
                project_root=working_dir if tool_context is not None else None,
            )
            # 组织不变的系统工具提示
            stable_tools = prompt_assembler.build_stable_tools(
                session_state,
                tools=tools,
            )
            # 运行时环境信息
            runtime_blocks = prompt_assembler.build_runtime_blocks(
                session_state,
                working_dir=working_dir,
            )
            overlay_blocks = prompt_assembler.build_query_overlay_blocks(session_state, state)

            prepared = governor.assess(
                session_state=session_state,
                run_state=state,
                store=store,
                query_source="main_loop",
                stable_system=stable_system,
                stable_tools=stable_tools,
                runtime_blocks=runtime_blocks,
                overlay_blocks=overlay_blocks,
            )
            if renderer and prepared.observability.get("steps") != ["estimate"]:
                obs = prepared.observability
                before = obs.get("before_tokens", 0)
                after = obs.get("after_tokens", 0)
                saved = before - after
                water_line = obs.get("water_line", "")
                strategies = [
                    step for step in obs.get("steps", [])
                    if step not in ("estimate", "per_message_budget")
                ]
                parts = []
                if water_line:
                    parts.append(f"[{water_line}]")
                if strategies:
                    parts.append("+".join(strategies))
                if saved > 0:
                    parts.append(f"{before//1000}k→{after//1000}k (↓{saved//1000}k)")
                else:
                    parts.append(f"{before//1000}k→{after//1000}k")
                renderer.show_status("上下文管理: " + " ".join(parts))

            view = view_builder.build(
                prepared,
                run_state=state,
            )
            api_messages = _inject_ephemeral_memory_context(view.messages, recalled_memory)

            # ── 步骤 3：调用模型 ─────────────────────────────────────
            active_tools = None if state.stop_reason == "max_turns" else view.tools
            request_options = ModelRequestOptions(
                cancel_check=(lambda: tool_context.cancelled) if tool_context is not None else None
            )

            if STREAMING_ENABLED and hasattr(model_gateway, "stream_once"):
                # Streaming path
                turn_id = uuid4().hex
                accumulator = StreamAccumulator()
                state.current_turn_id = turn_id
                state.current_response_streaming = True
                if renderer is not None and hasattr(renderer, "begin_stream"):
                    renderer.begin_stream(turn_id, {"source": "main_loop"})
                try:
                    for event in model_gateway.stream_once(
                        api_messages,
                        system=view.system,
                        tools=active_tools,
                        request_options=request_options,
                        turn_id=turn_id,
                    ):
                        state.last_stream_sequence = event.sequence
                        state.stream_source_mode = event.source_mode
                        if event.type == "thinking_delta":
                            state.current_thinking_visible = True
                        elif event.type == "content_delta":
                            state.current_content_visible = True
                        accumulator.consume(event)
                        if renderer is not None and hasattr(renderer, "consume_event"):
                            renderer.consume_event(event)
                    model_resp = accumulator.finalize()
                except RequestCancelledError:
                    if renderer is not None and hasattr(renderer, "show_status"):
                        renderer.show_status("已取消当前运行。")
                    self._persist_state(session_state, store)
                    return QueryResult(
                        final_output="已取消当前运行。",
                        stop_reason=StopReason.ABORTED,
                        success=False,
                        turns_used=state.turn_count,
                        tool_calls_executed=state.tool_calls_executed,
                        files_modified=state.files_modified,
                    )
                except ContextWindowExceededError:
                    if state.reactive_recovery_attempted:
                        raise
                    governor.reactive_recover(
                        session_state=session_state,
                        run_state=state,
                        store=store,
                        stable_system=stable_system,
                        stable_tools=stable_tools,
                        runtime_blocks=runtime_blocks,
                        overlay_blocks=overlay_blocks,
                    )
                    state.reactive_recovery_attempted = True
                    state.current_response_streaming = False
                    if renderer is not None and hasattr(renderer, "end_stream"):
                        renderer.end_stream(turn_id, {"source_mode": state.stream_source_mode})
                    continue
                except Exception as exc:
                    snapshot = accumulator.snapshot()
                    if PERSIST_PARTIAL_STREAM_OUTPUT and snapshot["content"]:
                        store.append({"role": "assistant", "content": snapshot["content"], "incomplete": True})
                    if renderer is not None and hasattr(renderer, "show_error"):
                        renderer.show_error(f"模型请求失败: {exc}")
                    state.current_response_streaming = False
                    if renderer is not None and hasattr(renderer, "end_stream"):
                        renderer.end_stream(turn_id, {"source_mode": state.stream_source_mode})
                    return QueryResult(
                        final_output=f"模型请求失败：{exc}",
                        stop_reason=StopReason.API_ERROR,
                        success=False,
                        turns_used=state.turn_count,
                        tool_calls_executed=state.tool_calls_executed,
                        files_modified=state.files_modified,
                    )
                finally:
                    state.current_response_streaming = False
                    if renderer is not None and hasattr(renderer, "end_stream"):
                        renderer.end_stream(turn_id, {"source_mode": state.stream_source_mode})
            else:
                # Batch (non-streaming) path
                try:
                    try:
                        model_resp = model_gateway.call_once(
                            api_messages,
                            system=view.system,
                            tools=active_tools,
                            request_options=request_options,
                        )
                    except TypeError as exc:
                        if "request_options" not in str(exc):
                            raise
                        model_resp = model_gateway.call_once(
                            api_messages,
                            system=view.system,
                            tools=active_tools,
                        )
                except ContextWindowExceededError:
                    if state.reactive_recovery_attempted:
                        raise
                    governor.reactive_recover(
                        session_state=session_state,
                        run_state=state,
                        store=store,
                        stable_system=stable_system,
                        stable_tools=stable_tools,
                        runtime_blocks=runtime_blocks,
                        overlay_blocks=overlay_blocks,
                    )
                    state.reactive_recovery_attempted = True
                    continue
                except RequestCancelledError:
                    self._persist_state(session_state, store)
                    return QueryResult(
                        final_output="已取消当前运行。",
                        stop_reason=StopReason.ABORTED,
                        success=False,
                        turns_used=state.turn_count,
                        tool_calls_executed=state.tool_calls_executed,
                        files_modified=state.files_modified,
                    )
                except Exception as exc:
                    if renderer is not None and hasattr(renderer, "show_error"):
                        renderer.show_error(f"模型请求失败: {exc}")
                    return QueryResult(
                        final_output=f"模型请求失败：{exc}",
                        stop_reason=StopReason.API_ERROR,
                        success=False,
                        turns_used=state.turn_count,
                        tool_calls_executed=state.tool_calls_executed,
                        files_modified=state.files_modified,
                    )
            prompt_tokens = getattr(model_resp, "prompt_tokens", None)
            if isinstance(prompt_tokens, int):
                session_state.compact_state["last_prompt_tokens"] = prompt_tokens

            state.last_model_response = model_resp
            store.append(model_resp.to_message())
            if renderer and getattr(model_resp, "reasoning", "").strip() and not state.current_thinking_visible:
                renderer.show_thinking("思考过程", model_resp.reasoning)

            # ── 分支 0：输出被截断（finish=max_tokens）→ 继续循环 ──
            # 模型的回复被 max_tokens 截断，说明还有内容要输出。
            # 注入一条 user 消息让模型继续，而不是返回不完整的文本。
            # 同时处理截断发生在 tool call 参数中间的情况：tool_calls 存在
            # 但 args 为空/无效，应该走继续逻辑而非执行空参数的工具。
            if getattr(model_resp, "is_truncated", False):
                has_valid_tool_call = any(
                    tc.get("args") or tc.get("name")
                    for tc in (model_resp.tool_calls or [])
                )
                if not has_valid_tool_call:
                    store.append({"role": "user", "content": "你的回复被截断了，请继续完成。"})
                    continue

            # ── 分支 A：已达上限但模型仍想调工具 → 强制终止 ─────────
            if model_resp.tool_calls and state.stop_reason == "max_turns":
                return QueryResult(
                    final_output="",
                    stop_reason=StopReason.MAX_TURNS,
                    success=False,
                    turns_used=state.turn_count,
                    tool_calls_executed=state.tool_calls_executed,
                    files_modified=state.files_modified,
                )

            # ── 分支 B：模型要求调用工具 → 执行工具批次 ─────────────
            if model_resp.tool_calls:
                parsed_calls = _parse_tool_calls(model_resp.tool_calls)

                # UI 展示：如果有文字就显示文字，否则显示工具操作摘要
                # 流式模式已通过 Live 实时展示文本，不再重复显示
                if renderer:
                    if model_resp.content.strip() and not state.current_content_visible:
                        renderer.show_assistant(model_resp.content)
                    else:
                        fallback_status = _build_tool_fallback_status(parsed_calls)
                        if fallback_status:
                            renderer.show_status(fallback_status)

                # 追踪连续未写 todo 的轮次
                _note_assistant_turn(state, model_resp)

                # 执行工具（readonly 并行，write 串行）
                batch = tool_runtime.execute_batch(
                    parsed_calls,
                    run_state=state,
                    apply_session_update=lambda update: apply_session_update(session_state, update),
                    apply_run_update=apply_run_update,
                )
                tool_name_by_id = {call.call_id: call.name for call in parsed_calls}
                persisted_messages = []
                for message in batch.messages:
                    rewritten = dict(message)
                    if rewritten.get("role") == "tool" and rewritten.get("tool_call_id"):
                        tool_name = tool_name_by_id.get(rewritten["tool_call_id"], "")
                        if tool_name != "read_file":
                            rewritten["content"] = offloader.maybe_persist(
                                rewritten["tool_call_id"],
                                str(rewritten.get("content", "")),
                                tool_name=tool_name,
                            )
                    persisted_messages.append(rewritten)
                store.extend(persisted_messages)
                self._persist_state(session_state, store)
                state.turn_count += 1
                state.tool_calls_executed += len(parsed_calls)
                apply_transition(state, TransitionReason.NEXT_TURN)
                _render_todo_state_update(renderer, session_state, state, batch)

                # 策略后置注入（目前为空，预留扩展点）
                after_messages = policy_runner.after_tool_batch(session_state, state, batch)
                if after_messages:
                    store.extend(after_messages)

                # 检查是否达到 max_turns
                stop_reason = policy_runner.should_stop(session_state, state)
                if stop_reason == "max_turns" and state.stop_reason != "max_turns":
                    state.stop_reason = "max_turns"
                    apply_transition(state, TransitionReason.MAX_TURNS_RECOVERY)
                    # 注入一条 user 消息，让模型知道该收尾了
                    store.append({"role": "user", "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"})
                    continue

                continue

            # ── 分支 C：模型输出最终文本 → 正常完成 ────────────────
            if model_resp.has_final_text:
                provider = getattr(session_state, "memory_provider", None)
                if provider is not None and user_message_content:
                    try:
                        provider.sync_turn(user_message_content, model_resp.content)
                        provider.queue_prefetch(user_message_content)
                    except Exception:
                        pass

                if should_review_memory:
                    try:
                        spawn_background_memory_review(
                            session_state=session_state,
                            transcript=store.snapshot(),
                            model_gateway=model_gateway,
                            tool_runtime=tool_runtime,
                            tools=tools,
                        )
                    except Exception:
                        pass

                self._persist_state(
                    session_state,
                    store,
                    usage={
                        "input_tokens": getattr(model_resp, "prompt_tokens", 0),
                        "output_tokens": getattr(model_resp, "completion_tokens", 0),
                    },
                )
                return QueryResult(
                    final_output=model_resp.content,
                    stop_reason=StopReason.MAX_TURNS if state.stop_reason == "max_turns" else StopReason.COMPLETED,
                    turns_used=state.turn_count,
                    tool_calls_executed=state.tool_calls_executed,
                    files_modified=state.files_modified,
                    streaming_displayed=state.current_content_visible,
                )

            # ── 分支 D：模型返回空响应 → 交给 recovery 处理 ────────
            # recovery 可能注入追问消息让模型重试，也可能判定为不可恢复
            decision = recovery.handle(model_resp, state)
            if decision.should_continue:
                if decision.transition_reason is not None:
                    apply_transition(state, decision.transition_reason)
                store.extend(decision.follow_up_messages)
                continue

            return QueryResult(
                final_output="",
                stop_reason=StopReason.EMPTY_RESPONSE,
                success=False,
                turns_used=state.turn_count,
                tool_calls_executed=state.tool_calls_executed,
                files_modified=state.files_modified,
            )
