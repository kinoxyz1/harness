"""Skill 相关性策略 — 每轮检查当前任务是否匹配未激活的 skill，注入 transcript 尾部提醒。

你在数据流中的位置：
    QueryLoop.run()
      → policy_runner.before_model_call()
        → SkillRelevancePolicy.before_model_call()   ← 你在这里
      → store.extend(messages)                        ← 注入到 conversation_messages 尾部

匹配策略（通过 SKILL_LLM_MATCH 配置切换）：
    false（默认）: 精确子串关键词匹配（_match_by_keywords）
    true:          LLM 分类调用（_classify_via_llm），支持语义匹配

注入通道：PolicyRunner → store.extend() → conversation_messages
不通过 build_query_overlay_blocks（那个拼进 system prompt，达不到尾部强化目标）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from core.llm.client import ModelGateway, ModelRequestOptions
from core.shared.config import SKILL_LLM_MATCH

logger = logging.getLogger(__name__)

_CLASSIFICATION_SYSTEM_PROMPT = (
    "你是 skill 匹配器。根据用户意图，从可用 skill 列表中选出匹配的。\n"
    "只允许依据当前这一条用户请求判断，不要被更早的对话、assistant 解释、架构讨论带偏。\n"
    "如果用户只是在讨论 skill、agent、workflow、subagent、能力边界，不等于要激活对应 meta-skill。\n"
    "对于 skill-creator，只有用户明确要求创建、编写、更新 skill 或 SKILL.md 时才允许返回。\n"
    "不确定时返回 NONE。\n"
    "只返回匹配的 skill ID，逗号分隔。无匹配则返回 NONE。\n"
    "不要解释，不要输出其他内容。"
)


class SkillRelevancePolicy:
    """每轮分析当前任务上下文，如果匹配某个 skill 的 when_to_use，
    注入 <system-reminder> 到 transcript 尾部。

    匹配策略通过 SKILL_LLM_MATCH 环境变量配置：
    - false（默认）: 精确子串关键词匹配
    - true: LLM 分类调用，支持语义匹配

    与 TodoPlanningPolicy 使用完全相同的注入通道：
    before_model_call() → [user message] → store.extend()。
    """

    RELEVANCE_BUDGET_CHARS = 1500
    COOLDOWN_QUERIES = 3
    CONTEXT_WINDOW_MESSAGES = 6
    META_SKILL_PATTERNS = {
        "skill-creator": (
            re.compile(r"(创建|新建|编写|写好|生成|制作|更新|修改).{0,24}(skill|技能)", re.IGNORECASE),
            re.compile(r"skill\.md", re.IGNORECASE),
            re.compile(r"\b(create|build|write|update|edit)\b.{0,24}\bskill\b", re.IGNORECASE),
        ),
    }

    def __init__(self, model_gateway: ModelGateway | None = None):
        self._model_gateway = model_gateway
        self._use_llm = SKILL_LLM_MATCH and model_gateway is not None

    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        catalog = session_state.skill_catalog
        if not catalog:
            return []

        invoked = session_state.invoked_skills
        cooldown = session_state.skill_relevance_cooldown
        current_stale = session_state.queries_since_skill_activation

        current_user_text = self._extract_recent_context(session_state.conversation_messages)
        if not current_user_text:
            return []

        # 收集未冷却、未激活的候选 skill
        candidates: dict[str, Any] = {}
        for skill_id, meta in sorted(catalog.items()):
            if skill_id in invoked:
                continue
            last_reminded = cooldown.get(skill_id, -999)
            if current_stale - last_reminded < self.COOLDOWN_QUERIES:
                continue
            candidates[skill_id] = meta

        if not candidates:
            return []

        # 根据配置选择匹配策略
        matched_ids = self._match(current_user_text, candidates)
        matched_ids = self._post_validate_matches(current_user_text, matched_ids)

        if not matched_ids:
            return []

        # 从 catalog 中获取匹配 skill 的描述
        matched: list[tuple[str, str]] = []
        for skill_id in matched_ids:
            if skill_id in catalog:
                matched.append((skill_id, catalog[skill_id].description))

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
            "以下 skill 与当前任务高度相关但尚未激活。\n"
            "可优先考虑调用 skill 工具加载匹配的 skill，再根据展开后的指令决定下一步。\n\n"
            + "\n".join(lines)
            + "\n</system-reminder>"
        )
        return [{"role": "user", "content": content}]

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None

    # ── 匹配分派 ──────────────────────────────────────────────

    def _match(self, context_text: str, candidates: dict[str, Any]) -> list[str]:
        """根据配置分派到关键词匹配或 LLM 分类。"""
        if self._use_llm:
            return self._classify_via_llm(context_text, candidates)
        return self._match_by_keywords(context_text, candidates)

    # ── 关键词匹配（默认，现有逻辑）──────────────────────────

    def _match_by_keywords(self, context_text: str, candidates: dict[str, Any]) -> list[str]:
        """精确子串关键词匹配。保留原有行为。"""
        matched: list[str] = []
        for skill_id, meta in candidates.items():
            source = meta.when_to_use or meta.description or ""
            if not source:
                continue
            keywords = [
                kw.strip().lower()
                for kw in source.replace("，", ",").replace("；", ",").split(",")
                if kw.strip()
            ]
            for kw in keywords:
                if len(kw) >= 2 and kw in context_text:
                    matched.append(skill_id)
                    break
        return matched

    # ── LLM 分类匹配（SKILL_LLM_MATCH=true 时启用）────────────

    def _classify_via_llm(self, context_text: str, candidates: dict[str, Any]) -> list[str]:
        """通过轻量 LLM 调用判断 skill 相关性。"""
        if self._model_gateway is None:
            return []

        skill_summary = self._format_skill_summary(candidates)
        user_prompt = (
            f"可用 skill:\n{skill_summary}\n\n"
            f"当前用户请求:\n{context_text}"
        )

        try:
            resp = self._model_gateway.call_once(
                messages=[{"role": "user", "content": user_prompt}],
                system=_CLASSIFICATION_SYSTEM_PROMPT,
                tools=None,
                request_options=ModelRequestOptions(
                    thinking_mode="disabled",
                    max_output_tokens=50,
                ),
            )
        except Exception:
            logger.debug("Skill relevance LLM call failed, skipping", exc_info=True)
            return []

        return self._parse_classification(resp.content, set(candidates.keys()))

    def _format_skill_summary(self, candidates: dict[str, Any]) -> str:
        """将 skill 元信息格式化为 prompt 片段。"""
        lines = []
        for skill_id, meta in sorted(candidates.items()):
            triggers = meta.when_to_use or ""
            desc = (meta.description or "")[:100]
            lines.append(f"- {skill_id} | {triggers} | {desc}")
        return "\n".join(lines)

    def _parse_classification(self, response_text: str, valid_ids: set[str]) -> list[str]:
        """解析 LLM 返回的 skill ID 列表，过滤掉无效 ID。"""
        text = response_text.strip()
        if not text or text.upper() == "NONE":
            return []
        return [sid.strip() for sid in text.split(",") if sid.strip() in valid_ids]

    def _post_validate_matches(self, current_user_text: str, matched_ids: list[str]) -> list[str]:
        """对高风险 meta-skill 做本地显式意图校验。"""
        filtered: list[str] = []
        seen: set[str] = set()
        for skill_id in matched_ids:
            if skill_id in seen:
                continue
            seen.add(skill_id)
            patterns = self.META_SKILL_PATTERNS.get(skill_id)
            if patterns and not any(pattern.search(current_user_text) for pattern in patterns):
                continue
            filtered.append(skill_id)
        return filtered

    # ── 共用方法 ──────────────────────────────────────────────

    def _extract_recent_context(self, messages: list[dict[str, Any]]) -> str:
        """只提取最后一条 user 消息作为 skill router 输入。"""
        if not messages:
            return ""
        for msg in reversed(messages):
            content = msg.get("content", "")
            if msg.get("role") == "user" and isinstance(content, str):
                return content.lower()
        return ""
