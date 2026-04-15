from __future__ import annotations

from typing import Any

from core.prompt.assembler import PromptAssembler
from core.query.loop import QueryLoop
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageViewBuilder


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
    ):
        self._state = SessionState(conversation_messages=[])
        self._store = SessionStore(self._state)
        self._view_builder = view_builder or MessageViewBuilder()
        self._prompt_assembler = PromptAssembler()
        self._query_loop = query_loop or QueryLoop()
        self._model_gateway = model_gateway
        self._tool_runtime = tool_runtime
        self._tool_context = tool_context
        self._policy_runner = policy_runner
        self._recovery = recovery
        self._bootstrapped = False

    @property
    def state(self) -> SessionState:
        return self._state

    def append_message(self, message: dict[str, Any]) -> None:
        self._store.append(message)

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
        if not self._bootstrapped:
            self._bootstrap_session_messages()
            self._bootstrapped = True
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
        )
