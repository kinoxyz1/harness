"""Todo 追踪策略 — 确保模型不会忘记刷新计划。

你在数据流中的位置：
    QueryLoop.run()
      → policy_runner.before_model_call()
        → TodoPlanningPolicy.before_model_call()  ← 你在这里
      → 可能注入一条 user 角色的 <system-reminder> 消息

两种触发条件：

1. skill 刚展开（post_skill_replan）：
   模型调用了 skill 工具加载新 skill → barrier 触发 → run_state.todo_replan_required=True
   → 注入"某个 skill 刚刚展开，请刷新 todo"的提醒

2. 连续多轮没写 todo（todo_stale）：
   模型连续 STALE_ASSISTANT_TURNS（默认 4）轮没调用 todo 工具
   → 注入"当前计划可能已过时，请刷新"的提醒 + 当前计划快照

设计考量：这些提醒以 role="user" 注入，因为 API 不支持 system 角色在 messages 中。
这有时会让模型误以为"用户没说话"——是已知的设计权衡。
"""
from __future__ import annotations


class TodoPlanningPolicy:
    STALE_ASSISTANT_TURNS = 4  # 连续多少轮没写 todo 后触发提醒

    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        if run_state.todo_replan_required:
            return [{
                "role": "user",
                "content": (
                    "<system-reminder type=\"post_skill_replan\">"
                    "某个 skill 刚刚展开。若任务是多步骤，请先刷新 todo，并让计划对齐当前 workflow。"
                    "</system-reminder>"
                ),
            }]

        todo_state = session_state.todo_state
        if todo_state.items and run_state.assistant_turns_since_todo >= self.STALE_ASSISTANT_TURNS:
            # 防止同一轮重复提醒
            if todo_state.last_reminder_turn == run_state.turn_count:
                return []
            todo_state.last_reminder_turn = run_state.turn_count
            snapshot = "\n".join(
                f"- [{item.status}] {item.content}" + (f" ({item.workflow_ref})" if item.workflow_ref else "")
                for item in todo_state.items
            )
            return [{
                "role": "user",
                "content": (
                    "<system-reminder type=\"todo_stale\">\n"
                    "当前计划可能已过时，请先刷新 todo。\n"
                    f"{snapshot}\n"
                    "</system-reminder>"
                ),
            }]
        return []

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None
