"""LLM 调用层：封装与 OpenAI 兼容 API 的所有交互。

职责：
- LLMResponse：对 API response 的结构化封装
- OpenAIClient：管理 client 生命周期，封装调用逻辑
- _parse_tool_args：工具调用参数解析
"""
from __future__ import annotations

import json
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


class OpenAIClient:
    """封装 OpenAI 兼容 API 的调用逻辑。

    管理底层 client 实例，提供 call() 方法进行 LLM 调用，
    返回结构化的 LLMResponse。
    """

    def __init__(self) -> None:
        self._client = create_llm_client()

    def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        display: RunDisplayOptions | None = None,
    ) -> LLMResponse:
        """调用 LLM API，返回结构化的 LLMResponse。

        显示动态计时，完成后打印耗时和 token 用量。
        所有调用者拿到统一的 LLMResponse，不需要关心底层细节。
        """
        display = display or RunDisplayOptions()
        params: dict[str, Any] = {
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
            raw = self._client.chat.completions.create(**params)
            return raw  # type: ignore[return-value]

        # 非流式调用：API 放后台线程，主线程负责刷新计时显示
        result: dict[str, Any] = {}
        error: dict[str, Any] = {}

        def do_call() -> None:
            try:
                result["data"] = self._client.chat.completions.create(**params)
            except Exception as e:
                error["data"] = e

        start = time.time()
        thread = threading.Thread(target=do_call)
        thread.start()

        # 主线程：每秒刷新计时
        while thread.is_alive():
            elapsed = int(time.time() - start)
            if not display.quiet:
                sys.stdout.write(f"\r\033[K\033[32m正在思考... {elapsed}s\033[0m")
                sys.stdout.flush()
            thread.join(timeout=1.0)

        # 清除计时行
        if not display.quiet:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        if error.get("data"):
            raise error["data"]

        response = result["data"]
        elapsed = time.time() - start

        llm_resp = LLMResponse(response)

        if not display.quiet:
            _console.print(
                f"[dim]{elapsed:.1f}s │ token {llm_resp.prompt_tokens}↓ {llm_resp.completion_tokens}↑"
                f" │ finish={llm_resp.finish_reason}[/dim]"
            )

        return llm_resp


def _parse_tool_args(raw: str | None) -> dict[str, Any]:
    """解析工具调用参数，解析失败时返回包含错误信息的字典。"""
    if raw is None:
        return {"_parse_error": "Arguments is None"}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return {"_parse_error": f"Invalid JSON: {e}"}
