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

from .context import ContextPatch, ExecutionBarrier, ToolResult, ToolUseContext
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
    tool_results: list[dict[str, Any]]
    files_modified: list[str]
    tool_names: list[str]
    injected_messages: list[dict[str, Any]]
    context_patches: list[ContextPatch]
    barrier: ExecutionBarrier | None
    tool_successes: list[bool] | None = None


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

    def execute_batch(self, tool_calls: list[ToolCall]) -> ToolBatchResult:
        """接收一批 tool_call，分批执行，返回有序结果。"""
        if not tool_calls:
            return ToolBatchResult(
                tool_results=[], files_modified=[], tool_names=[],
                injected_messages=[], context_patches=[], barrier=None, tool_successes=[],
            )

        # If any call is a skill tool, use sequential barrier-aware execution
        if any(call.name == "skill" for call in tool_calls):
            return self._execute_with_barrier(tool_calls)

        batches = self._partition(tool_calls)
        all_results: dict[int, ToolResult] = {}

        if not self._display.quiet:
            sys.stdout.write(f"\033[36m[Runtime] 收到 {len(tool_calls)} 个 tool_call，分为 {len(batches)} 批：\033[0m\n")
            for i, b in enumerate(batches):
                mode = "并行" if b.parallel else "串行"
                names = [c.name for c in b.calls]
                sys.stdout.write(f"\033[36m[Runtime]   Batch {i}: [{mode}] {names}\033[0m\n")

        batch_start = time.time()
        for batch in batches:
            if batch.parallel:
                batch_results = self._execute_parallel(batch)
            else:
                batch_results = self._execute_serial(batch)
            all_results.update(batch_results)

        elapsed = time.time() - batch_start
        if not self._display.quiet:
            sys.stdout.write(f"\033[36m[Runtime] 全部完成，耗时 {elapsed:.2f}s\033[0m\n")

        ordered_calls = tool_calls
        ordered_results = [all_results[i] for i in range(len(tool_calls))]
        if self._renderer is not None and not self._display.quiet:
            for call, result in zip(ordered_calls, ordered_results):
                self._renderer.show_tool_call(call.name, call.args)
                self._renderer.show_tool_result(call.name, result.output)
        tool_messages = [
            {
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": result.output,
            }
            for call, result in zip(ordered_calls, ordered_results)
        ]

        # Collect injected_messages, context_patches, and barrier from results
        injected_messages: list[dict[str, Any]] = []
        context_patches: list[ContextPatch] = []
        barrier: ExecutionBarrier | None = None
        for result in all_results.values():
            injected_messages.extend(result.injected_messages)
            if result.context_patch is not None:
                context_patches.append(result.context_patch)
            if result.barrier is not None and result.barrier.stop_after_tool:
                barrier = result.barrier

        return ToolBatchResult(
            tool_results=tool_messages,
            files_modified=self._context.files_modified,
            tool_names=[call.name for call in ordered_calls],
            injected_messages=injected_messages,
            context_patches=context_patches,
            barrier=barrier,
            tool_successes=[result.success for result in ordered_results],
        )

    def _execute_with_barrier(self, tool_calls: list[ToolCall]) -> ToolBatchResult:
        """Execute tool calls sequentially with barrier awareness.

        If a tool returns a barrier, subsequent calls get skipped results.
        """
        ordered_results: dict[int, ToolResult] = {}
        injected_messages: list[dict[str, Any]] = []
        context_patches: list[ContextPatch] = []
        barrier: ExecutionBarrier | None = None

        if not self._display.quiet:
            sys.stdout.write(f"\033[36m[Runtime] 收到 {len(tool_calls)} 个 tool_call（含 skill，顺序执行）\033[0m\n")

        for pos, call in enumerate(tool_calls):
            if barrier is not None:
                # This and all subsequent calls are skipped
                ordered_results[call.idx] = ToolResult(
                    output=f"(skipped: superseded by {barrier.reason} barrier; re-issue after re-evaluation if still needed)",
                    success=False,
                    error="skipped",
                )
                continue

            result = self._run_single(call)
            ordered_results[call.idx] = result
            injected_messages.extend(result.injected_messages)
            if result.context_patch is not None:
                context_patches.append(result.context_patch)
            if result.barrier is not None and result.barrier.stop_after_tool:
                barrier = result.barrier

        # Build tool result messages
        tool_messages = [
            {
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": ordered_results[call.idx].output,
            }
            for call in tool_calls
        ]

        # Render results
        if self._renderer is not None and not self._display.quiet:
            for call in tool_calls:
                result = ordered_results[call.idx]
                self._renderer.show_tool_call(call.name, call.args)
                self._renderer.show_tool_result(call.name, result.output)

        return ToolBatchResult(
            tool_results=tool_messages,
            files_modified=self._context.files_modified,
            tool_names=[call.name for call in tool_calls],
            injected_messages=injected_messages,
            context_patches=context_patches,
            barrier=barrier,
            tool_successes=[ordered_results[call.idx].success for call in tool_calls],
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

    def _execute_parallel(self, batch: _Batch) -> dict[int, ToolResult]:
        results: dict[int, ToolResult] = {}
        if not self._display.quiet:
            sys.stdout.write(f"\033[36m[Runtime] ▶ 并行执行 {len(batch.calls)} 个只读工具：{[c.name for c in batch.calls]}\033[0m\n")

        with ThreadPoolExecutor(max_workers=len(batch.calls)) as pool:
            futures = {
                pool.submit(self._run_single, call): call.idx
                for call in batch.calls
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    results[idx] = ToolResult(
                        output=f"Internal error: {e}",
                        success=False,
                        error="internal_error",
                    )

        return results

    def _execute_serial(self, batch: _Batch) -> dict[int, ToolResult]:
        results: dict[int, ToolResult] = {}
        for call in batch.calls:
            if not self._display.quiet:
                sys.stdout.write(f"\033[36m[Runtime] ▶ 串行执行写工具：{call.name}\033[0m\n")
            results[call.idx] = self._run_single(call)
            if (
                call.name == "todo"
                and results[call.idx].success
                and self._renderer is not None
                and not self._display.quiet
            ):
                todo_state = getattr(self._context.session_state, "todo_state", None)
                if todo_state is not None and todo_state.items:
                    self._renderer.show_progress(todo_state.items)
                elif todo_state is not None and todo_state.last_completed_items:
                    self._renderer.show_completion_summary(
                        completed=len(todo_state.last_completed_items),
                        total=len(todo_state.last_completed_items),
                        elapsed=0.0,
                    )
        return results

    def _run_single(self, call: ToolCall) -> ToolResult:
        self._context._set_call_identity(
            name=call.name, call_id=call.call_id, turn=self._context.turn_count
        )
        start = time.time()

        result_holder: list[ToolResult] = []
        error_holder: list[Exception] = []

        def run():
            try:
                result = self._registry.execute(call.name, call.args, self._context)
                if len(result.output) > MAX_OUTPUT_CHARS:
                    truncated_output = result.output[:MAX_OUTPUT_CHARS]
                    result = ToolResult(
                        output=truncated_output + f"\n\n... (输出已截断，原始 {len(result.output)} 字符，显示前 {MAX_OUTPUT_CHARS} 字符)",
                        success=result.success,
                        error=result.error,
                        truncated=True,
                        injected_messages=result.injected_messages,
                        context_patch=result.context_patch,
                        barrier=result.barrier,
                    )
                result_holder.append(result)
            except Exception as e:
                error_holder.append(e)

        thread = threading.Thread(target=run)
        thread.start()

        shown_progress = False
        while thread.is_alive():
            thread.join(timeout=1.0)
            if thread.is_alive():
                elapsed = int(time.time() - start)
                if elapsed >= 2:
                    if not self._display.quiet:
                        sys.stdout.write(f"\r\033[K\033[36m[Runtime]   ⏳ {call.name} 执行中... {elapsed}s\033[0m")
                        sys.stdout.flush()
                        shown_progress = True

        if shown_progress and not self._display.quiet:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        elapsed = time.time() - start

        if error_holder:
            if not self._display.quiet:
                sys.stdout.write(f"\033[36m[Runtime]   ✗ {call.name} 异常 ({elapsed:.2f}s): {error_holder[0]}\033[0m\n")
            return ToolResult(
                output=f"Internal error: {error_holder[0]}",
                success=False,
                error="internal_error",
            )

        result = result_holder[0]
        if not self._display.quiet:
            sys.stdout.write(f"\033[36m[Runtime]   ✓ {call.name} 完成 ({elapsed:.2f}s, success={result.success})\033[0m\n")
        return result
