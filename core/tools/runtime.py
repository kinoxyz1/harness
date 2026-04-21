"""工具执行运行时：管理一批 tool_call 的编排执行。

职责：分批（只读并行/写独占串行）、回写顺序保证、异常保护、context 注入。
不负责：LLM 调用、循环控制、messages 管理。
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .context import (
    RunUpdate,
    SessionUpdate,
    ToolInvocationOutcome,
    ToolOutcomeStatus,
    ToolUseContext,
)
from ..shared.config import MAX_OUTPUT_CHARS
from ..shared.run_options import RunDisplayOptions


@dataclass
class ToolCall:
    """对 API 返回的 tool_call 的内部表示。"""

    idx: int
    name: str
    call_id: str
    args: dict[str, Any]


@dataclass(slots=True)
class ToolBatchResult:
    messages: list[dict[str, Any]]
    tool_names: list[str]
    tool_statuses: list[ToolOutcomeStatus]
    session_updates: list[SessionUpdate]
    run_updates: list[RunUpdate]


@dataclass
class _Batch:
    """一批可一起执行的 tool call（内部使用）。"""

    calls: list[ToolCall]
    parallel: bool


class ToolExecutorRuntime:
    """工具执行运行时。"""

    def __init__(
        self,
        registry,
        context: ToolUseContext,
        display: RunDisplayOptions | None = None,
        renderer=None,
    ):
        self._registry = registry
        self._context = context
        self._display = display or RunDisplayOptions()
        self._renderer = renderer

    def _trace_enabled(self) -> bool:
        return (not self._display.quiet) and self._display.runtime_trace == "debug"

    def _should_render_generic_tool_event(self, tool_name: str) -> bool:
        if tool_name != "todo":
            return True
        return self._display.runtime_trace == "debug"

    def execute_batch(
        self,
        tool_calls: list[ToolCall],
        *,
        run_state,
        apply_session_update,
        apply_run_update,
    ) -> ToolBatchResult:
        """接收一批 tool_call，分批执行，返回有序结果。"""
        if not tool_calls:
            return ToolBatchResult(
                messages=[],
                tool_names=[],
                tool_statuses=[],
                session_updates=[],
                run_updates=[],
            )

        batches = self._partition(tool_calls)
        all_results: dict[int, ToolInvocationOutcome] = {}
        applied_session_updates: list[SessionUpdate] = []
        applied_run_updates: list[RunUpdate] = []

        if self._trace_enabled():
            sys.stdout.write(f"\033[36m[Runtime] 收到 {len(tool_calls)} 个 tool_call，分为 {len(batches)} 批：\033[0m\n")
            for i, b in enumerate(batches):
                mode = "并行" if b.parallel else "串行"
                names = [c.name for c in b.calls]
                sys.stdout.write(f"\033[36m[Runtime]   Batch {i}: [{mode}] {names}\033[0m\n")

        batch_start = time.time()
        for batch in batches:
            if batch.parallel:
                batch_results = self._execute_parallel(batch, run_state=run_state)
                for call in batch.calls:
                    result = batch_results[call.idx]
                    self._apply_updates(
                        outcome=result,
                        run_state=run_state,
                        apply_session_update=apply_session_update,
                        apply_run_update=apply_run_update,
                        applied_session_updates=applied_session_updates,
                        applied_run_updates=applied_run_updates,
                    )
            else:
                batch_results = self._execute_serial(
                    batch,
                    run_state=run_state,
                    apply_session_update=apply_session_update,
                    apply_run_update=apply_run_update,
                    applied_session_updates=applied_session_updates,
                    applied_run_updates=applied_run_updates,
                )
            all_results.update(batch_results)

        elapsed = time.time() - batch_start
        if self._trace_enabled():
            sys.stdout.write(f"\033[36m[Runtime] 全部完成，耗时 {elapsed:.2f}s\033[0m\n")

        ordered_calls = tool_calls
        ordered_results = [all_results[call.idx] for call in ordered_calls]
        if self._renderer is not None and not self._display.quiet:
            for call, result in zip(ordered_calls, ordered_results):
                if not self._should_render_generic_tool_event(call.name):
                    continue
                self._renderer.show_tool_call(call.name, call.args)
                self._renderer.show_tool_result(call.name, self._first_content(result))

        return ToolBatchResult(
            messages=self._flatten_outcome_messages(ordered_calls, ordered_results),
            tool_names=[call.name for call in ordered_calls],
            tool_statuses=[result.status for result in ordered_results],
            session_updates=applied_session_updates,
            run_updates=applied_run_updates,
        )

    def _partition(self, calls: list[ToolCall]) -> list[_Batch]:
        batches: list[_Batch] = []
        current_parallel: list[ToolCall] = []

        for call in calls:
            if self._registry.is_readonly(call.name):
                current_parallel.append(call)
            else:
                if current_parallel:
                    batches.append(_Batch(current_parallel, parallel=True))
                    current_parallel = []
                batches.append(_Batch([call], parallel=False))

        if current_parallel:
            batches.append(_Batch(current_parallel, parallel=True))

        return batches

    def _execute_parallel(
        self,
        batch: _Batch,
        *,
        run_state,
    ) -> dict[int, ToolInvocationOutcome]:
        results: dict[int, ToolInvocationOutcome] = {}
        turn = getattr(run_state, "turn_count", 0)
        if self._trace_enabled():
            sys.stdout.write(f"\033[36m[Runtime] ▶ 并行执行 {len(batch.calls)} 个只读工具：{[c.name for c in batch.calls]}\033[0m\n")

        executable_calls: list[ToolCall] = []
        allowed_tools = getattr(run_state, "allowed_tools_override", None)
        for call in batch.calls:
            if allowed_tools is not None and call.name not in allowed_tools:
                results[call.idx] = self._make_rejected_outcome(call, allowed_tools)
            else:
                executable_calls.append(call)

        if not executable_calls:
            return results

        with ThreadPoolExecutor(max_workers=len(executable_calls)) as pool:
            futures = {
                pool.submit(self._run_single, call, turn=turn): call
                for call in executable_calls
            }
            for future in as_completed(futures):
                call = futures[future]
                try:
                    results[call.idx] = future.result()
                except Exception as e:
                    results[call.idx] = ToolInvocationOutcome(
                        status=ToolOutcomeStatus.FAILURE,
                        error="internal_error",
                        messages=[
                            {
                                "role": "tool",
                                "tool_call_id": call.call_id,
                                "content": f"Internal error: {e}",
                            }
                        ],
                    )

        return results

    def _execute_serial(
        self,
        batch: _Batch,
        *,
        run_state,
        apply_session_update,
        apply_run_update,
        applied_session_updates: list[SessionUpdate],
        applied_run_updates: list[RunUpdate],
    ) -> dict[int, ToolInvocationOutcome]:
        results: dict[int, ToolInvocationOutcome] = {}
        turn = getattr(run_state, "turn_count", 0)
        for call in batch.calls:
            if self._trace_enabled():
                sys.stdout.write(f"\033[36m[Runtime] ▶ 串行执行写工具：{call.name}\033[0m\n")
            allowed_tools = getattr(run_state, "allowed_tools_override", None)
            if allowed_tools is not None and call.name not in allowed_tools:
                outcome = self._make_rejected_outcome(call, allowed_tools)
            else:
                outcome = self._run_single(call, turn=turn)
            results[call.idx] = outcome
            self._apply_updates(
                outcome=outcome,
                run_state=run_state,
                apply_session_update=apply_session_update,
                apply_run_update=apply_run_update,
                applied_session_updates=applied_session_updates,
                applied_run_updates=applied_run_updates,
            )
        return results

    def _apply_updates(
        self,
        *,
        outcome: ToolInvocationOutcome,
        run_state,
        apply_session_update,
        apply_run_update,
        applied_session_updates: list[SessionUpdate],
        applied_run_updates: list[RunUpdate],
    ) -> None:
        for update in outcome.run_updates:
            apply_run_update(run_state, update)
            applied_run_updates.append(update)
        for update in outcome.session_updates:
            apply_session_update(update)
            applied_session_updates.append(update)

    def _first_content(self, outcome: ToolInvocationOutcome) -> str:
        if not outcome.messages:
            return ""
        first = outcome.messages[0]
        if not isinstance(first, dict):
            return str(first)
        content = first.get("content", "")
        if content is None:
            return ""
        return content if isinstance(content, str) else str(content)

    def _make_rejected_outcome(
        self,
        call: ToolCall,
        allowed_tools: set[str],
    ) -> ToolInvocationOutcome:
        allowed_str = ", ".join(sorted(allowed_tools))
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.BLOCKED,
            error="rejected_tool",
            messages=[
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": f"Tool '{call.name}' rejected: not allowed by runtime policy. allowed_tools=[{allowed_str}]",
                }
            ],
        )

    def _flatten_outcome_messages(
        self,
        ordered_calls: list[ToolCall],
        ordered_results: list[ToolInvocationOutcome],
    ) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        for call, outcome in zip(ordered_calls, ordered_results):
            for message in outcome.messages:
                if isinstance(message, dict):
                    normalized = dict(message)
                    normalized.setdefault("role", "tool")
                    normalized.setdefault("tool_call_id", call.call_id)
                    flattened.append(normalized)
                else:
                    flattened.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": str(message),
                        }
                    )
        return flattened

    def _build_call_context(self, call: ToolCall, *, turn: int) -> ToolUseContext:
        call_context = ToolUseContext(
            working_dir=self._context.working_dir,
            max_turns=self._context.max_turns,
        )
        # Keep per-call identity isolated while sharing runtime state handles.
        call_context._file_state = self._context._file_state
        call_context._cancelled = self._context._cancelled
        call_context._session_state = self._context._session_state
        call_context._skill_registry = self._context._skill_registry
        call_context._set_call_identity(
            name=call.name, call_id=call.call_id, turn=turn
        )
        return call_context

    def _run_single(self, call: ToolCall, *, turn: int) -> ToolInvocationOutcome:
        call_context = self._build_call_context(call, turn=turn)
        start = time.time()

        result_holder: list[ToolInvocationOutcome] = []
        error_holder: list[Exception] = []

        def run():
            try:
                outcome = self._registry.execute(call.name, call.args, call_context)
                result_holder.append(self._truncate_first_message(outcome))
            except Exception as e:
                error_holder.append(e)

        thread = threading.Thread(target=run)
        thread.start()

        shown_trace_progress = False
        shown_compact_status = False
        while thread.is_alive():
            thread.join(timeout=1.0)
            if thread.is_alive():
                elapsed = int(time.time() - start)
                if elapsed >= 2:
                    if self._trace_enabled():
                        sys.stdout.write(f"\r\033[K\033[36m[Runtime]   ⏳ {call.name} 执行中... {elapsed}s\033[0m")
                        sys.stdout.flush()
                        shown_trace_progress = True
                    elif (
                        self._renderer is not None
                        and not self._display.quiet
                        and not shown_compact_status
                    ):
                        self._renderer.show_status(f"{call.name} 执行中... {elapsed}s")
                        shown_compact_status = True

        if shown_trace_progress and not self._display.quiet:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        elapsed = time.time() - start

        if error_holder:
            if self._trace_enabled():
                sys.stdout.write(f"\033[36m[Runtime]   ✗ {call.name} 异常 ({elapsed:.2f}s): {error_holder[0]}\033[0m\n")
            return ToolInvocationOutcome(
                status=ToolOutcomeStatus.FAILURE,
                error="internal_error",
                messages=[
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": f"Internal error: {error_holder[0]}",
                    }
                ],
            )

        result = result_holder[0] if result_holder else ToolInvocationOutcome(
            status=ToolOutcomeStatus.FAILURE,
            error="missing_outcome",
            messages=[
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": "Internal error: missing tool outcome",
                }
            ],
        )
        if self._trace_enabled():
            success = result.status == ToolOutcomeStatus.SUCCESS
            sys.stdout.write(f"\033[36m[Runtime]   ✓ {call.name} 完成 ({elapsed:.2f}s, success={success})\033[0m\n")
        return result

    def _truncate_first_message(self, outcome: ToolInvocationOutcome) -> ToolInvocationOutcome:
        if not outcome.messages:
            return outcome
        first = outcome.messages[0]
        if not isinstance(first, dict):
            return outcome
        content = first.get("content")
        if content is None:
            return outcome
        text = content if isinstance(content, str) else str(content)
        if len(text) <= MAX_OUTPUT_CHARS:
            return outcome
        truncated = (
            text[:MAX_OUTPUT_CHARS]
            + f"\n\n... (输出已截断，原始 {len(text)} 字符，显示前 {MAX_OUTPUT_CHARS} 字符)"
        )
        copied_messages: list[dict[str, Any]] = []
        for message in outcome.messages:
            if isinstance(message, dict):
                copied_messages.append(dict(message))
            else:
                copied_messages.append({"role": "tool", "content": str(message)})
        copied_messages[0]["content"] = truncated
        return ToolInvocationOutcome(
            status=outcome.status,
            session_updates=list(outcome.session_updates),
            run_updates=list(outcome.run_updates),
            messages=copied_messages,
            error=outcome.error,
        )
