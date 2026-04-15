from __future__ import annotations

from typing import Any

from .response import ModelResponse


class ModelGateway:
    """执行单次模型调用，屏蔽底层 API 差异。"""

    def __init__(self, client: Any | None = None):
        self._client = client

    def call_once(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> ModelResponse:
        if self._client is None:
            raise RuntimeError("No LLM client configured")

        response = self._client.call(messages, tools=tools)
        return ModelResponse(
            content=response.content or "",
            tool_calls=list(response.tool_calls or []),
            finish_reason=response.finish_reason,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )
