"""提示词组装器 — 构建 system prompt 的三层结构。

你在数据流中的位置：
    MessageViewBuilder.build()
      → prompt_assembler.build_stable()        ← 你在这里
      → prompt_assembler.build_runtime()
      → prompt_assembler.build_overlay()
    → 拼接为完整的 system prompt

三层设计（稳定性递减）：
    1. stable（缓存层）：框架指令 + skill 目录
       - 内容在 skill 不变时完全不变，可以通过 cache key 命中
       - cache key = skills_revision + system_context sha256

    2. runtime（动态层）：环境 + 激活的 skill + todo + 文件状态
       - 每轮都重新渲染，因为 skill 激活状态、todo 进度、文件缓存都会变
       - 包裹在 <runtime-context> XML 标签中

    3. overlay（信号层）：replan 标记、barrier 原因
       - 仅在有控制面信号时才生成
       - 例如 skill 刚展开时注入 <todo-replan>skill_expanded</todo-replan>

核心原则：所有 prompt 内容从 state 实时渲染，不依赖 transcript。
这样即使 transcript 被截断，模型仍能看到完整的 skill 指令和 todo 状态。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.prompt.cache import PromptCache
from core.prompt.system_context import get_system_context, get_user_context
from core.query.state import RunState
from core.session.state import SessionState, TodoItem

if TYPE_CHECKING:
    from core.skills.models import InvokedSkillRecord, SkillMeta
    from core.skills.registry import SkillRegistry
    from core.tools.context import FileState


def _stable_cache_key(state: SessionState, *, project_root: str | None = None) -> str:
    """生成 stable prompt 的缓存 key。

    只有当 skill 文件发生修改或系统指令变更时，key 才会变化。
    同一个会话内，大部分轮次都会命中缓存，避免重复渲染。
    """
    system_prompt = get_system_context(project_root=project_root)
    digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    revision = state.skills_revision or "no-skills"
    return f"stable_system_prompt:{revision}:{digest}"


def _render_skill_catalog(state: SessionState) -> str:
    """将 state.skill_catalog 渲染为 <available-skills> XML。

    这是 stable 层的一部分，告诉模型"你可以用哪些 skill"。
    模型看到匹配的 skill 后会调用 skill 工具加载它。
    """
    if not state.skill_catalog:
        return ""
    lines = ["<available-skills>"]
    for skill_id, meta in sorted(state.skill_catalog.items()):
        lines.append(f'  <skill id="{skill_id}">')
        lines.append(f"    名称：{meta.name}")
        lines.append(f"    描述：{meta.description}")
        if meta.when_to_use:
            lines.append(f"    适用：{meta.when_to_use}")
        lines.append("  </skill>")
    lines.append("</available-skills>")
    return "\n".join(lines)


def _render_todo_state(items: list[TodoItem]) -> str:
    """将 todo 列表渲染为 <todo-state> XML。

    这是 runtime 层的一部分，让模型看到当前的计划进度。
    模型基于此决定下一步做什么、是否需要刷新计划。
    """
    if not items:
        return ""
    lines = ["<todo-state>"]
    for item in items:
        lines.append(f'  <item status="{item.status}">')
        lines.append(f"    {item.active_form}")
        lines.append("  </item>")
    lines.append("</todo-state>")
    return "\n".join(lines)


def _render_file_runtime(read_file_state: dict[str, Any], *, char_budget: int) -> str:
    """将 session 中已读文件的状态渲染为 <file-runtime> XML。

    这是 runtime 层的一部分，让模型知道"我已经看过哪些文件"。
    每个文件最多显示 400 字符的摘要，避免撑爆上下文。

    为什么需要这个？因为 transcript slice 可能截断了早期的 read_file 工具调用，
    但模型仍需要知道文件的概况来做决策。
    """
    from core.tools.context import FileState as _FileState

    if not read_file_state:
        return ""
    lines: list[str] = ["<file-runtime>"]
    budget_used = len("<file-runtime>\n</file-runtime>")
    for path, value in sorted(
        read_file_state.items(),
        key=lambda item: item[1].timestamp if hasattr(item[1], "timestamp") else 0.0,
        reverse=True,
    ):
        state = value
        excerpt = state.content[:400] if hasattr(state, "content") else str(value)[:400]
        is_full = state.is_full_read if hasattr(state, "is_full_read") else True
        block = [
            f'  <file path="{Path(path).name}" full_read="{str(is_full).lower()}">',
            excerpt,
            "  </file>",
        ]
        rendered = "\n".join(block)
        if budget_used + len(rendered) > char_budget:
            break
        lines.extend(block)
        budget_used += len(rendered)
    lines.append("</file-runtime>")
    return "\n".join(lines) if len(lines) > 2 else ""


class PromptAssembler:
    """提示词组装器：从 SessionState 中提取上下文并组装 system prompt。

    提供 4 个组装接口，被 MessageViewBuilder 组合调用：
    - build_stable: 稳定层（缓存命中则跳过渲染）
    - build_runtime: 动态层（每轮重新渲染）
    - build_query_overlay: 信号层（有信号时才生成）
    - build_internal_runtime_view: 调试快照（不发送给模型）
    """

    def __init__(self, cache: PromptCache | None = None, skill_registry: SkillRegistry | None = None):
        self._cache = cache or PromptCache()
        self._skill_registry = skill_registry

    def build_stable(self, state: SessionState, *, project_root: str | None = None) -> str:
        """构建稳定系统提示词（第 1 层）。

        组成：框架指令（_FRAMEWORK_PROMPT）+ skill 目录（<available-skills>）+ 子代理后缀。

        缓存策略：
        - cache key = skills_revision + system_context hash
        - skill 文件没改 → 直接命中缓存 → 零渲染开销
        - skill 文件改了 → 重新渲染 → 新内容写入缓存
        """
        cache_key = _stable_cache_key(state, project_root=project_root)
        cached = self._cache.get(state.prompt_cache, cache_key)
        if cached is not None:
            return cached
        parts = [get_system_context(project_root=project_root)]
        catalog = _render_skill_catalog(state)
        if catalog:
            parts.append(catalog)
        if state.system_prompt_override:
            parts.append(state.system_prompt_override)
        stable_prompt = "\n\n".join(parts)
        return self._cache.set(state.prompt_cache, cache_key, stable_prompt)

    def build_active_skill_messages(self, state: SessionState) -> list[dict[str, str]]:
        """将已激活的 skill 渲染为 <active-skills> 内容。

        从 state.invoked_skills 读取（不是 transcript），
        所以即使 transcript 被截断，模型仍能看到完整的 skill 指令。

        Skill 激活后，其完整内容（包括引用文件）会被存储在 InvokedSkillRecord.content 中，
        每轮都重新拼接到 runtime 层。
        """
        if not state.invoked_skills:
            return []
        parts: list[str] = ["<active-skills>"]
        for skill_id, record in sorted(state.invoked_skills.items(), key=lambda pair: pair[1].invoked_at_turn):
            parts.append(f'  <active-skill id="{skill_id}">')
            parts.append(record.content)
            parts.append("  </active-skill>")
        parts.append("</active-skills>")
        return [{"role": "system", "content": "\n".join(parts)}]

    def build_runtime_context(
        self, state: SessionState, *, working_dir: str, char_budget: int | None = None
    ) -> str:
        """构建运行时上下文（第 2 层），包裹在 <runtime-context> 中。

        内容组成（按优先级排列）：
        1. 环境信息（工作目录、日期、平台）
        2. 激活的 skill 指令（<active-skills>）
        3. Todo 状态（<todo-state>）
        4. 已读文件摘要（<file-runtime>，最多 12K 字符）

        整体截断到 char_budget（默认 36K 字符）。
        """
        total_budget = char_budget or 36_000
        parts: list[str] = []
        user_ctx = get_user_context(working_dir)
        if user_ctx:
            parts.append(user_ctx)
        active_msgs = self.build_active_skill_messages(state)
        if active_msgs:
            parts.append(active_msgs[0]["content"])
        todo_xml = _render_todo_state(state.todo_state.items)
        if todo_xml:
            parts.append(todo_xml)
        file_block = _render_file_runtime(state.read_file_state, char_budget=12_000)
        if file_block:
            parts.append(file_block)
        if not parts:
            return ""
        body = "\n\n".join(part for part in parts if part)[:total_budget].strip()
        if not body:
            return ""
        return f"<runtime-context>\n{body}\n</runtime-context>"

    def build_query_overlay(self, state: SessionState, run_state: RunState) -> str:
        """构建单轮覆盖信号（第 3 层）。

        仅在有控制面信号时生成：
        - <todo-replan>: skill 刚展开，需要模型重新规划 todo
        - <barrier>: 工具要求中断当前批次（如 skill_expanded）

        这个层是最"轻"的，大多数轮次返回空字符串。
        """
        if not run_state.todo_replan_required and not run_state.barrier_reason:
            return ""
        parts: list[str] = ["<query-overlay>"]
        if run_state.todo_replan_required:
            reason = run_state.todo_replan_reason or ""
            parts.append(f"<todo-replan>{reason}</todo-replan>")
        if run_state.barrier_reason:
            parts.append(f"<barrier>{run_state.barrier_reason}</barrier>")
        parts.append("</query-overlay>")
        return "\n".join(parts)

    def build_internal_runtime_view(
        self, state: SessionState, run_state: RunState
    ) -> dict[str, object]:
        """构建内部状态快照，用于调试和日志。不会发送给模型。"""
        return {
            "invoked_skills": list(state.invoked_skills.keys()),
            "todo_items": [item.active_form for item in state.todo_state.items],
            "read_file_state": dict(state.read_file_state),
            "barrier_reason": run_state.barrier_reason,
        }

    def build_stable_context(
        self, state: SessionState, *, project_root: str | None = None
    ) -> str:
        """build_stable 的别名，供外部调用的规范接口。"""
        return self.build_stable(state, project_root=project_root)
