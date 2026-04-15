from __future__ import annotations

from core.skills.models import InvokedSkillRecord, SkillContent


def build_skill_runtime_message(skill_id: str, content: SkillContent) -> dict[str, str]:
    lines = [
        "<skill-runtime>",
        f'  <skill id="{skill_id}" source="local-inline">',
        "    <instruction>",
        content.body,
        "    </instruction>",
    ]
    if content.reference_bodies:
        lines.append("    <reference-files>")
        for path, body in content.reference_bodies.items():
            lines.append(f'      <file path="{path}">')
            lines.append(body)
            lines.append("      </file>")
        lines.append("    </reference-files>")
    lines.extend(["  </skill>", "</skill-runtime>"])
    return {"role": "system", "content": "\n".join(lines)}


def ensure_inline_skill_budget(*, state, new_content: str, max_chars: int = 24_000) -> None:
    used_chars = sum(
        len(message.get("content", ""))
        for message in state.conversation_messages
        if message.get("role") == "system" and "<skill-runtime>" in message.get("content", "")
    )
    if used_chars + len(new_content) > max_chars:
        raise ValueError(f"Inline skill budget exceeded: {used_chars + len(new_content)} > {max_chars}")


def apply_skill_invocation(*, state, skill_id: str, content: SkillContent, turn: int) -> dict[str, str]:
    message = build_skill_runtime_message(skill_id, content)
    ensure_inline_skill_budget(state=state, new_content=message["content"])
    state.invoked_skills[skill_id] = InvokedSkillRecord(
        skill_id=skill_id,
        skill_path=str(content.meta.skill_file),
        content_digest=content.content_digest,
        content=message["content"],
        invoked_at_turn=turn,
    )
    return message
