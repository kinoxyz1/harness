"""验证 Protocol 定义可以被正确实现。"""
from __future__ import annotations
from typing import Any
from core.interfaces import LLMClient, ContextPlugin, Renderer


class TestRendererProtocol:
    def test_minimal_renderer(self):
        class MinimalRenderer:
            def show_thinking(self, title: str, reasoning: str) -> None: pass
            def show_assistant(self, content: str | None) -> None: pass
            def show_timing(self, elapsed: float, prompt_tokens: int, completion_tokens: int, finish_reason: str) -> None: pass
            def show_current_todo(self, item: Any, completed: int, total: int) -> None: pass
            def show_progress(self, items: list[Any]) -> None: pass
            def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None: pass
            def show_tool_call(self, name: str, args: dict[str, Any]) -> None: pass
            def show_tool_result(self, name: str, output: str) -> None: pass
            def show_error(self, message: str) -> None: pass
            def show_status(self, message: str) -> None: pass
        renderer = MinimalRenderer()
        assert isinstance(renderer, Renderer)

    def test_context_plugin(self):
        class TestPlugin:
            def inject(self, messages: list[dict[str, Any]]) -> None: pass
        plugin = TestPlugin()
        assert isinstance(plugin, ContextPlugin)

    def test_llm_client(self):
        class TestClient:
            def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Any: pass
        client = TestClient()
        assert isinstance(client, LLMClient)

