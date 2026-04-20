"""恢复管理器 — 处理模型的异常响应。

你在数据流中的位置：
    QueryLoop.run()
      → 模型返回空响应（没有 content 也没有 tool_calls）
      → recovery.handle(model_resp, state)      ← 你在这里
      → 返回 RecoveryDecision
      → 如果 should_continue=True，注入追问消息，继续循环
      → 如果 should_continue=False，返回空 QueryResult

处理的两种异常：

1. finish_reason="length"：模型输出被 max_tokens 截断
   → 注入"请继续输出"，让模型接着说

2. 空响应：模型既没有文本也没有工具调用
   → 注入"请直接给出最终答复"，尝试引导模型回复

设计意图：这些是"软恢复"，给模型第二次机会。
如果重试后仍然失败，QueryLoop 会返回 EMPTY_RESPONSE。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class RecoveryDecision:
    should_continue: bool
    follow_up_messages: list[dict[str, str]] = field(default_factory=list)


class RecoveryManager:
    def handle(self, model_resp, state) -> RecoveryDecision:
        # 截断恢复：模型想说但说完了，让它继续
        if model_resp.finish_reason == "length":
            return RecoveryDecision(
                should_continue=True,
                follow_up_messages=[{"role": "user", "content": "请继续输出。"}],
            )
        # 空响应恢复：模型什么都没说，引导它回复
        if not model_resp.has_final_text:
            return RecoveryDecision(
                should_continue=True,
                follow_up_messages=[{"role": "user", "content": "请直接给出最终答复。"}],
            )
        return RecoveryDecision(should_continue=False)
