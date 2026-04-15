from __future__ import annotations

import importlib
import pathlib
from typing import Any, Callable

from .context import FileState, ToolResult, ToolUseContext, safe_path
from .runtime import ToolCall, ToolExecutorRuntime


__all__ = [
    "FileState",
    "ToolCall",
    "ToolExecutorRuntime",
    "ToolResult",
    "ToolRegistry",
    "ToolUseContext",
    "auto_discover",
    "registry",
    "safe_path",
]


class ToolRegistry:
    """工具注册表：自动发现、查询、路由。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}
        self._schemas: list[dict[str, Any]] = []
        self._readonly: dict[str, bool] = {}
        self._annotations: dict[str, dict[str, bool]] = {}
        self._required_params: dict[str, list[str]] = {}

    def register(self, module: Any) -> None:
        """从一个工具模块中注册。模块需包含 SCHEMA 和 handle。"""
        name: str = module.SCHEMA["function"]["name"]
        self._handlers[name] = module.handle
        self._schemas.append(module.SCHEMA)
        self._readonly[name] = getattr(module, "READONLY", False)
        annotations = getattr(module, "ANNOTATIONS", {})
        if annotations:
            self._annotations[name] = annotations
        required = module.SCHEMA.get("function", {}).get("parameters", {}).get("required", [])
        self._required_params[name] = required

    def schemas(self) -> list[dict[str, Any]]:
        """返回所有工具 schema（传给 API 调用）。"""
        return self._schemas

    def has(self, name: str) -> bool:
        return name in self._handlers

    def is_readonly(self, name: str) -> bool:
        """查询工具是否为只读。优先从 ANNOTATIONS 读取，回退到 READONLY。"""
        ann = self._annotations.get(name)
        if ann and "readonly" in ann:
            return ann["readonly"]
        return self._readonly.get(name, False)

    def annotations(self, name: str) -> dict[str, bool]:
        """查询工具的完整 ANNOTATIONS。"""
        return dict(self._annotations.get(name, {}))

    def filtered(self, allowed_names: set[str]) -> ToolRegistry:
        """返回只包含允许工具的新注册表。"""
        new_reg = ToolRegistry()
        for schema in self._schemas:
            name = schema["function"]["name"]
            if name in allowed_names:
                new_reg._handlers[name] = self._handlers[name]
                new_reg._schemas.append(schema)
                new_reg._readonly[name] = self._readonly.get(name, False)
                if name in self._annotations:
                    new_reg._annotations[name] = self._annotations[name]
                if name in self._required_params:
                    new_reg._required_params[name] = self._required_params[name]
        return new_reg

    def execute(self, name: str, args: dict[str, Any], context: ToolUseContext) -> ToolResult:
        """查表 → 验证参数 → 执行 → 返回 ToolResult。"""
        handler = self._handlers.get(name)
        if not handler:
            return ToolResult(output=f"Unknown tool '{name}'", success=False, error="not_found")

        required = self._required_params.get(name, [])
        missing = [p for p in required if p not in args]
        if missing:
            return ToolResult(
                output=f"Missing required parameters: {', '.join(missing)}",
                success=False,
                error="missing_params",
            )

        return handler(args, context)


def auto_discover() -> ToolRegistry:
    """扫描 core/tools/builtin/ 目录，自动注册所有工具模块。"""
    reg = ToolRegistry()
    tools_dir = pathlib.Path(__file__).parent / "builtin"
    for file in tools_dir.glob("*.py"):
        if file.name.startswith("_"):
            continue
        module = importlib.import_module(f"core.tools.builtin.{file.stem}")
        reg.register(module)
    return reg


# 模块加载时自动发现并注册所有工具
registry = auto_discover()
