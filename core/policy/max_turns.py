"""最大轮次策略 — 防止 Agent 无限循环。

你在数据流中的位置：
    QueryLoop.run()
      → 工具执行完毕后
      → policy_runner.should_stop()
        → MaxTurnsPolicy.should_stop()          ← 你在这里
      → 返回 "max_turns"
      → QueryLoop 注入"你已达到迭代安全上限"
      → 模型被强制给出最终回复（不再传 tools）

为什么需要这个？模型可能陷入循环（反复调用工具不收敛），
max_turns 是安全阀，强制模型在有限步数内给出结果。
"""
from __future__ import annotations


class MaxTurnsPolicy:
    def __init__(self, max_turns: int):
        self._max_turns = max_turns

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        return []

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, context, state) -> str | None:
        """当工具调用轮次达到上限时返回 "max_turns"。"""
        if state.turn_count >= self._max_turns:
            return "max_turns"
        return None
