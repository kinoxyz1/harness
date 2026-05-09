"""Skill 使用间隔提醒策略 — 长时间未激活 skill 时注入温和提醒。

你在数据流中的位置：
    QueryLoop.run()
      → policy_runner.before_model_call()
        → SkillUsageNudgePolicy.before_model_call()   ← 你在这里
      → store.extend(messages)

类似 Claude Code 的 todo_reminder / task_reminder 机制。

skill 使用检测基于 session_state.invoked_skills 的快照对比，
覆盖两条激活路径：
  - 模型调用 skill 工具（经过 tool batch）
  - 用户执行 /skills use <id>（不经过 tool batch，直接写入 invoked_skills）
"""
from __future__ import annotations


class SkillUsageNudgePolicy:
    STALE_QUERIES = 6

    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        catalog = session_state.skill_catalog
        if not catalog:
            return []

        current_keys = set(session_state.invoked_skills.keys())
        last_keys = session_state.last_known_skill_keys

        # 快照对比：如果 invoked_keys 增长了，说明有新 skill 被激活
        if current_keys != last_keys:
            session_state.queries_since_skill_activation = 0
            session_state.last_known_skill_keys = current_keys
        else:
            session_state.queries_since_skill_activation += 1

        if session_state.queries_since_skill_activation < self.STALE_QUERIES:
            return []

        # 统计未激活的 skill 数量
        uninvited = [sid for sid in catalog if sid not in current_keys]
        if not uninvited:
            return []

        content = (
            "<system-reminder type=\"skill_nudge\">\n"
            f"当前会话有 {len(catalog)} 个可用 skill，"
            f"但最近 {session_state.queries_since_skill_activation} 次查询未激活新 skill。\n"
            "如果当前任务涉及数据分析、报告生成、调试、TDD 等场景，"
            "考虑先加载对应 skill。\n"
            "</system-reminder>"
        )
        return [{"role": "user", "content": content}]

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None
