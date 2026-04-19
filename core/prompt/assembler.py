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
    """生成 stable prompt 的缓存 key，由 skills_revision + system_context sha256 组成。"""
    system_prompt = get_system_context(project_root=project_root)
    digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    revision = state.skills_revision or "no-skills"
    return f"stable_system_prompt:{revision}:{digest}"


def _render_skill_catalog(state: SessionState) -> str:
    """将 state.skill_catalog 渲染为 <available-skills> XML，列出所有已发现的 skill 名称和描述。"""
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
    """将 todo 列表渲染为 <todo-state> XML，包含每项的 status 和 active_form。"""
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

    按最近读取时间倒序排列，每个文件最多截取 400 字符，总大小不超过 char_budget。
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
    """提示词组装器：负责从 SessionState 中提取各类上下文并组装为发送给模型的 prompt。

    核心设计原则：所有 prompt 内容都从 state 实时渲染，不依赖 transcript 中的历史消息。
    这样即使 transcript 被压缩或截断，模型仍然能获得完整的运行时上下文。

    提供 4 个组装接口，被 MessageViewBuilder 组合调用：
    - build_stable_context: 稳定不变的系统提示（系统指令 + skill 目录 + 子代理后缀）
    - build_runtime_context: 每轮变化的运行时上下文（环境 + 激活的 skill + todo + 文件）
    - build_query_overlay: 单轮查询覆盖层（replan 标记、barrier 原因）
    - build_internal_runtime_view: 调试用的内部状态快照
    """

    def __init__(self, cache: PromptCache | None = None, skill_registry: SkillRegistry | None = None):
        self._cache = cache or PromptCache()
        self._skill_registry = skill_registry

    def build_stable(self, state: SessionState, *, project_root: str | None = None) -> str:
        """构建稳定系统提示词并缓存。

        内容组成：系统指令 + skill 目录（<available-skills>）+ system_prompt_override（子代理后缀）。
        以 skills_revision + system_context sha256 为 key 缓存，skill 不变时直接命中。

        Args:
            state: 会话状态，包含 skill_catalog、skills_revision、prompt_cache 等。
            project_root: 项目根目录，用于加载项目级系统指令。None 则使用默认指令。

        Returns:
            组装好的稳定系统提示词字符串。
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
        """将已激活的 skill 渲染为 <active-skills> 系统消息。

        从 state.invoked_skills（而非 transcript）读取，按激活轮次排序。
        每个 skill 包裹在 <active-skill id="..."> 标签中，内容为完整运行时指令。

        Args:
            state: 会话状态，包含 invoked_skills 字典。

        Returns:
            长度为 0 或 1 的列表。空列表表示无激活 skill；
            否则返回 [{"role": "system", "content": "<active-skills>..."}]。
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
        """构建运行时上下文，包裹在 <runtime-context> XML 中。

        内容组成（按顺序拼接）：
        1. 用户环境信息（工作目录、系统信息等）
        2. 已激活 skill 的指令内容
        3. Todo 列表状态（<todo-state>）
        4. 已读文件的摘要（<file-runtime>，最多 12K 字符）

        最终整体截断到 char_budget（默认 36K 字符）。

        Args:
            state: 会话状态，包含 invoked_skills、todo_state、read_file_state 等。
            working_dir: 当前工作目录，用于生成环境信息。
            char_budget: 总字符预算上限。None 则使用默认 36_000。

        Returns:
            "<runtime-context>...</runtime-context>" 字符串，或 ""（无内容时）。
        """
        total_budget = char_budget or 36_000
        parts: list[str] = []
        # User context (environment info)
        user_ctx = get_user_context(working_dir)
        if user_ctx:
            parts.append(user_ctx)
        # Active skill messages content
        active_msgs = self.build_active_skill_messages(state)
        if active_msgs:
            parts.append(active_msgs[0]["content"])
        # Todo state
        todo_xml = _render_todo_state(state.todo_state.items)
        if todo_xml:
            parts.append(todo_xml)
        # File runtime
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
        """构建单轮查询覆盖层，包含需要模型注意的控制面信号。

        当需要模型重新规划 todo（如 skill 扩展后）或 barrier 触发时，
        生成 <query-overlay> 提示模型调整行为。

        Args:
            state: 会话状态（本方法当前未使用 state，保留参数供扩展）。
            run_state: 当轮运行状态，包含 todo_replan_required、barrier_reason 等。

        Returns:
            "<query-overlay>...</query-overlay>" 字符串，或 ""（无覆盖信号时）。
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
        """构建内部运行时状态快照，用于调试和日志。

        Args:
            state: 会话状态。
            run_state: 当轮运行状态。

        Returns:
            包含以下 key 的字典：
            - invoked_skills: list[str] — 已激活 skill ID 列表
            - todo_items: list[str] — todo 项的 active_form 列表
            - read_file_state: dict — 已读文件状态映射
            - barrier_reason: str | None — barrier 触发原因
        """
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
