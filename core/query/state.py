"""运行时状态 — 单次 query 内部的可变状态。

你在数据流中的位置：
    QueryLoop.run()
      → state = RunState()                      ← 每次调用都重新创建
      → 贯穿整个 think-act 循环，记录轮次、控制面信号等

与 SessionState 的区别：
    SessionState 跨 query 持久化（对话历史、skill、todo 等）
    RunState 只存在于单次 run() 调用内，run() 结束后丢弃

RunState 是 QueryLoop 和各个组件之间的"共享黑板"：
    - ToolExecutorRuntime 写入 context_patches → QueryLoop 读取并应用
    - MaxTurnsPolicy 读取 turn_count → 决定是否强制终止
    - TodoPlanningPolicy 读取 assistant_turns_since_todo → 决定是否注入提醒
    - MessageViewBuilder 读取 allowed_tools_override → 过滤工具列表
    - AnthropicClient（未来）读取 effort_override → 调整 thinking budget
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

    # ── 控制面信号（由工具通过 ContextPatch 写入）──────────────
    allowed_tools_override: set[str] | None = None  # 限制后续可用的工具集合
    model_override: str | None = None               # 切换模型
    effort_override: str | None = None               # 调整推理深度（已定义但未消费）

    # ── Barrier 信号（由工具通过 ExecutionBarrier 写入）───────
    barrier_reason: str | None = None                # 中断原因（如 "skill_expanded"）

    # ── Todo replan 信号 ─────────────────────────────────────
    todo_replan_required: bool = False               # 是否需要模型重新规划 todo
    todo_replan_reason: str | None = None
    assistant_turns_since_todo: int = 0              # 连续未写 todo 的轮次

    # ── UI 状态 ──────────────────────────────────────────────
    last_displayed_todo_items: list["TodoItem"] | None = None  # 用于 UI 去重
