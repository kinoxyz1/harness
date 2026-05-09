"""Skill 相关性策略 — 每轮检查当前任务是否匹配未激活的 skill，注入 transcript 尾部提醒。

你在数据流中的位置：
    QueryLoop.run()
      → policy_runner.before_model_call()
        → SkillRelevancePolicy.before_model_call()   ← 你在这里
      → store.extend(messages)                        ← 注入到 conversation_messages 尾部

对抗行为锚定的核心机制：
    在 transcript 尾部（最新位置）注入与 transcript 模式相反的信号，
    提醒模型"有匹配的 skill 可以用"，打破 in-context learning bias。

注入通道：PolicyRunner → store.extend() → conversation_messages
不通过 build_query_overlay_blocks（那个拼进 system prompt，达不到尾部强化目标）。
"""
from __future__ import annotations

from typing import Any


class SkillRelevancePolicy:
    """每轮分析当前任务上下文，如果匹配某个 skill 的 when_to_use，
    注入 <system-reminder> 到 transcript 尾部。

    与 TodoPlanningPolicy 使用完全相同的注入通道：
    before_model_call() → [user message] → store.extend()。
    """

    RELEVANCE_BUDGET_CHARS = 1500
    COOLDOWN_QUERIES = 3
    CONTEXT_WINDOW_MESSAGES = 6  # 从最近 N 条消息中提取文本做匹配

    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        catalog = session_state.skill_catalog
        if not catalog:
            return []

        invoked = session_state.invoked_skills
        cooldown = session_state.skill_relevance_cooldown
        current_stale = session_state.queries_since_skill_activation

        # 从最近消息中提取文本作为匹配上下文
        context_text = self._extract_recent_context(session_state.conversation_messages)
        if not context_text:
            return []

        matched: list[tuple[str, str]] = []  # (skill_id, description)

        for skill_id, meta in sorted(catalog.items()):
            if skill_id in invoked:
                continue  # 已激活的不需要提醒

            # 冷却检查
            last_reminded = cooldown.get(skill_id, -999)
            if current_stale - last_reminded < self.COOLDOWN_QUERIES:
                continue

            # 关键词匹配：when_to_use 或 description 中的关键词出现在上下文中
            if self._matches(context_text, meta):
                matched.append((skill_id, meta.description))

        if not matched:
            return []

        # 更新冷却
        for skill_id, _ in matched:
            cooldown[skill_id] = current_stale

        # 构造注入消息（budget 截断）
        lines: list[str] = []
        budget = self.RELEVANCE_BUDGET_CHARS
        for skill_id, desc in matched:
            line = f"- {skill_id}: {desc}"
            if len("\n".join(lines)) + len(line) > budget:
                break
            lines.append(line)

        content = (
            "<system-reminder type=\"skill_relevance\">\n"
            "以下 skill 与当前任务相关但尚未激活。"
            "如果匹配你的工作，建议先调用 skill 工具加载：\n\n"
            + "\n".join(lines)
            + "\n</system-reminder>"
        )
        return [{"role": "user", "content": content}]

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None

    # ── 内部方法 ──────────────────────────────────────────────

    def _extract_recent_context(self, messages: list[dict[str, Any]]) -> str:
        """从最近 N 条消息中提取纯文本作为匹配上下文。"""
        recent = messages[-self.CONTEXT_WINDOW_MESSAGES:] if messages else []
        parts: list[str] = []
        for msg in recent:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str) and role in ("user", "assistant"):
                parts.append(content)
        return " ".join(parts).lower()

    def _matches(self, context_text: str, meta: Any) -> bool:
        """关键词匹配：when_to_use 或 description 中的关键片段出现在上下文中。

        匹配策略：
        1. 优先使用 when_to_use（如果存在），取其中的关键词
        2. 回退到 description
        3. 对中文文本做子串匹配，对英文做单词匹配
        """
        # when_to_use 中的逗号/分号分隔短语
        source = meta.when_to_use or meta.description or ""
        if not source:
            return False

        # 将 when_to_use 拆分为独立短语/关键词
        keywords = [kw.strip().lower() for kw in source.replace("，", ",").replace("；", ",").split(",") if kw.strip()]

        # 至少一个关键词出现在上下文中
        for kw in keywords:
            if len(kw) >= 2 and kw in context_text:
                return True
        return False
