from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from core.prompt.cache import PromptCache
from core.prompt.system_context import get_system_context, get_user_context
from core.query.state import RunState
from core.session.state import SessionState

if TYPE_CHECKING:
    from core.skills.models import SkillMeta
    from core.skills.registry import SkillRegistry


def _stable_cache_key(state: SessionState, *, project_root: str | None = None) -> str:
    system_prompt = get_system_context(project_root=project_root)
    digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    revision = state.skills_revision or "no-skills"
    return f"stable_system_prompt:{revision}:{digest}"


def _render_skill_catalog(state: SessionState) -> str:
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


def _render_reference_files(meta: SkillMeta, reference_bodies: dict[str, str]) -> list[str]:
    if not meta.references:
        return []
    lines = ["    <reference-files>"]
    for ref in meta.references:
        lines.append(f'      <file path="{ref.prompt_path}">')
        body_text = reference_bodies.get(ref.prompt_path)
        if body_text:
            lines.append(body_text)
        elif ref.purpose:
            lines.append(f"        {ref.purpose}")
        lines.append("      </file>")
    lines.append("    </reference-files>")
    return lines


class PromptAssembler:
    def __init__(self, cache: PromptCache | None = None, skill_registry: SkillRegistry | None = None):
        self._cache = cache or PromptCache()
        self._skill_registry = skill_registry

    def build_stable(self, state: SessionState, *, project_root: str | None = None) -> str:
        cache_key = _stable_cache_key(state, project_root=project_root)
        cached = self._cache.get(state.prompt_cache, cache_key)
        if cached is not None:
            return cached
        parts = [get_system_context(project_root=project_root)]
        catalog = _render_skill_catalog(state)
        if catalog:
            parts.append(catalog)
        stable_prompt = "\n\n".join(parts)
        return self._cache.set(state.prompt_cache, cache_key, stable_prompt)

    def build_active_skill_messages(self, state: SessionState) -> list[dict[str, str]]:
        return []

    def build_environment_message(self, *, working_dir: str) -> dict[str, str]:
        return {"role": "user", "content": get_user_context(working_dir)}

    def build_dynamic(self, state: SessionState, run_state: RunState) -> list[dict[str, str]]:
        return []
