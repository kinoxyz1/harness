"""策略框架 — 在 QueryLoop 循环中注入控制逻辑。

你在数据流中的位置：
    QueryLoop.run() 的每个循环迭代中：
      ① policy_runner.before_model_call()     ← 调用模型前
         → 注入提醒消息（如"计划过时了"）
      ② ... 模型调用 + 工具执行 ...
      ③ policy_runner.after_tool_batch()       ← 工具执行后
         → 注入后续消息（当前为空，预留扩展点）
      ④ policy_runner.should_stop()            ← 检查是否终止循环
         → 返回 "max_turns" 时注入强制收尾消息

三个钩子的作用：
    before_model_call: 在模型看到消息前注入额外的 user 消息
    after_tool_batch: 工具执行完毕后注入消息（暂未使用）
    should_stop: 返回非 None 值时，循环进入强制收尾模式
"""
from __future__ import annotations

from typing import Protocol


class RunPolicy(Protocol):
    """策略协议 — 所有策略必须实现这三个方法。"""

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        """模型调用前，返回要注入的消息列表。"""
        raise NotImplementedError

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        """工具批次执行后，返回要注入的消息列表。"""
        raise NotImplementedError

    def should_stop(self, context, state) -> str | None:
        """检查是否应该停止循环。返回停止原因或 None（继续）。"""
        raise NotImplementedError


class PolicyRunner:
    """按顺序执行所有注册的策略，收集结果。"""

    def __init__(self, policies: list[RunPolicy]):
        self._policies = policies

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for policy in self._policies:
            messages.extend(policy.before_model_call(context, state))
        return messages

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for policy in self._policies:
            messages.extend(policy.after_tool_batch(context, state, batch_result))
        return messages

    def should_stop(self, context, state) -> str | None:
        """第一个返回非 None 的策略决定停止原因。"""
        for policy in self._policies:
            decision = policy.should_stop(context, state)
            if decision is not None:
                return decision
        return None
