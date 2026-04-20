"""工具上下文 — 工具执行的运行时环境和通用数据结构。

你在数据流中的位置：
    ToolExecutorRuntime.execute_batch()
      → 对每个 tool_call 调用 handle(args, context)  ← context 就是 ToolUseContext
      → 工具通过 context 读取工作目录、缓存文件状态、标记修改的文件

    另外，ContextPatch 和 ExecutionBarrier 是工具向 QueryLoop 回传信号的机制：
    工具执行结果 ToolResult
      → context_patch: ContextPatch          ← 你在这里
      → barrier: ExecutionBarrier
    → QueryLoop._apply_batch_control_plane() 读取并应用到 RunState

关键数据结构：
    ContextPatch: 工具请求修改运行参数（限制可用工具、切换模型、调整 effort）
    ExecutionBarrier: 工具要求中断当前批次（如 skill 展开后需要重新评估）
    ToolResult: 工具执行的统一返回格式
    FileState: 单个文件的内容缓存（read_file 工具写入）
    ToolUseContext: 工具执行的运行时上下文（绑定到 SessionState）
    safe_path: 路径解析工具函数（展开 ~，确保绝对路径正确）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ContextPatch:
    """工具向 RunState 回传的参数覆盖请求。

    典型场景：skill 工具加载 skill 后，需要限制后续可用的工具集合。
    例如 plan 类型子代理只能用 readonly 工具。

    allowed_tools 取交集策略：多次 patch 的 allowed_tools 会越用越窄，
    但永远不会扩大。
    """
    allowed_tools: set[str] | None = None
    model_override: str | None = None
    effort_override: str | None = None


@dataclass(slots=True)
class ExecutionBarrier:
    """工具要求停止当前批次，让模型重新评估。

    目前只有一种 barrier：skill_expanded
    （skill 工具加载了新 skill，模型需要看到 skill 内容后再决定下一步）
    """
    stop_after_tool: bool = True
    reason: str | None = None


def safe_path(path: str, working_dir: str) -> Path:
    """解析路径：展开 ~，绝对路径直接使用，相对路径基于工作目录解析。"""
    expanded = Path(path).expanduser()
    return (Path(working_dir).resolve() / expanded).resolve()


@dataclass
class ToolResult:
    """工具执行的统一返回格式。

    output: 工具的可读输出（会显示在终端，也会发回给模型）
    success: 是否执行成功
    context_patch: 可选的参数覆盖请求
    barrier: 可选的批次中断信号
    """
    output: str
    success: bool
    error: str | None = None
    truncated: bool = False
    injected_messages: list[dict[str, Any]] = field(default_factory=list)
    context_patch: ContextPatch | None = None
    barrier: ExecutionBarrier | None = None


@dataclass
class FileState:
    """工具对单个文件的认知状态。

    read_file 工具每次读取文件时更新，后续 edit_file/write_file 会校验 mtime。
    PromptAssembler 从 read_file_state 读取这些信息，渲染 <file-runtime>。
    """
    content: str
    timestamp: float
    offset: int | None = None
    limit: int | None = None

    @property
    def is_full_read(self) -> bool:
        return self.offset is None and self.limit is None


class ToolUseContext:
    """工具执行上下文 — 在整个会话中持久化。

    绑定到 SessionState（通过 bind_runtime），工具通过它：
    - 获取工作目录
    - 缓存已读文件的内容（避免重复读取）
    - 记录被修改的文件列表
    """
    def __init__(self, *, working_dir: str, max_turns: int):
        self._working_dir = working_dir
        self._max_turns = max_turns
        self._tool_name: str = ""
        self._tool_call_id: str = ""
        self._turn_count: int = 0
        self._file_state: dict[str, FileState] = {}
        self._files_modified: list[str] = []
        self._messages: list[dict[str, Any]] | None = None
        self._cancelled: bool = False
        self._session_state: Any = None
        self._skill_registry: Any = None

    @property
    def working_dir(self) -> str:
        return self._working_dir

    @property
    def max_turns(self) -> int:
        return self._max_turns

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
        self._tool_name = name
        self._tool_call_id = call_id
        self._turn_count = turn

    # ── 文件状态管理 ──────────────────────────────────────────

    def get_file_state(self, path: str) -> FileState | None:
        """获取文件缓存。如果文件在磁盘上被外部修改，自动失效。"""
        state = self._file_state.get(path)
        if state is None:
            return None
        try:
            if os.path.getmtime(path) != state.timestamp:
                del self._file_state[path]
                return None
        except OSError:
            del self._file_state[path]
            return None
        return state

    def set_file_state(self, path: str, state: FileState) -> None:
        self._file_state[path] = state

    def update_file_state(self, path: str, content: str) -> None:
        """写入文件后更新缓存。"""
        self._file_state[path] = FileState(
            content=content,
            timestamp=os.path.getmtime(path),
        )

    def invalidate_file_state(self, path: str) -> None:
        self._file_state.pop(path, None)

    @property
    def files_modified(self) -> list[str]:
        return list(self._files_modified)

    def mark_file_modified(self, path: str) -> None:
        if path not in self._files_modified:
            self._files_modified.append(path)

    def set_messages(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages

    @property
    def messages(self) -> list[dict[str, Any]] | None:
        return self._messages

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def session_state(self) -> Any:
        return self._session_state

    @property
    def skill_registry(self) -> Any:
        return self._skill_registry

    def _cancel(self) -> None:
        self._cancelled = True

    def bind_runtime(self, *, session_state: Any | None = None, skill_registry: Any | None = None) -> None:
        """绑定到 SessionState，共享文件状态缓存。

        这样工具的 read_file 缓存和 SessionState 的 read_file_state 是同一个 dict，
        PromptAssembler 可以直接读取工具缓存的文件内容。
        """
        if session_state is not None:
            self._session_state = session_state
            if hasattr(session_state, "read_file_state") and isinstance(session_state.read_file_state, dict):
                self._file_state = session_state.read_file_state
        if skill_registry is not None:
            self._skill_registry = skill_registry
