from __future__ import annotations

from core.prompt.cache import PromptCache
from core.prompt.system_context import get_system_context, get_user_context
from core.query.state import RunState
from core.session.state import SessionState


class PromptAssembler:
    def __init__(self, cache: PromptCache | None = None):
        self._cache = cache or PromptCache()

    def build_stable(self, state: SessionState, *, project_root: str | None = None) -> str:
        cached = self._cache.get(state.prompt_cache, "stable_system_prompt")
        if cached is not None:
            return cached
        stable_prompt = get_system_context(project_root=project_root)
        return self._cache.set(state.prompt_cache, "stable_system_prompt", stable_prompt)

    def build_environment_message(self, *, working_dir: str) -> dict[str, str]:
        return {"role": "user", "content": get_user_context(working_dir)}

    def build_dynamic(self, state: SessionState, run_state: RunState) -> list[dict[str, str]]:
        return []
