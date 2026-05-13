"""LLM 调用层：封装与 Anthropic messages API 的所有交互。

职责：
- LLMResponse：对 API response 的结构化封装（协议无关的内部对象）
- AnthropicClient：管理 client 生命周期，封装调用逻辑
- _parse_response：Anthropic block → 内部 LLMResponse 的归一化
"""
from __future__ import annotations

from collections.abc import Iterator
import sys
import threading
import time
from typing import Any
from uuid import uuid4

from rich.console import Console

from ..shared.config import (
    LLM_RETRY_ATTEMPTS,
    LLM_RETRY_BACKOFF_SECONDS,
    MAX_TOKENS,
    MODEL,
    THINKING_BUDGET,
    THINKING_MODE,
)
from .client import ContextWindowExceededError, ModelRequestOptions, RequestCancelledError
from .factory import create_llm_client
from .protocol import normalize_messages
from ..shared.run_options import RunDisplayOptions
from ..shared.stream_events import StreamEvent, make_event

_console = Console()


def _sanitize_surrogates(obj: Any) -> Any:
    """递归清除数据结构中的 UTF-16 代理字符（surrogate）。

    macOS CJK 输入法 + 删除操作可能在 input() 中引入孤立的代理字符，
    导致 Anthropic SDK 的 JSON 序列化 (.encode('utf-8')) 崩溃。
    对有效字符串无开销（try 命中直接返回），仅在有代理字符时才做替换。
    """
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return obj
        except UnicodeEncodeError:
            return obj.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _sanitize_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_surrogates(item) for item in obj]
    return obj


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
        reasoning_signature: str | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning = reasoning
        self.reasoning_signature = reasoning_signature
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
        self._adaptive_supported: bool | None = None  # None = 未检测, True/False = 缓存结果

    def _apply_thinking(self, params: dict[str, Any]) -> None:
        """根据配置和模型能力设置 thinking 参数。

        auto 模式：优先尝试 adaptive（模型自决定思考深度），不支持则 fallback。
        enabled 模式：使用固定 budget_tokens。
        disabled 模式：不设置 thinking。
        """
        if THINKING_MODE == "disabled":
            return
        if THINKING_MODE == "enabled":
            params["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
            return
        # auto: 使用缓存或尝试 adaptive
        if self._adaptive_supported is False:
            params["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
        else:
            params["thinking"] = {"type": "adaptive"}

    def _is_context_window_exceeded(self, err: Exception) -> bool:
        text = str(err).lower()
        return any(
            phrase in text
            for phrase in (
                "prompt is too long",
                "prompt too long",
                "context length",
                "context window",
                "maximum context",
            )
        )

    def _raise_context_window_exceeded_if_needed(self, err: Exception) -> None:
        if self._is_context_window_exceeded(err):
            raise ContextWindowExceededError(str(err))

    def _is_transient_gateway_error(self, err: Exception) -> bool:
        text = str(err).lower()
        return any(
            phrase in text
            for phrase in (
                "504",
                "502",
                "503",
                "gateway time-out",
                "gateway timeout",
                "bad gateway",
                "service unavailable",
            )
        )

    def _reset_client(self) -> None:
        try:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        self._client = create_llm_client()

    def call(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        display: RunDisplayOptions | None = None,
        request_options: ModelRequestOptions | None = None,
    ) -> LLMResponse:
        """执行一次 Anthropic API 调用。

        system 合并策略：将外部传入的 system 参数与 normalize_messages 从 messages
        中提取的系统文本合并为 full_system，确保不会不会丢失任何系统级指令。

        Args:
            messages: 对话消息列表。
            system: 外部传入的系统提示（来自 ModelInputView.system）。
            tools: 可用工具 schema 列表。
            stream: 是否流式输出（使用 stream() 方法代替）。
            display: 显示选项，控制是否打印计时和 token 统计。

        Returns:
            LLMResponse 包含 content、tool_calls、reasoning、token 统计等。

        Raises:
            NotImplementedError: stream=True 时。
        """
        if stream:
            raise NotImplementedError("Streaming is not supported in this migration")

        display = display or RunDisplayOptions()
        request_options = request_options or ModelRequestOptions()

        normalized_system, api_messages = normalize_messages(messages)
        full_system = "\n\n".join(part for part in [system, normalized_system] if part)

        # Defense in depth: 清除所有消息中的代理字符，防止 JSON 序列化崩溃
        api_messages = _sanitize_surrogates(api_messages)
        full_system = _sanitize_surrogates(full_system)

        params: dict[str, Any] = {
            "model": MODEL,
            "system": full_system,
            "messages": api_messages,
            "max_tokens": (
                request_options.max_output_tokens
                if request_options.max_output_tokens is not None
                else MAX_TOKENS
            ),
        }
        if tools:
            params["tools"] = tools
        if request_options.thinking_mode != "disabled":
            self._apply_thinking(params)
        adaptive_probe_attempted = params.get("thinking", {}).get("type") == "adaptive"

        start = time.time()
        attempts = max(2 if adaptive_probe_attempted else 1, LLM_RETRY_ATTEMPTS)

        for attempt in range(1, attempts + 1):
            result: dict[str, Any] = {}
            error: dict[str, Any] = {}

            def do_call() -> None:
                try:
                    result["data"] = self._client.messages.create(**params)
                except Exception as e:
                    error["data"] = e

            thread = threading.Thread(target=do_call, daemon=True)
            thread.start()

            while thread.is_alive():
                elapsed = int(time.time() - start)
                if request_options.cancel_check and request_options.cancel_check():
                    if not display.quiet:
                        sys.stdout.write("\r\033[K")
                        sys.stdout.flush()
                    self._reset_client()
                    raise RequestCancelledError("request cancelled by user")
                if not display.quiet:
                    sys.stdout.write(f"\r\033[K\033[32m正在思考... {elapsed}s\033[0m")
                    sys.stdout.flush()
                thread.join(timeout=1.0)

            if not display.quiet:
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()

            if error.get("data"):
                err = error["data"]
                if isinstance(err, Exception):
                    self._raise_context_window_exceeded_if_needed(err)
                if (
                    self._adaptive_supported is None
                    and isinstance(err, Exception)
                    and "adaptive" in str(err).lower()
                ):
                    self._adaptive_supported = False
                    params["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
                    continue
                if (
                    attempt < attempts
                    and isinstance(err, Exception)
                    and self._is_transient_gateway_error(err)
                ):
                    time.sleep(LLM_RETRY_BACKOFF_SECONDS * attempt)
                    self._reset_client()
                    continue
                raise err

            response = result["data"]
            break
        else:
            raise RuntimeError("LLM call exhausted retry loop without response")

        if self._adaptive_supported is None and adaptive_probe_attempted:
            self._adaptive_supported = True

        elapsed = time.time() - start

        llm_resp = _parse_response(response)
        llm_resp._raw = response

        if not display.quiet:
            _console.print(
                f"[dim]{elapsed:.1f}s │ token {llm_resp.prompt_tokens}↓ {llm_resp.completion_tokens}↑"
                f" │ finish={llm_resp.finish_reason}[/dim]"
            )

        return llm_resp

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        request_options: ModelRequestOptions | None = None,
        turn_id: str | None = None,
    ) -> Iterator[StreamEvent]:
        request_options = request_options or ModelRequestOptions()
        turn_id = turn_id or uuid4().hex

        normalized_system, api_messages = normalize_messages(messages)
        full_system = "\n\n".join(part for part in [system, normalized_system] if part)
        api_messages = _sanitize_surrogates(api_messages)
        full_system = _sanitize_surrogates(full_system)

        params = {
            "model": MODEL,
            "system": full_system,
            "messages": api_messages,
            "max_tokens": request_options.max_output_tokens or MAX_TOKENS,
        }
        if tools:
            params["tools"] = tools
        if request_options.thinking_mode != "disabled":
            self._apply_thinking(params)

        sequence = 0

        def emit(type: str, payload: dict[str, Any] | None = None):
            nonlocal sequence
            sequence += 1
            return make_event(type, turn_id, sequence, "model", "live", payload)

        try:
            with self._client.messages.stream(**params) as stream:
                input_tokens = 0
                output_tokens = 0
                finish_reason = "end_turn"
                reasoning_signature = ""
                yield emit("response_start")
                for sdk_event in stream:
                    if request_options.cancel_check and request_options.cancel_check():
                        self._reset_client()
                        raise RequestCancelledError("request cancelled by user")
                    if sdk_event.type == "message_start":
                        input_tokens = int(getattr(sdk_event.message.usage, "input_tokens", 0))
                    elif sdk_event.type == "content_block_delta":
                        delta = sdk_event.delta
                        if getattr(delta, "type", "") == "thinking_delta":
                            yield emit("thinking_delta", {"text": getattr(delta, "thinking", "")})
                        elif getattr(delta, "type", "") == "text_delta":
                            yield emit("content_delta", {"text": getattr(delta, "text", "")})
                    elif sdk_event.type == "message_delta":
                        finish_reason = getattr(sdk_event.delta, "stop_reason", finish_reason) or finish_reason
                        output_tokens = int(getattr(sdk_event.usage, "output_tokens", output_tokens))
                    elif sdk_event.type == "message_stop":
                        yield emit(
                            "response_completed",
                            {
                                "finish_reason": finish_reason,
                                "prompt_tokens": input_tokens,
                                "completion_tokens": output_tokens,
                                "reasoning_signature": reasoning_signature,
                            },
                        )
        except RequestCancelledError:
            raise
        except Exception as err:
            self._raise_context_window_exceeded_if_needed(err)
            raise


def _parse_response(response: Any) -> LLMResponse:
    """将 Anthropic API response 归一化为内部 LLMResponse。"""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning: str | None = None
    reasoning_signature: str | None = None

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
            reasoning_signature = getattr(block, "signature", None)

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
        reasoning_signature=reasoning_signature,
    )
