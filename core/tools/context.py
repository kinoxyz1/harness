"""工具上下文与运行时协议类型。

你在数据流中的位置：
    ToolExecutorRuntime.execute_batch()
      → 对每个 tool_call 调用 handle(args, context)
      → 工具返回 ToolInvocationOutcome
      → runtime / query reducer 应用 SessionUpdate 和 RunUpdate

当前原则：
    - ToolUseContext 主要提供只读运行时句柄
    - 工具不应直接改写 SessionState / RunState
    - 文件认知、todo、skill、运行时覆盖等都应通过结构化 updates 回写
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SessionUpdateKind(str, Enum):
    INVOKE_SKILL = "invoke_skill"
    SET_TODO_ITEMS = "set_todo_items"
    UPSERT_FILE_STATE = "upsert_file_state"
    INVALIDATE_FILE_STATE = "invalidate_file_state"
    APPEND_SKILL_EVENT = "append_skill_event"


class RunUpdateKind(str, Enum):
    MARK_FILE_MODIFIED = "mark_file_modified"
    NARROW_ALLOWED_TOOLS = "narrow_allowed_tools"
    SET_MODEL_OVERRIDE = "set_model_override"
    SET_EFFORT_OVERRIDE = "set_effort_override"
    RESET_TODO_TURN_COUNTER = "reset_todo_turn_counter"


class ToolOutcomeStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"
    NEEDS_USER = "needs_user"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class SessionUpdate:
    kind: SessionUpdateKind
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunUpdate:
    kind: RunUpdateKind
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolInvocationOutcome:
    status: ToolOutcomeStatus = ToolOutcomeStatus.SUCCESS
    session_updates: list[SessionUpdate] = field(default_factory=list)
    run_updates: list[RunUpdate] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def make_tool_message(context: "ToolUseContext", content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": context.tool_call_id,
        "content": content,
    }


def safe_path(path: str, working_dir: str) -> Path:
    """解析路径：展开 ~，绝对路径直接使用，相对路径基于工作目录解析。"""
    expanded = Path(path).expanduser()
    return (Path(working_dir).resolve() / expanded).resolve()


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
    total_lines: int | None = None

    @property
    def is_full_read(self) -> bool:
        return self.offset is None and self.limit is None


class ToolUseContext:
    """工具执行上下文。

    绑定到运行时后，工具通过它读取工作目录、当前调用身份、session_state、
    skill_registry，以及已知文件状态。状态回写应通过 ToolInvocationOutcome
    中的 updates 完成，而不是直接修改 context 持有的对象。
    """
    def __init__(self, *, working_dir: str, max_turns: int):
        self._working_dir = working_dir
        self._max_turns = max_turns
        self._tool_name: str = ""
        self._tool_call_id: str = ""
        self._turn_count: int = 0
        self._file_state: dict[str, FileState] = {}
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
        """获取文件缓存；若磁盘内容已过期，仅返回 None，不直接改写 session state。"""
        source = self._file_state
        if (
            self._session_state is not None
            and hasattr(self._session_state, "read_file_state")
            and isinstance(self._session_state.read_file_state, dict)
        ):
            source = self._session_state.read_file_state

        state = source.get(path)
        if state is None:
            return None
        try:
            if os.path.getmtime(path) != state.timestamp:
                return None
        except OSError:
            return None
        return state

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
        """绑定运行时句柄；工具通过显式 updates 回写 session/run state。"""
        if session_state is not None:
            self._session_state = session_state
        if skill_registry is not None:
            self._skill_registry = skill_registry
