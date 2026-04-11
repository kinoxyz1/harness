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

from .tools import ToolResult, ToolUseContext
from .config import MAX_OUTPUT_CHARS


@dataclass
class ToolCall:
    """对 API 返回的 tool_call 的内部表示。"""

    idx: int                    # 在原始列表中的位置（保证回写顺序）
    name: str                   # 工具名
    call_id: str                # API 的 tool_call.id
    args: dict[str, Any]        # 解析后的参数


@dataclass
class _Batch:
    """一批可一起执行的 tool call（内部使用）。"""

    calls: list[ToolCall]
    parallel: bool              # True = 可并行，False = 独占串行


class ToolExecutorRuntime:
    """工具执行运行时。"""

    def __init__(self, registry, context: ToolUseContext):
        self._registry = registry
        self._context = context

    def execute_batch(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """接收一批 tool_call，分批执行，返回有序结果。"""
        if not tool_calls:
            return []

        batches = self._partition(tool_calls)
        all_results: dict[int, ToolResult] = {}

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
        sys.stdout.write(f"\033[36m[Runtime] 全部完成，耗时 {elapsed:.2f}s\033[0m\n")

        # 按原始顺序返回
        return [all_results[i] for i in range(len(tool_calls))]

    def _partition(self, calls: list[ToolCall]) -> list[_Batch]:
        """按 READONLY 分批：连续只读 → 并行 batch，写 → 独占 batch。"""
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
        """并行执行只读工具，结果按 idx 排序。"""
        results: dict[int, ToolResult] = {}
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
        """串行执行写工具（独占）。"""
        results: dict[int, ToolResult] = {}
        for call in batch.calls:
            sys.stdout.write(f"\033[36m[Runtime] ▶ 串行执行写工具：{call.name}\033[0m\n")
            results[call.idx] = self._run_single(call)
        return results

    def _run_single(self, call: ToolCall) -> ToolResult:
        """执行单个工具调用（注入身份 + 异常保护 + 进度显示）。"""
        self._context._set_call_identity(
            name=call.name, call_id=call.call_id, turn=self._context.turn_count
        )
        start = time.time()

        # 在后台线程执行工具，主线程显示进度
        result_holder: list[ToolResult] = []
        error_holder: list[Exception] = []

        def run():
            try:
                result = self._registry.execute(call.name, call.args, self._context)
                # Truncate oversized output
                if len(result.output) > MAX_OUTPUT_CHARS:
                    truncated_output = result.output[:MAX_OUTPUT_CHARS]
                    result = ToolResult(
                        output=truncated_output + f"\n\n... (输出已截断，原始 {len(result.output)} 字符，显示前 {MAX_OUTPUT_CHARS} 字符)",
                        success=result.success,
                        error=result.error,
                        truncated=True,
                    )
                result_holder.append(result)
            except Exception as e:
                error_holder.append(e)

        thread = threading.Thread(target=run)
        thread.start()

        # 主线程：等待执行，超过 2 秒显示进度
        shown_progress = False
        while thread.is_alive():
            thread.join(timeout=1.0)
            if thread.is_alive():
                elapsed = int(time.time() - start)
                if elapsed >= 2:
                    sys.stdout.write(f"\r\033[K\033[36m[Runtime]   ⏳ {call.name} 执行中... {elapsed}s\033[0m")
                    sys.stdout.flush()
                    shown_progress = True

        if shown_progress:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        elapsed = time.time() - start

        if error_holder:
            sys.stdout.write(f"\033[36m[Runtime]   ✗ {call.name} 异常 ({elapsed:.2f}s): {error_holder[0]}\033[0m\n")
            return ToolResult(
                output=f"Internal error: {error_holder[0]}",
                success=False,
                error="internal_error",
            )

        result = result_holder[0]
        sys.stdout.write(f"\033[36m[Runtime]   ✓ {call.name} 完成 ({elapsed:.2f}s, success={result.success})\033[0m\n")
        return result
