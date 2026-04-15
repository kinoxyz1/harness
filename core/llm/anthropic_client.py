"""LLM 调用层：封装与 Anthropic messages API 的所有交互。

职责：
- LLMResponse：对 API response 的结构化封装（协议无关的内部对象）
- AnthropicClient：管理 client 生命周期，封装调用逻辑
- _parse_response：Anthropic block → 内部 LLMResponse 的归一化
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any

from rich.console import Console

from ..shared.config import ENABLE_THINKING, MAX_TOKENS, MODEL
from .factory import create_llm_client
from .protocol import normalize_messages
from ..shared.run_options import RunDisplayOptions

_console = Console()


class LLMResponse:
    """对 API response 的结构化封装。

    所有调用者通过这个类访问响应，不需要关心底层 SDK 的差异。
    """

    def __init__(
        self,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        finish_reason: str = "end_turn",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        reasoning: str | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning = reasoning
        self.finish_reason = finish_reason
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    @property
    def has_content(self) -> bool:
        return bool(self.content and self.content.strip())

    @property
    def is_tool_call(self) -> bool:
        if self.finish_reason == "tool_use":
            return True
        if self.tool_calls and not self.has_content:
            return True
        return False

    @property
    def is_truncated(self) -> bool:
        return self.finish_reason == "max_tokens"

    @property
    def raw_response(self) -> Any:
        return self._raw if hasattr(self, "_raw") else None


class AnthropicClient:
    """封装 Anthropic messages API 的调用逻辑。"""

    def __init__(self) -> None:
        self._client = create_llm_client()

    def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        display: RunDisplayOptions | None = None,
    ) -> LLMResponse:
        if stream:
            raise NotImplementedError("Streaming is not supported in this migration")

        display = display or RunDisplayOptions()

        system, api_messages = normalize_messages(messages)

        params: dict[str, Any] = {
            "model": MODEL,
            "system": system,
            "messages": api_messages,
            "max_tokens": MAX_TOKENS,
        }
        if tools:
            params["tools"] = tools
        if ENABLE_THINKING:
            params["thinking"] = {"type": "enabled", "budget_tokens": min(MAX_TOKENS, 10000)}

        result: dict[str, Any] = {}
        error: dict[str, Any] = {}

        def do_call() -> None:
            try:
                result["data"] = self._client.messages.create(**params)
            except Exception as e:
                error["data"] = e

        start = time.time()
        thread = threading.Thread(target=do_call)
        thread.start()

        while thread.is_alive():
            elapsed = int(time.time() - start)
            if not display.quiet:
                sys.stdout.write(f"\r\033[K\033[32m正在思考... {elapsed}s\033[0m")
                sys.stdout.flush()
            thread.join(timeout=1.0)

        if not display.quiet:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        if error.get("data"):
            raise error["data"]

        response = result["data"]
        elapsed = time.time() - start

        llm_resp = _parse_response(response)
        llm_resp._raw = response

        if not display.quiet:
            _console.print(
                f"[dim]{elapsed:.1f}s │ token {llm_resp.prompt_tokens}↓ {llm_resp.completion_tokens}↑"
                f" │ finish={llm_resp.finish_reason}[/dim]"
            )

        return llm_resp


def _parse_response(response: Any) -> LLMResponse:
    """将 Anthropic API response 归一化为内部 LLMResponse。"""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning: str | None = None

    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "name": block.name,
                "args": block.input if isinstance(block.input, dict) else {},
            })
        elif block.type == "thinking":
            reasoning = block.thinking

    content = "\n".join(text_parts) if text_parts else None

    prompt_tokens = response.usage.input_tokens if response.usage else 0
    completion_tokens = response.usage.output_tokens if response.usage else 0

    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=response.stop_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning=reasoning,
    )
