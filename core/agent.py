from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

from rich.console import Console
from rich.panel import Panel

from .config import MAX_TOKENS, MAX_TURNS, MODEL, ENABLE_THINKING, SHOW_THINKING
from .context import get_system_context, get_user_context
from .llm import create_llm_client
from .protocol import normalize_messages
from .tools import ToolResult, ToolUseContext, registry
from .runtime import ToolExecutorRuntime, ToolCall

console = Console()

# ─── LLM 调用层 ──────────────────────────────────────────


class LLMResponse:
    """对 API response 的结构化封装。

    所有调用者通过这个类访问响应，不需要关心底层 SDK 的差异。
    提供 has_content / is_tool_call / is_truncated 等语义化属性，
    而不是让调用者自己去猜 finish_reason。
    """

    def __init__(self, response) -> None:
        self._response = response
        self._choice = response.choices[0]
        self._msg = self._choice.message
        self._finish_reason = self._choice.finish_reason

        # 安全提取字段（兼容不同 SDK 版本）
        self.content: str | None = self._msg.content if hasattr(self._msg, "content") else None
        self.reasoning: str | None = getattr(self._msg, "reasoning_content", None)
        self.tool_calls = getattr(self._msg, "tool_calls", None)
        self.finish_reason: str = self._finish_reason or "unknown"

        # token 用量
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        if hasattr(response, "usage") and response.usage:
            self.prompt_tokens = response.usage.prompt_tokens or 0
            self.completion_tokens = response.usage.completion_tokens or 0

    @property
    def has_content(self) -> bool:
        """是否有用户可见的文字内容（忽略纯空白）。"""
        return bool(self.content and self.content.strip())

    @property
    def is_tool_call(self) -> bool:
        """模型是否请求工具调用。"""
        if self.finish_reason == "tool_calls":
            return True
        # 防御：某些思考模型可能返回 tool_calls 但 finish_reason 不是 "tool_calls"
        if self.tool_calls and not self.has_content:
            return True
        return False

    @property
    def is_truncated(self) -> bool:
        """模型是否被 token 限制截断。"""
        return self.finish_reason == "length"

    @property
    def raw_response(self) -> Any:
        """原始 response 对象（需要时使用）。"""
        return self._response

    def to_message_dict(self) -> dict[str, Any]:
        """转换为可追加到 messages 的字典。"""
        try:
            return self._msg.model_dump()
        except Exception:
            d: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
            if self.tool_calls:
                d["tool_calls"] = [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in self.tool_calls
                ]
            return d


def _call_llm(client, messages, tools=None, stream=False) -> LLMResponse:
    """调用 LLM API，返回结构化的 LLMResponse。

    显示动态计时，完成后打印耗时和 token 用量。
    所有调用者拿到统一的 LLMResponse，不需要关心底层细节。
    """
    params = {
        "model": MODEL,
        "messages": normalize_messages(messages, enable_thinking=ENABLE_THINKING),
        "extra_body": {"enable_thinking": ENABLE_THINKING, "parallel_tool_calls": True},
        "max_tokens": MAX_TOKENS,
    }
    if tools:
        params["tools"] = tools
    if stream:
        params["stream"] = True

    # 流式调用：直接返回（暂不封装 LLMResponse）
    if stream:
        raw = client.chat.completions.create(**params)
        return raw  # type: ignore[return-value]

    # 非流式调用：API 放后台线程，主线程负责刷新计时显示
    result: dict[str, Any] = {}
    error: dict[str, Any] = {}

    def do_call():
        try:
            result["data"] = client.chat.completions.create(**params)
        except Exception as e:
            error["data"] = e

    start = time.time()
    thread = threading.Thread(target=do_call)
    thread.start()

    # 主线程：每秒刷新计时
    while thread.is_alive():
        elapsed = int(time.time() - start)
        sys.stdout.write(f"\r\033[K\033[32m正在思考... {elapsed}s\033[0m")
        sys.stdout.flush()
        thread.join(timeout=1.0)

    # 清除计时行
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()

    if error.get("data"):
        raise error["data"]

    response = result["data"]
    elapsed = time.time() - start

    llm_resp = LLMResponse(response)

    console.print(
        f"[dim]{elapsed:.1f}s │ token {llm_resp.prompt_tokens}↓ {llm_resp.completion_tokens}↑"
        f" │ finish={llm_resp.finish_reason}[/dim]"
    )

    return llm_resp


# ─── 工具调用解析 ─────────────────────────────────────────


def _parse_tool_args(raw: str | None) -> dict[str, Any]:
    """解析工具调用参数，解析失败时返回包含错误信息的字典。"""
    if raw is None:
        return {"_parse_error": "Arguments is None"}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return {"_parse_error": f"Invalid JSON: {e}"}


# ─── 显示 ────────────────────────────────────────────────


def _print_assistant_msg(msg_dict: dict[str, Any] | LLMResponse) -> None:
    """打印助手消息的推理内容和文字内容。"""
    if isinstance(msg_dict, LLMResponse):
        reasoning = msg_dict.reasoning
        content = msg_dict.content
    else:
        reasoning = msg_dict.get("reasoning_content")
        content = msg_dict.get("content")

    if SHOW_THINKING and reasoning and reasoning.strip():
        console.print(Panel(reasoning, title="思考过程", border_style="dim"))

    if content and content.strip():
        print(content)


# ─── 上下文注入 ──────────────────────────────────────────


def _inject_system_context(messages: list[dict[str, Any]]) -> None:
    """将通用系统提示词追加到已有的系统消息中。"""
    marker = "<!-- system-context-injected -->"
    for msg in messages:
        if msg.get("role") == "system" and marker in (msg.get("content") or ""):
            return

    system_ctx = get_system_context(os.getcwd())

    for msg in messages:
        if msg.get("role") == "system":
            existing = msg.get("content") or ""
            msg["content"] = f"{existing}\n\n{marker}\n\n{system_ctx}"
            return

    messages.insert(0, {
        "role": "system",
        "content": f"{marker}\n\n{system_ctx}",
    })


def _inject_user_context(messages: list[dict[str, Any]]) -> None:
    """在消息列表中注入环境信息。"""
    marker = "<!-- user-context-injected -->"
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content", "").startswith(marker):
            return

    user_ctx = get_user_context(os.getcwd())
    content = f"{marker}\n{user_ctx}"

    insert_pos = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            insert_pos = i
            break
    else:
        insert_pos = len(messages)

    messages.insert(insert_pos, {"role": "user", "content": content})


# ─── 核心循环 ─────────────────────────────────────────────


def _execute_tool_turn(
    llm_resp: LLMResponse,
    messages: list[dict[str, Any]],
    tool_context: ToolUseContext,
    client,
    tools_schema: list[dict[str, Any]],
) -> LLMResponse:
    """执行一轮工具调用：解析 → 执行 → 回写 → 再次调用 LLM。"""
    # 打印助手消息（可能有文字 + 工具调用）
    _print_assistant_msg(llm_resp)
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

    # 分离：解析成功 vs 失败（JSON 截断）
    valid_calls = []
    for tc in tool_calls:
        if "_parse_error" in tc.args:
            console.print(f"[red]Parse error: {tc.name}: {tc.args['_parse_error']}[/red]")
        else:
            valid_calls.append(tc)

    # 执行工具 — 只执行解析成功的调用
    if valid_calls:
        runtime = ToolExecutorRuntime(registry, tool_context)
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
        console.print(f"\n[yellow]$ {tc.args.get('command', tc.name)}[/yellow]")
        print(result.output)

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
    return _call_llm(client, messages, tools=tools_schema)


def _ensure_final_response(
    llm_resp: LLMResponse,
    messages: list[dict[str, Any]],
    client,
    tools_schema: list[dict[str, Any]],
    max_retries: int = 2,
) -> LLMResponse:
    """确保模型产出用户可见的内容。

    处理三种异常情况：
    1. finish_reason == "length": 被截断，发"请继续"
    2. finish_reason == "stop" + 空 content: 思考模型用完 token 做推理
    3. 其他: 兜底重试

    与之前的 _handle_empty_content 的关键区别：
    - 用 has_content（.strip()）而非 truthy 检测
    - 有最大重试次数，不会死循环
    - 重试后如果模型要调工具，允许它调用
    - 每次重试用不同策略
    """
    for attempt in range(max_retries):
        if llm_resp.has_content:
            return llm_resp

        # 诊断信息
        reason = llm_resp.finish_reason
        has_reasoning = bool(llm_resp.reasoning and llm_resp.reasoning.strip())
        console.print(
            f"[dim](finish_reason={reason}, "
            f"content={'空' if not llm_resp.content else f'{len(llm_resp.content)}字符(空白)'}, "
            f"reasoning={'有' if has_reasoning else '无'}, "
            f"重试 {attempt + 1}/{max_retries})[/dim]"
        )

        # 根据原因选择不同的 follow-up
        # 关键原则：有 tools 时鼓励继续任务，不阻断工作流
        if llm_resp.is_truncated:
            follow_up = "你的回复被截断了，请继续输出。"
        elif tools_schema:
            # 有工具可用 → 鼓励模型继续完成任务（不要停在中间）
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
            llm_resp = _call_llm(client, messages, tools=tools_schema)

            # 如果重试后模型要调工具，执行一轮完整的工具循环
            if llm_resp.is_tool_call and tools_schema:
                tool_context = ToolUseContext(working_dir=os.getcwd(), max_turns=MAX_TURNS)
                tool_context.set_messages(messages)
                llm_resp = _run_tool_loop(llm_resp, messages, tool_context, client, tools_schema)

        except Exception as e:
            console.print(f"[red]重试失败: {e}[/red]")
            # 构造一个兜底 LLMResponse
            return llm_resp  # 返回最后一次有效的响应

    # 重试用完，如果还是没有内容，手动构造提示
    if not llm_resp.has_content:
        console.print("[yellow](模型多次未返回有效内容，请尝试继续对话)[/yellow]")

    return llm_resp


def _run_tool_loop(
    llm_resp: LLMResponse,
    messages: list[dict[str, Any]],
    tool_context: ToolUseContext,
    client,
    tools_schema: list[dict[str, Any]],
) -> LLMResponse:
    """运行工具调用循环。

    退出条件：
    1. 模型返回可见内容（has_content）— 任务完成
    2. turn_count >= max_turns — 安全兜底
    3. 空内容重试耗尽（思考模型常见）

    与旧版关键区别：空内容不再立即退出循环，而是在循环内重试。
    这解决了思考模型（如 kimi-k2-thinking）在工具执行后返回空 content 的问题——
    模型可能在推理阶段用完了 token 预算，但实际任务并未完成。
    """
    turn_count = tool_context.turn_count
    empty_retries = 0
    MAX_EMPTY_RETRIES = 3

    while True:
        # 1) 正常工具调用
        if llm_resp.is_tool_call:
            empty_retries = 0  # 成功的工具调用重置计数器
            turn_count += 1
            tool_context._turn_count = turn_count

            # 安全兜底
            if turn_count >= tool_context.max_turns:
                messages.append({
                    "role": "user",
                    "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。",
                })
                llm_resp = _call_llm(client, messages, tools=tools_schema)
                break

            llm_resp = _execute_tool_turn(llm_resp, messages, tool_context, client, tools_schema)
            continue

        # 2) 有可见内容 → 正常退出
        if llm_resp.has_content:
            break

        # 3) 空内容（思考模型常见）→ 在循环内重试
        if empty_retries < MAX_EMPTY_RETRIES:
            empty_retries += 1
            has_reasoning = bool(llm_resp.reasoning and llm_resp.reasoning.strip())
            console.print(
                f"[dim](工具循环中模型返回空内容，"
                f"finish_reason={llm_resp.finish_reason}，"
                f"reasoning={'有' if has_reasoning else '无'}，"
                f"重试 {empty_retries}/{MAX_EMPTY_RETRIES})[/dim]"
            )

            # 将空响应和继续指令加入消息（content 用 "" 而非 None，避免 normalize 丢弃）
            messages.append({"role": "assistant", "content": llm_resp.content or ""})
            messages.append({"role": "user", "content": (
                "你刚才执行了工具操作，但还没有输出结果。"
                "请继续完成你的任务——如果需要读取文件、执行命令或其他操作，请使用工具；"
                "如果已经完成所有步骤，请给出最终的分析结果和行动建议。"
            )})
            llm_resp = _call_llm(client, messages, tools=tools_schema)
            continue

        # 4) 重试耗尽，退出
        break

    return llm_resp


# ─── 入口 ────────────────────────────────────────────────


def agent_loop(messages: list[dict[str, Any]]) -> None:
    """运行一次代理循环：调用 LLM，执行工具。

    架构保证：
    - agent_loop 永远不会静默退出（要么显示内容，要么显示错误）
    - 所有 LLM 响应都经过 LLMResponse 封装
    - 空 content 会被 _ensure_final_response 自动处理
    """
    client = create_llm_client()
    tools_schema = registry.schemas()

    # 注入上下文
    _inject_system_context(messages)
    _inject_user_context(messages)

    # 首次 LLM 调用
    try:
        llm_resp = _call_llm(client, messages, tools=tools_schema)
    except Exception as e:
        console.print(f"[red]API 调用失败: {e}[/red]")
        return

    # 非工具调用响应 — 确保有内容后直接结束
    if not llm_resp.is_tool_call:
        llm_resp = _ensure_final_response(llm_resp, messages, client, None)
        _print_assistant_msg(llm_resp)
        messages.append({"role": "assistant", "content": llm_resp.content or ""})
        return

    # 工具调用循环
    tool_context = ToolUseContext(working_dir=os.getcwd(), max_turns=MAX_TURNS)
    tool_context.set_messages(messages)

    try:
        llm_resp = _run_tool_loop(llm_resp, messages, tool_context, client, tools_schema)
    except Exception as e:
        console.print(f"[red]工具执行异常: {e}[/red]")
        import traceback
        traceback.print_exc()
        return

    # 确保最终有可见内容
    llm_resp = _ensure_final_response(llm_resp, messages, client, tools_schema)
    _print_assistant_msg(llm_resp)
    messages.append({"role": "assistant", "content": llm_resp.content or ""})
