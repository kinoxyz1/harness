"""代理循环编排器。

将 LLM 调用、工具执行、上下文注入、显示渲染组合为完整的代理循环。
对外暴露 AgentLoop 类（依赖注入）和 agent_loop() 向后兼容入口。
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import time
import traceback
from typing import Any

from .config import MAX_TURNS
from .context import ContextPipeline, SystemContextPlugin, UserContextPlugin
from .llm_client import LLMResponse, OpenAIClient, _parse_tool_args
from .renderer import RichRenderer
from .runtime import ToolCall, ToolExecutorRuntime
from .run_options import RunDisplayOptions
from .tools import ToolResult, ToolUseContext, registry
from .tools.todo import get_state, increment_rounds, reset_rounds


# ─── AgentLoop ────────────────────────────────────────────


@dataclass
class AgentRunResult:
    """单次 agent 运行的结构化结果。"""

    final_output: str
    success: bool
    stop_reason: str
    turns_used: int
    files_modified: list[str]


class AgentLoop:
    """代理循环编排器。持有三个可替换依赖，对外暴露 run()。"""

    def __init__(
        self,
        llm: Any,
        renderer: Any,
        context: ContextPipeline,
        tools_schema: list[dict[str, Any]] | None = None,
        display: RunDisplayOptions | None = None,
    ) -> None:
        self._llm = llm
        self._renderer = renderer
        self._context = context
        self._tools_schema = tools_schema or registry.schemas()
        self._display = display or RunDisplayOptions()

    def run(
        self,
        messages: list[dict[str, Any]],
        *,
        tool_context: ToolUseContext | None = None,
    ) -> AgentRunResult:
        """运行一次代理循环。等价于原 agent_loop()。"""
        # 1. 注入上下文
        self._context.inject_all(messages)

        # 2. 首次 LLM 调用
        try:
            llm_resp = self._llm.call(messages, tools=self._tools_schema, display=self._display)
        except Exception as e:
            self._renderer.show_error(f"API 调用失败: {e}")
            return self._build_run_result(
                tool_context=tool_context,
                final_output="",
                success=False,
                stop_reason="api_error",
            )

        # 3. 非工具调用响应 — 确保有内容后直接结束
        if not llm_resp.is_tool_call:
            llm_resp = self._ensure_final_response(llm_resp, messages, None, tool_context)
            self._print_response(llm_resp)
            final_output = llm_resp.content or ""
            messages.append({"role": "assistant", "content": final_output})
            return self._build_run_result(
                tool_context=tool_context,
                final_output=final_output,
                success=bool(final_output.strip()),
                stop_reason="completed" if final_output.strip() else "empty_response",
            )

        # 4. 工具调用循环
        tool_context = tool_context or ToolUseContext(working_dir=os.getcwd(), max_turns=MAX_TURNS)
        tool_context.set_messages(messages)

        try:
            llm_resp, stop_reason = self._run_tool_loop(llm_resp, messages, tool_context)
        except Exception as e:
            self._renderer.show_error(f"工具执行异常: {e}")
            traceback.print_exc()
            return self._build_run_result(
                tool_context=tool_context,
                final_output="",
                success=False,
                stop_reason="tool_error",
            )

        # 5. 确保最终有可见内容
        final_tools_schema = None if stop_reason == "max_turns" else self._tools_schema
        llm_resp = self._ensure_final_response(llm_resp, messages, final_tools_schema, tool_context)
        if stop_reason != "max_turns" and llm_resp.has_content:
            stop_reason = "completed"
        elif stop_reason == "completed" and not llm_resp.has_content:
            stop_reason = "empty_response"

        self._print_response(llm_resp)
        final_output = llm_resp.content or ""
        messages.append({"role": "assistant", "content": final_output})
        return self._build_run_result(
            tool_context=tool_context,
            final_output=final_output,
            success=bool(final_output.strip()),
            stop_reason=stop_reason,
        )

    def _build_run_result(
        self,
        *,
        tool_context: ToolUseContext | None,
        final_output: str,
        success: bool,
        stop_reason: str,
    ) -> AgentRunResult:
        files_modified = list(getattr(tool_context, "files_modified", [])) if tool_context else []
        turns_used = tool_context.turn_count if tool_context else 0
        return AgentRunResult(
            final_output=final_output,
            success=success,
            stop_reason=stop_reason,
            turns_used=turns_used,
            files_modified=files_modified,
        )

    # ─── 显示 ─────────────────────────────────────────────

    def _print_response(self, llm_resp: LLMResponse) -> None:
        """显示助手回复（思考过程 + 文字内容）。"""
        self._renderer.show_thinking("思考过程", llm_resp.reasoning or "")
        self._renderer.show_assistant(llm_resp.content)

    # ─── 工具执行 ─────────────────────────────────────────

    def _execute_tool_turn(
        self,
        llm_resp: LLMResponse,
        messages: list[dict[str, Any]],
        tool_context: ToolUseContext,
    ) -> tuple[LLMResponse, list[str]]:
        """执行一轮工具调用：解析 -> 执行 -> 回写 -> 再次调用 LLM。

        Returns:
            (llm_response, list_of_called_tool_names)
        """
        # 打印助手消息（可能有文字 + 工具调用）
        self._print_response(llm_resp)
        messages.append(llm_resp.to_message_dict())

        # 解析 tool calls
        tool_calls = [
            ToolCall(
                idx=i,
                name=tc.function.name,
                call_id=tc.id,
                args=_parse_tool_args(tc.function.arguments),
            )
            for i, tc in enumerate(llm_resp.tool_calls)
        ]

        called_tool_names = [tc.name for tc in tool_calls]

        # 分离：解析成功 vs 失败（JSON 截断）
        valid_calls = []
        for tc in tool_calls:
            if "_parse_error" in tc.args:
                self._renderer.show_error(f"Parse error: {tc.name}: {tc.args['_parse_error']}")
            else:
                valid_calls.append(tc)

        # 执行工具 — 只执行解析成功的调用
        if valid_calls:
            runtime = ToolExecutorRuntime(registry, tool_context, display=self._display)
            valid_results = runtime.execute_batch(valid_calls)
            valid_result_map = {tc.idx: r for tc, r in zip(valid_calls, valid_results)}
        else:
            valid_result_map = {}

        # 构建完整结果列表（解析失败的用指导性错误代替）
        tool_result_list = []
        for tc in tool_calls:
            if tc.idx in valid_result_map:
                tool_result_list.append(valid_result_map[tc.idx])
            else:
                # 根据工具类型给出针对性的恢复建议
                if tc.name == "write_file":
                    recovery_hint = (
                        f"错误：write_file 参数被截断（{tc.args['_parse_error']}）。\n"
                        f"文件内容太大，无法在单次调用中写入。\n\n"
                        f"请使用分块写入策略：\n"
                        f"1. write_file(path, 第一块内容, mode='write')  ← 创建文件\n"
                        f"2. write_file(path, 第二块内容, mode='append') ← 追加\n"
                        f"3. write_file(path, 第三块内容, mode='append') ← 追加\n"
                        f"... 直到所有内容写完。每块控制在 100 行以内。"
                    )
                else:
                    recovery_hint = (
                        f"错误：工具调用参数被截断，JSON 解析失败（{tc.args['_parse_error']}）。\n"
                        f"请尝试减少参数内容后重试。"
                    )
                tool_result_list.append(ToolResult(
                    output=recovery_hint,
                    success=False,
                    error="json_parse_error",
                ))

        # 打印工具结果
        for tc, result in zip(tool_calls, tool_result_list):
            self._renderer.show_tool_call(tc.name, tc.args)
            self._renderer.show_tool_result(tc.name, result.output)

        # 回写 messages
        tool_results = [
            {
                "role": "tool",
                "tool_call_id": tc.call_id,
                "content": r.output,
            }
            for tc, r in zip(tool_calls, tool_result_list)
        ]
        messages.extend(tool_results)

        # 携带工具结果再次调用 LLM
        return self._llm.call(messages, tools=self._tools_schema, display=self._display), called_tool_names

    # ─── 空内容恢复 ───────────────────────────────────────

    def _ensure_final_response(
        self,
        llm_resp: LLMResponse,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
        tool_context: ToolUseContext | None = None,
        max_retries: int = 2,
    ) -> LLMResponse:
        """确保模型产出用户可见的内容。

        处理三种异常情况：
        1. finish_reason == "length": 被截断，发"请继续"
        2. finish_reason == "stop" + 空 content: 思考模型用完 token 做推理
        3. 其他: 兜底重试
        """
        for attempt in range(max_retries):
            if llm_resp.has_content:
                return llm_resp

            # 诊断信息
            reason = llm_resp.finish_reason
            has_reasoning = bool(llm_resp.reasoning and llm_resp.reasoning.strip())
            self._renderer.show_status(
                f"(finish_reason={reason}, "
                f"content={'空' if not llm_resp.content else f'{len(llm_resp.content)}字符(空白)'}, "
                f"reasoning={'有' if has_reasoning else '无'}, "
                f"重试 {attempt + 1}/{max_retries})"
            )

            # 根据原因选择不同的 follow-up
            if llm_resp.is_truncated:
                follow_up = "你的回复被截断了，请继续输出。"
            elif tools_schema:
                follow_up = (
                    "你刚才的回复似乎没有输出。请继续完成你的任务。"
                    "如果需要读取文件、执行命令或其他操作，请使用工具。"
                )
            elif has_reasoning:
                follow_up = "请基于你的分析，用中文给出回答。不需要再推理，直接输出结论即可。"
            else:
                follow_up = "请用中文回答用户的问题。"

            # 追加 assistant（可能为空）+ user follow-up
            messages.append({"role": "assistant", "content": llm_resp.content or ""})
            messages.append({"role": "user", "content": follow_up})

            try:
                llm_resp = self._llm.call(messages, tools=tools_schema, display=self._display)

                # 如果重试后模型要调工具，执行一轮完整的工具循环
                if llm_resp.is_tool_call and tools_schema:
                    retry_context = tool_context or ToolUseContext(
                        working_dir=os.getcwd(),
                        max_turns=MAX_TURNS,
                    )
                    retry_context.set_messages(messages)
                    llm_resp, _ = self._run_tool_loop(llm_resp, messages, retry_context)

            except Exception as e:
                self._renderer.show_error(f"重试失败: {e}")
                return llm_resp

        if not llm_resp.has_content:
            self._renderer.show_status("(模型多次未返回有效内容，请尝试继续对话)")

        return llm_resp

    # ─── 工具循环 ─────────────────────────────────────────

    def _run_tool_loop(
        self,
        llm_resp: LLMResponse,
        messages: list[dict[str, Any]],
        tool_context: ToolUseContext,
    ) -> tuple[LLMResponse, str]:
        """运行工具调用循环。

        退出条件：
        1. 模型返回可见内容（has_content）— 任务完成
        2. turn_count >= max_turns — 安全兜底
        3. 空内容重试耗尽（思考模型常见）
        """
        turn_count = tool_context.turn_count
        empty_retries = 0
        MAX_EMPTY_RETRIES = 3
        stop_reason = "completed"

        while True:
            # 1) 正常工具调用
            if llm_resp.is_tool_call:
                empty_retries = 0
                turn_count += 1
                tool_context._turn_count = turn_count

                # 安全兜底
                if turn_count >= tool_context.max_turns:
                    messages.append({
                        "role": "user",
                        "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。",
                    })
                    llm_resp = self._llm.call(messages, tools=None, display=self._display)
                    stop_reason = "max_turns"
                    break

                llm_resp, called_tools = self._execute_tool_turn(llm_resp, messages, tool_context)

                # Todo 进度跟踪（通过 todo_manage 工具）
                if "todo" in called_tools:
                    state = get_state()
                    self._renderer.show_progress(state.items)
                    reset_rounds()
                else:
                    rounds = increment_rounds()
                    if rounds >= 3:
                        messages.append({
                            "role": "user",
                            "content": "<reminder>重新评估你的计划，更新进度后再继续。</reminder>",
                        })

                continue

            # 2) 有可见内容 → 退出
            if llm_resp.has_content:
                break

            # 3) 空内容（思考模型常见）→ 在循环内重试
            if empty_retries < MAX_EMPTY_RETRIES:
                empty_retries += 1
                has_reasoning = bool(llm_resp.reasoning and llm_resp.reasoning.strip())
                self._renderer.show_status(
                    f"(工具循环中模型返回空内容，"
                    f"finish_reason={llm_resp.finish_reason}，"
                    f"reasoning={'有' if has_reasoning else '无'}，"
                    f"重试 {empty_retries}/{MAX_EMPTY_RETRIES})"
                )

                messages.append({"role": "assistant", "content": llm_resp.content or ""})
                messages.append({"role": "user", "content": (
                    "你刚才执行了工具操作，但还没有输出结果。"
                    "请继续完成你的任务——如果需要读取文件、执行命令或其他操作，请使用工具；"
                    "如果已经完成所有步骤，请给出最终的分析结果和行动建议。"
                )})
                llm_resp = self._llm.call(messages, tools=self._tools_schema, display=self._display)
                continue

            # 4) 重试耗尽，退出
            stop_reason = "empty_response"
            break

        return llm_resp, stop_reason


# ─── 向后兼容入口 ─────────────────────────────────────────


def agent_loop(messages: list[dict[str, Any]]) -> None:
    """向后兼容入口。内部装配默认依赖。"""
    llm = OpenAIClient()
    renderer = RichRenderer()
    context = ContextPipeline()
    context.register(SystemContextPlugin())
    context.register(UserContextPlugin())
    AgentLoop(llm, renderer, context).run(messages)


def plan_phase(messages: list[dict[str, Any]], client) -> None:
    """兼容层：规划阶段已删除，此函数不再执行任何操作。"""
    pass
