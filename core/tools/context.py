from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ContextPatch:
    allowed_tools: set[str] | None = None
    model_override: str | None = None
    effort_override: str | None = None


@dataclass(slots=True)
class ExecutionBarrier:
    stop_after_tool: bool = True
    reason: str | None = None


def safe_path(path: str, working_dir: str) -> Path:
    """解析路径：展开 ~，绝对路径直接使用，相对路径基于工作目录解析。"""
    expanded = Path(path).expanduser()
    return (Path(working_dir).resolve() / expanded).resolve()


@dataclass
class ToolResult:
    """工具执行的统一返回格式。"""

    output: str
    success: bool
    error: str | None = None
    truncated: bool = False
    injected_messages: list[dict[str, Any]] = field(default_factory=list)
    context_patch: ContextPatch | None = None
    barrier: ExecutionBarrier | None = None


@dataclass
class FileState:
    """工具对单个文件的认知状态。"""

    content: str
    timestamp: float
    offset: int | None = None
    limit: int | None = None

    @property
    def is_full_read(self) -> bool:
        return self.offset is None and self.limit is None


class ToolUseContext:
    """工具执行上下文。"""

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

    def get_file_state(self, path: str) -> FileState | None:
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
        if session_state is not None:
            self._session_state = session_state
            if hasattr(session_state, "read_file_state") and isinstance(session_state.read_file_state, dict):
                self._file_state = session_state.read_file_state
        if skill_registry is not None:
            self._skill_registry = skill_registry
