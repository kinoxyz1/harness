from __future__ import annotations

import importlib
import pathlib
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolResult:
    """工具执行的统一返回格式。"""

    output: str
    success: bool
    error: str | None = None


@dataclass
class ToolContext:
    """工具执行的上下文信息。"""

    working_dir: str = ""
    session_id: str = ""


class ToolRegistry:
    """工具注册表：自动发现、查询、路由。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}
        self._schemas: list[dict[str, Any]] = []
        self._readonly: dict[str, bool] = {}
        self._required_params: dict[str, list[str]] = {}

    def register(self, module: Any) -> None:
        """从一个工具模块中注册。模块需包含 SCHEMA 和 handle。"""
        name: str = module.SCHEMA["function"]["name"]
        self._handlers[name] = module.handle
        self._schemas.append(module.SCHEMA)
        self._readonly[name] = getattr(module, "READONLY", False)
        # 提取必填参数列表
        required = module.SCHEMA.get("function", {}).get("parameters", {}).get("required", [])
        self._required_params[name] = required

    def schemas(self) -> list[dict[str, Any]]:
        """返回所有工具 schema（传给 API 调用）。"""
        return self._schemas

    def has(self, name: str) -> bool:
        return name in self._handlers

    def execute(self, name: str, args: dict[str, Any], context: ToolContext) -> ToolResult:
        """查表 → 验证参数 → 执行 → 返回 ToolResult。"""
        handler = self._handlers.get(name)
        if not handler:
            return ToolResult(output=f"Unknown tool '{name}'", success=False, error="not_found")

        # 验证必填参数
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
    """扫描 core/tools/ 目录，自动注册所有工具模块。"""
    reg = ToolRegistry()
    tools_dir = pathlib.Path(__file__).parent
    for file in tools_dir.glob("*.py"):
        if file.name.startswith("_"):
            continue
        module = importlib.import_module(f"core.tools.{file.stem}")
        reg.register(module)
    return reg


# 模块加载时自动发现并注册所有工具
registry = auto_discover()
