"""会话引擎 — 连接"用户输入"和"QueryLoop 主循环"的协调者。

你在数据流中的位置：
    用户输入
      → SessionEngine.submit_user_message()   ← 你在这里
        → bootstrap()（首次调用时发现 skill）
        → 追加 user 消息到 conversation_messages
        → QueryLoop.run()（进入 think-act 循环）

Engine 持有 SessionState（会话的所有可变状态），其他组件通过 Engine 获取状态引用。
Bootstrap 是幂等的：多次调用只执行一次，且不向 transcript 写入任何消息。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.prompt.assembler import PromptAssembler
from core.query.loop import QueryLoop
from core.session.commands import execute_skills_command
from core.session.context_manager import ContextManager
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageViewBuilder
from core.skills import SkillRegistry, compute_skills_revision

from . import compact_service


class SessionEngine:
    """会话引擎：管理一次完整会话的生命周期。

    职责：
    - bootstrap: 发现本地 skill 并计算 revision（不污染 transcript）
    - handle_command: 处理 /skills 命令
    - submit_user_message: 提交用户消息并启动 QueryLoop
    """

    def __init__(
        self,
        *,
        model_gateway,
        tool_runtime,
        tool_context,
        policy_runner,
        recovery,
        query_loop=None,
        view_builder=None,
        context_manager=None,
        skill_registry=None,
        tools=None,
        renderer=None,
    ):
        """
        Args:
            model_gateway: 模型网关，执行 API 调用。
            tool_runtime: 工具运行时，执行工具批次。
            tool_context: 工具上下文（包含 working_dir），会绑定 session_state。
            policy_runner: 策略运行器，控制循环行为（如 max_turns）。
            recovery: 恢复管理器，处理空响应。
            query_loop: 查询循环实例，默认创建 QueryLoop()。
            view_builder: 消息视图构建器，默认创建 MessageViewBuilder(tools)。
            context_manager: 查询前上下文管理器，默认创建 ContextManager(...)。
            skill_registry: Skill 注册器，默认创建 SkillRegistry()。
            tools: 可用工具 schema 列表，传给 MessageViewBuilder。
            renderer: UI 渲染器，可选。
        """
        self._state = SessionState(conversation_messages=[])
        self._store = SessionStore(self._state)
        self._skill_registry = skill_registry or SkillRegistry()
        self._prompt_assembler = PromptAssembler(skill_registry=self._skill_registry)
        self._view_builder = view_builder or MessageViewBuilder(tools=tools)
        self._query_loop = query_loop or QueryLoop()
        self._context_manager = context_manager or ContextManager(
            compact_service=compact_service,
            summary_gateway=model_gateway,
        )
        self._model_gateway = model_gateway
        self._tool_runtime = tool_runtime
        self._tool_context = tool_context
        self._policy_runner = policy_runner
        self._recovery = recovery
        self._renderer = renderer
        self._bootstrapped = False

        # Give tool context access to session state and skill registry
        if self._tool_context is not None and hasattr(self._tool_context, "bind_runtime"):
            self._tool_context.bind_runtime(session_state=self._state, skill_registry=self._skill_registry)

    @property
    def state(self) -> SessionState:
        return self._state

    def append_message(self, message: dict[str, Any]) -> None:
        self._store.append(message)

    def bootstrap(self) -> None:
        """发现本地 skill 并计算 revision。

        幂等方法：多次调用只执行一次。不会向 conversation_messages 写入任何消息，
        stable prompt 和 environment 由 PromptAssembler 在每轮查询时实时渲染。

        做了什么：
        1. 扫描 .harness/skills/ 目录下的 SKILL.md 文件
        2. 解析每个 skill 的 frontmatter（name、description、when-to-use）
        3. 计算所有 skill 的 revision hash（用于 stable prompt 缓存 key）
        """
        if self._bootstrapped:
            return
        working_dir = Path(self._tool_context.working_dir) if self._tool_context else Path(".")
        skills_dir = working_dir / ".harness" / "skills"
        self._state.skill_catalog = self._skill_registry.discover(
            skills_dir,
            working_dir=working_dir,
        )
        self._state.skills_revision = compute_skills_revision(self._state.skill_catalog)
        self._bootstrapped = True

    def handle_command(self, raw: str) -> str:
        """处理 /skills 命令（list/show/use/off/reload）。

        这些命令不进入 QueryLoop，直接操作 SessionState 中的 skill_catalog 和 invoked_skills。

        Args:
            raw: 完整的命令字符串，如 "/skills use analysis-report"。

        Returns:
            命令执行结果的可读文本。
        """
        self.bootstrap()
        result = execute_skills_command(raw, state=self._state, registry=self._skill_registry)
        return result.output

    def submit_user_message(self, text):
        """提交用户消息并执行查询循环。

        完整流程：
        1. bootstrap() — 首次调用时发现 skill（幂等）
        2. 追加 user 消息到 conversation_messages
        3. 启动 QueryLoop.run() — 进入 think-act 循环
           QueryLoop 内部会反复调用 view_builder → model_gateway → tool_runtime
           直到模型给出最终文本或达到轮次上限

        Args:
            text: 用户输入的文本。

        Returns:
            QueryResult 包含最终输出、停止原因等。
        """
        self.bootstrap()
        self._store.append({"role": "user", "content": text})
        return self._query_loop.run(
            session_state=self._state,
            store=self._store,
            view_builder=self._view_builder,
            prompt_assembler=self._prompt_assembler,
            model_gateway=self._model_gateway,
            tool_runtime=self._tool_runtime,
            tool_context=self._tool_context,
            policy_runner=self._policy_runner,
            recovery=self._recovery,
            context_manager=self._context_manager,
            renderer=self._renderer,
        )
