from __future__ import annotations

from typing import Any

from .response import ModelResponse


class ModelGateway:
    """执行单次模型调用，屏蔽底层 API 差异。

    作为 QueryLoop 和底层 LLM 客户端之间的中间层，
    负责将内部响应格式统一为 ModelResponse。
    """

    def __init__(self, client: Any | None = None):
        """
        Args:
            client: 底层 LLM 客户端（如 AnthropicClient），需实现 call() 方法。
                    None 时调用 call_once 会抛出 RuntimeError。
        """
        self._client = client

    def call_once(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None,
    ) -> ModelResponse:
        """执行一次模型调用。

        system 和 messages 分通道传递到底层客户端，不再把系统提示塞入 messages。

        Args:
            messages: 对话消息列表（transcript slice），不含系统提示。
            system: 系统提示词，由 PromptAssembler 渲染的 stable + runtime + overlay。
            tools: 可用工具 schema 列表。None 表示不传工具。

        Returns:
            ModelResponse 包含 content、tool_calls、finish_reason、token 统计等。

        Raises:
            RuntimeError: 未配置底层客户端时。
        """
        if self._client is None:
            raise RuntimeError("No LLM client configured")

        response = self._client.call(messages, system=system, tools=tools)
        return ModelResponse(
            content=response.content or "",
            tool_calls=list(response.tool_calls or []),
            finish_reason=response.finish_reason,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            reasoning=response.reasoning or "",
        )
