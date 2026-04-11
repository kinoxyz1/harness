from __future__ import annotations

import importlib
import os
import pathlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


def safe_path(p: str, working_dir: str) -> Path:
    """解析路径并确保不逃逸工作目录。

    将相对路径基于 working_dir 解析为绝对路径，
    然后检查最终路径是否仍在工作目录内。
    """
    base = Path(working_dir).resolve()
    path = (base / p).resolve()
    # if not path.is_relative_to(base):
    #     raise ValueError(f"Path escapes workspace: {p}")
    return path


@dataclass
class ToolResult:
    """工具执行的统一返回格式。"""

    output: str
    success: bool
    error: str | None = None
    truncated: bool = False


@dataclass
class FileState:
    """工具对单个文件的认知状态。参考 Claude Code 的 readFileState。"""

    content: str                        # 文件内容
    timestamp: float                    # os.path.getmtime() 读取时的修改时间
    offset: int | None = None           # 读取偏移（None = 全文）
    limit: int | None = None            # 读取行数限制（None = 全文）

    @property
    def is_full_read(self) -> bool:
        """是否为完整读取（不是局部读取）。"""
        return self.offset is None and self.limit is None


class ToolUseContext:
    """工具执行上下文。

    五层设计，每层有独立的访问规则：
    - 环境层：构造时设置，工具只读
    - 身份层：每次 tool call 更新，工具只读
    - 文件认知层：工具可读写，框架控制一致性
    - 对话层：只读引用
    - 控制层：外部信号，工具只能查询
    """

    def __init__(self, *, working_dir: str, max_turns: int):
        # ── 环境层 ──
        self._working_dir = working_dir
        self._max_turns = max_turns

        # ── 身份层 ──
        self._tool_name: str = ""
        self._tool_call_id: str = ""
        self._turn_count: int = 0

        # ── 文件认知层 ──
        self._file_state: dict[str, FileState] = {}

        # ── 对话层 ──
        self._messages: list[dict[str, Any]] | None = None

        # ── 控制层 ──
        self._cancelled: bool = False

    # ── 环境层 ──

    @property
    def working_dir(self) -> str:
        return self._working_dir

    @property
    def max_turns(self) -> int:
        return self._max_turns

    # ── 身份层 ──

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def tool_call_id(self) -> str:
        return self._tool_call_id

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def _set_call_identity(self, *, name: str, call_id: str, turn: int) -> None:
        """Agent loop 在每次 tool call 前调用。"""
        self._tool_name = name
        self._tool_call_id = call_id
        self._turn_count = turn

    # ── 文件认知层 ──

    def get_file_state(self, path: str) -> FileState | None:
        """查询工具对某个文件的认知。返回 None 表示未认知。"""
        return self._file_state.get(path)

    def set_file_state(self, path: str, state: FileState) -> None:
        """记录对文件的认知（read_file 成功后调用）。"""
        self._file_state[path] = state

    def update_file_state(self, path: str, content: str) -> None:
        """写工具修改文件后更新认知（edit_file / write_file 成功后调用）。"""
        self._file_state[path] = FileState(
            content=content,
            timestamp=os.path.getmtime(path),
        )

    def invalidate_file_state(self, path: str) -> None:
        """使对某个文件的认知失效。"""
        self._file_state.pop(path, None)

    # ── 对话层 ──

    def set_messages(self, messages: list[dict[str, Any]]) -> None:
        """Agent loop 构造时调用，传入只读引用。"""
        self._messages = messages

    @property
    def messages(self) -> list[dict[str, Any]] | None:
        return self._messages

    # ── 控制层 ──

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _cancel(self) -> None:
        """外部调用，请求取消。"""
        self._cancelled = True


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
        # 收集 ANNOTATIONS
        annotations = getattr(module, "ANNOTATIONS", {})
        if annotations:
            self._annotations[name] = annotations
        # 提取必填参数列表
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

    def execute(self, name: str, args: dict[str, Any], context: ToolUseContext) -> ToolResult:
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
