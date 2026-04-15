from __future__ import annotations

from pathlib import Path
from typing import Any

from core.prompt.assembler import PromptAssembler
from core.query.loop import QueryLoop
from core.session.commands import execute_skills_command
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageViewBuilder
from core.skills import SkillRegistry, compute_skills_revision


class SessionEngine:
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
        skill_registry=None,
        tools=None,
        renderer=None,
    ):
        self._state = SessionState(conversation_messages=[])
        self._store = SessionStore(self._state)
        self._skill_registry = skill_registry or SkillRegistry()
        self._prompt_assembler = PromptAssembler(skill_registry=self._skill_registry)
        self._view_builder = view_builder or MessageViewBuilder(tools=tools)
        self._query_loop = query_loop or QueryLoop()
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
        """Discover local skills and inject system/environment messages."""
        if self._bootstrapped:
            return
        working_dir = Path(self._tool_context.working_dir) if self._tool_context else Path(".")
        skills_dir = working_dir / ".harness" / "skills"
        self._state.skill_catalog = self._skill_registry.discover(
            skills_dir,
            working_dir=working_dir,
        )
        self._state.skills_revision = compute_skills_revision(self._state.skill_catalog)
        self._bootstrap_session_messages()
        self._bootstrapped = True

    def handle_command(self, raw: str) -> str:
        """Handle a /skills command. Returns output string."""
        self.bootstrap()
        result = execute_skills_command(raw, state=self._state, registry=self._skill_registry)
        return result.output

    def _bootstrap_session_messages(self) -> None:
        stable_prompt = self._prompt_assembler.build_stable(
            self._state,
            project_root=self._tool_context.working_dir if self._tool_context else None,
        )
        if not self._state.conversation_messages or self._state.conversation_messages[0]["role"] != "system":
            self._store.prepend({"role": "system", "content": stable_prompt})

        environment_message = self._prompt_assembler.build_environment_message(
            working_dir=self._tool_context.working_dir if self._tool_context else ".",
        )
        has_environment = any(
            message.get("role") == "user" and "<environment>" in message.get("content", "")
            for message in self._state.conversation_messages
        )
        if not has_environment:
            self._store.append(environment_message)

    def submit_user_message(self, text: str):
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
            renderer=self._renderer,
        )
