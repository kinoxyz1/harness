"""消息视图构建器 — 最终模型输入装配器。

你在数据流中的位置：
    QueryLoop.run()
      → ContextGovernor.assess()          预算治理、blocking gate、产出 PreparedQueryContext
      → view_builder.build()              ← 你在这里
        → 拼接 stable_system + runtime_blocks    组装最终 system prompt
        → _strip_old_thinking()                  清理旧的 reasoning_signature
        → 过滤 stable_tools                      根据 allowed_tools_override 限制工具
      → ModelInputView(system=..., messages=..., tools=...)
      → model_gateway.call_once(view)            发送给 API

核心设计：view_builder 不再决定上下文取舍
    本类只负责接收 ContextGovernor 产出的 PreparedQueryContext，
    做最轻量的规范化（签名清理、工具过滤）后输出 ModelInputView。
    不再做 transcript slice 选择，不做预算控制。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.query.state import RunState

from .query_context import PreparedQueryContext


@dataclass(slots=True)
class ModelInputView:
    """模型输入视图：一次模型调用所需的完整输入数据。

    Attributes:
        system: 系统提示词，由 stable_system + runtime_blocks 拼接而成。
        messages: 发送给模型的对话消息列表（working transcript）。
        tools: 可用工具的 JSON schema 列表。None 表示不传 tools。
        internal_runtime_view: 调试用的内部状态快照，不会发送给模型。
    """
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    internal_runtime_view: dict[str, Any] = field(default_factory=dict)


class MessageViewBuilder:
    """消息视图构建器：将 PreparedQueryContext 转换为 ModelInputView。

    职责：
    1. 从 PreparedQueryContext 中提取 stable_system + runtime_blocks 拼接 system
    2. 清理 working_transcript 中的 reasoning_signature
    3. 根据 run_state 的 allowed_tools_override 过滤工具列表
    """

    def __init__(self, tools: list[dict[str, Any]] | None = None):
        self._tools = tools

    def _strip_old_thinking(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """剥离所有 assistant 消息的 reasoning_signature。

        返回新列表（不修改原始 working_transcript）。

        reasoning_signature 是 API 返回的加密签名，体积通常是 thinking 内容的数十倍。
        传回 API 没有实际价值：非 Anthropic 提供商不识别，Anthropic 也不依赖它做推理。
        """
        cleaned: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("reasoning_signature"):
                stripped = {k: v for k, v in msg.items() if k != "reasoning_signature"}
                cleaned.append(stripped)
                continue
            cleaned.append(msg)
        return cleaned

    def build(
        self,
        prepared: PreparedQueryContext,
        *,
        run_state: RunState,
    ) -> ModelInputView:
        """从 PreparedQueryContext 构建 ModelInputView。

        组装流程：
        1. 清理 working_transcript 中的 reasoning_signature
        2. 拼接 system prompt：stable_system + runtime_blocks
        3. 过滤 stable_tools
        4. 输出 ModelInputView
        """
        transcript = self._strip_old_thinking(prepared.working_transcript)
        system_parts = [prepared.stable_system] + [
            block.content for block in prepared.runtime_blocks if block.content
        ]
        tools = prepared.stable_tools
        if run_state.allowed_tools_override is not None and tools is not None:
            tools = [tool for tool in tools if tool.get("name") in run_state.allowed_tools_override]
        return ModelInputView(
            system="\n\n".join(part for part in system_parts if part),
            messages=transcript,
            tools=tools,
            internal_runtime_view={
                "runtime_blocks": [block.kind for block in prepared.runtime_blocks],
                "working_transcript": list(transcript),
                "budget": dict(prepared.budget),
            },
        )
