from __future__ import annotations


class TodoPlanningPolicy:
    STALE_ASSISTANT_TURNS = 4

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
