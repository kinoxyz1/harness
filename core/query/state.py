"""运行时状态 — 单次 query 内部的可变状态。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.query.reducers import TransitionReason
    from core.session.state import TodoItem


@dataclass(slots=True)
class RunState:
    turn_count: int = 0                          # 工具调用轮次（每执行一批工具 +1）
    empty_retry_count: int = 0                   # 空响应重试次数
    stop_reason: str | None = None               # 一旦设为 "max_turns"，后续不再传 tools
    last_model_response: Any | None = None       # 最近一次模型响应，供策略判断
    tool_calls_executed: int = 0                 # 累计执行的工具调用数
    files_modified: list[str] = field(default_factory=list)  # 本轮修改的文件列表
    usage_delta: dict[str, int] = field(default_factory=dict)
    transition: "TransitionReason | None" = None

    allowed_tools_override: set[str] | None = None  # 限制后续可用的工具集合
    model_override: str | None = None               # 切换模型
    effort_override: str | None = None               # 调整推理深度（已定义但未消费）

    assistant_turns_since_todo: int = 0              # 连续未写 todo 的轮次

    last_displayed_todo_items: list["TodoItem"] | None = None  # 用于 UI 去重
