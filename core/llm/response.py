"""模型响应 — API 返回结果的内部表示。

你在数据流中的位置：
    AnthropicClient.call()
      → _parse_response()                      解析 API 原始响应
      → LLMResponse                            内部格式
    → ModelGateway.call_once()
      → ModelResponse                          ← 你在这里
    → QueryLoop 读取 content / tool_calls / reasoning
    → ModelResponse.to_message()               序列化回 dict，存入 conversation_messages

两个响应类的关系：
    LLMResponse：AnthropicClient 的输出，与 API 细节耦合（包含 _raw 引用）
    ModelResponse：ModelGateway 的输出，是 LLMResponse 的干净副本，被 QueryLoop 使用

to_message() 的关键行为：
    当 PERSIST_THINKING=true 时，reasoning 和 reasoning_signature 会作为
    assistant 消息的顶层字段保存。后续 protocol.py 的 _convert_assistant() 会
    将它们重建为 API 格式的 thinking block，发回给模型。

    这意味着 thinking 文本会随对话历史累积增长，
    view_builder.py 的 _strip_old_thinking() 负责清理旧的 thinking 来控制上下文大小。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..shared.config import MAX_REASONING_CHARS, PERSIST_THINKING


@dataclass(slots=True)
class ModelResponse:
    """模型响应的统一内部表示。"""
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning: str = ""
    reasoning_signature: str = ""

    @property
    def has_final_text(self) -> bool:
        """模型输出了最终的文本回复（非空、非纯工具调用）。"""
        return bool(self.content.strip())

    def to_message(self) -> dict[str, Any]:
        """将响应序列化为内部消息格式，存入 conversation_messages。

        消息格式：
        {
            "role": "assistant",
            "content": "...",
            "tool_calls": [...],        # 如果有工具调用
            "reasoning": "...",          # 如果 PERSIST_THINKING 且有 thinking
            "reasoning_signature": "..." # API 签名，发回 API 时需要
        }

        reasoning 字段会被 protocol.py 重建为 thinking block。
        reasoning_signature 是 API 返回的加密签名，后续发回 API 时必须携带。
        """
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        if PERSIST_THINKING and self.reasoning and self.reasoning.strip():
            truncated = self.reasoning[:MAX_REASONING_CHARS]
            message["reasoning"] = truncated
            if self.reasoning_signature:
                message["reasoning_signature"] = self.reasoning_signature
        return message
