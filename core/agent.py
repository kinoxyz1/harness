from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .config import MAX_TOKENS, MAX_TURNS, MODEL
from .llm import create_llm_client
from .tools import TOOLS, execute_tool

console = Console()

def _call_llm(client, messages, tools=None, stream=False):
    """调用 LLM API，显示动态计时，完成后打印耗时和 token 用量。"""
    params = {
        "model": MODEL,
        "messages": messages,
        "extra_body": {"enable_thinking": True},
        "max_tokens": MAX_TOKENS,
    }
    if tools:
        params["tools"] = tools
    if stream:
        params["stream"] = True

    # 流式调用：直接返回 Stream 对象，不走后台线程
    if stream:
        return client.chat.completions.create(**params)

    # 非流式调用：API 放后台线程，主线程负责刷新计时显示
    result = {}
    error = {}

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

    if not stream and hasattr(response, "usage") and response.usage:
        console.print(
            f"[dim]{elapsed:.1f}s │ token {response.usage.prompt_tokens}↓ {response.usage.completion_tokens}↑[/dim]"
        )

    return response

def _parse_tool_args(raw: str) -> dict[str, Any]:
    """解析工具调用参数，解析失败时返回包含错误信息的字典。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        return {"_parse_error": f"Invalid JSON: {e}"}

def print_response(msg: dict[str, Any]) -> None:
    """打印助手回复消息，如有推理内容则一并显示。"""
    reasoning = msg.get("reasoning_content")
    if reasoning:
        console.print(Panel(reasoning, title="思考过程", border_style="dim"))

    content = msg.get("content")
    if content:
        console.print(content)

def agent_loop(messages: list[dict[str, Any]]) -> None:
    """运行一次代理循环：调用大型语言模型(LLM)，执行工具。"""
    client = create_llm_client()

    response = _call_llm(client, messages, tools=TOOLS)

    choice = response.choices[0]

    # 非工具调用响应 — 打印后结束
    if choice.finish_reason != "tool_calls":
        msg_dict = choice.message.model_dump()
        print_response(msg_dict)
        messages.append({"role": "assistant", "content": msg_dict.get("content") or ""})
        return

    # 工具调用循环
    turn_count = 0
    while choice.finish_reason == "tool_calls":
        turn_count += 1

        # 安全兜底：触达上限时注入收尾提示, TODO: 主Agent不需要限制, 子Agent才需要这种限制, 暂时保留逻辑验证效果
        if turn_count >= MAX_TURNS:
            # console.print(f"[yellow]安全限制：已达到 {MAX_TURNS} 次迭代，正在收尾...[/yellow]")
            messages.append({
                "role": "user",
                "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。",
            })
            break
        msg = choice.message
        msg_dict = msg.model_dump()

        # 如果有推理内容则打印
        reasoning = msg_dict.get("reasoning_content")
        if reasoning:
            console.print(Panel(reasoning, title="思考过程", border_style="dim"))

        # 将助手消息（含工具调用）添加到历史记录
        messages.append(msg_dict)

        # 执行所有工具调用
        tool_results: list[dict[str, str]] = []
        for tool_call in msg.tool_calls:
            args = _parse_tool_args(tool_call.function.arguments)

            # 处理 JSON 解析失败的情况
            if "_parse_error" in args:
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": args["_parse_error"],
                })
                console.print(f"[red]Parse error: {args['_parse_error']}[/red]")
                continue

            command = args["command"]
            console.print(f"\n[yellow]$ {command}[/yellow]")
            output = execute_tool("bash", args)
            print(output)

            tool_results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

        messages.extend(tool_results)

        # 携带工具结果再次调用 LLM
        response = _call_llm(client, messages, tools=TOOLS)
        choice = response.choices[0]

    # 最终响应 — 以流式方式输出
    stream = _call_llm(client, messages, stream=True)

    collected_content = ""
    live_text = Text()

    with Live(live_text, console=console, refresh_per_second=15):
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                collected_content += delta.content
                live_text.append(delta.content)

    messages.append({"role": "assistant", "content": collected_content})