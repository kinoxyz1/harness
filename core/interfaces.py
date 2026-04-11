"""Agent 插件接口定义。

三个 Protocol：LLMClient、ContextPlugin、Renderer。
实现模块只需满足方法签名即可，不需要继承这些 Protocol。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    """LLM 调用抽象。"""

    def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        """调用 LLM，返回 LLMResponse。"""
        ...


@runtime_checkable
class ContextPlugin(Protocol):
    """上下文注入插件。每个插件负责向 messages 注入一类上下文。"""

    def inject(self, messages: list[dict[str, Any]]) -> None:
        """将上下文注入到 messages 中。需要自行保证幂等性。"""
        ...


@runtime_checkable
class Renderer(Protocol):
    """显示抽象。所有终端输出都通过此接口。"""

    def show_thinking(self, title: str, reasoning: str) -> None:
        """显示推理/思考过程。"""
        ...

    def show_assistant(self, content: str | None) -> None:
        """显示助手文字内容。"""
        ...

    def show_timing(
        self,
        elapsed: float,
        prompt_tokens: int,
        completion_tokens: int,
        finish_reason: str,
    ) -> None:
        """显示 LLM 调用计时信息。"""
        ...

    def show_current_todo(self, item: Any, completed: int, total: int) -> None:
        """显示当前聚焦的 todo。item 为 TodoItem。"""
        ...

    def show_progress(self, items: list[Any]) -> None:
        """显示完整进度概览。items 为 TodoItem 列表。"""
        ...

    def show_completion_summary(
        self, completed: int, total: int, elapsed: float
    ) -> None:
        """显示任务完成总结面板。"""
        ...

    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        """显示工具调用开始。"""
        ...

    def show_tool_result(self, name: str, output: str) -> None:
        """显示工具执行结果。"""
        ...

    def show_error(self, message: str) -> None:
        """显示错误信息。"""
        ...

    def show_status(self, message: str) -> None:
        """显示状态信息（灰色 dim）。"""
        ...


