"""会话状态 — 整个 Agent 运行期间的核心数据结构。

你在数据流中的位置：
    几乎所有组件都通过引用共享 SessionState：
    - SessionEngine 创建并持有它
    - QueryLoop 读写会话级长期状态，并配合 RunState 维护单轮过渡信息
    - PromptAssembler 从中读取 skill_catalog、invoked_skills、todo_state 来渲染 system prompt
    - MessageViewBuilder 从中读取 conversation_messages 来截取 transcript slice
    - ToolExecutorRuntime 应用工具返回的 session updates，刷新 read_file_state

两个层次的状态：
    SessionState（本文件）：跨 query 持久化，在一次会话中共享
    RunState（query/state.py）：单次 query 内部，每次 run() 重新创建
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.skills import SkillEvent, SkillMeta
from core.skills.models import InvokedSkillRecord


@dataclass(slots=True)
class TodoItem:
    """单个 todo 项。模型通过 todo 工具管理任务计划。"""
    content: str           # 完整任务描述（做什么、对什么对象、预期产出）
    active_form: str       # 简短展示文本，用于 UI spinner
    status: str            # pending / in_progress / completed
    workflow_ref: str | None = None  # 可选的 workflow 步骤编号


@dataclass(slots=True)
class TodoState:
    """Todo 列表状态，跟踪计划变更和 UI 展示去重。"""
    items: list[TodoItem] = field(default_factory=list)
    last_completed_items: list[TodoItem] = field(default_factory=list)
    last_write_turn: int | None = None       # 上次写入 todo 时的 turn 编号
    last_reminder_turn: int | None = None    # 上次注入"计划过时"提醒的 turn，防止重复


def _default_compact_state() -> dict[str, Any]:
    return {
        "tool_result_replacements": {},
        "consecutive_summary_failures": 0,
        "summary_compact_cooldown_until": 0.0,
        "last_prompt_tokens": 0,
        "last_compact_observability": {},
    }


@dataclass(slots=True)
class SessionState:
    """会话级状态 — 跨 query 持久化的所有数据。

    conversation_messages:
        对话历史，append-only（由 SessionStore 管理）。
        包含 user/assistant/tool 三种角色的消息。
        assistant 消息可能携带 reasoning（thinking 文本）和 tool_calls。
        tool 消息包含工具执行结果。
        QueryLoop 每轮追加，MessageViewBuilder 每轮截取。

    invoked_skills:
        已激活的 skill（key=skill_id, value=InvokedSkillRecord）。
        激活路径：(1) 用户 /skills use <id> (2) 模型调用 skill 工具。
        PromptAssembler 每轮从这里面读取 skill 内容，渲染到 <active-skills>。

    prompt_cache:
        stable system prompt 的字符串缓存（key=cache_key, value=渲染结果）。
        skill 不变时命中缓存，避免每轮重新组装。

    read_file_state:
        已读文件的内容缓存（key=绝对路径, value=FileState）。
        由 read_file/write_file/edit_file 等工具通过 session update 维护，
        PromptAssembler 渲染为 <file-runtime>。

    todo_state:
        任务计划。模型通过 todo 工具读写，TodoPlanningPolicy 监控是否过时。

    skill_catalog:
        已发现的 skill 元信息。bootstrap 时从 .harness/skills/ 扫描填充。
        只包含 name/description 等摘要，完整内容在 load() 时才读取。

    skills_revision:
        skill 目录的变更指纹（基于所有 SKILL.md 的 mtime）。
        用于 stable prompt 的缓存 key —— revision 不变就不用重新渲染。
    """
    conversation_messages: list[dict[str, Any]]
    prompt_cache: dict[str, str] = field(default_factory=dict)
    discovered_tools: set[str] = field(default_factory=set)
    skill_catalog: dict[str, SkillMeta] = field(default_factory=dict)
    skill_events: list[SkillEvent] = field(default_factory=list)
    invoked_skills: dict[str, InvokedSkillRecord] = field(default_factory=dict)
    skills_revision: str | None = None
    read_file_state: dict[str, Any] = field(default_factory=dict)
    system_prompt_override: str | None = None
    session_metadata: dict[str, Any] = field(default_factory=dict)
    usage_totals: dict[str, int] = field(default_factory=dict)
    todo_state: TodoState = field(default_factory=TodoState)
    compact_state: dict[str, Any] = field(default_factory=_default_compact_state)
