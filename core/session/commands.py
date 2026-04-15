from __future__ import annotations

import sys
from dataclasses import dataclass

from core.skills import SkillRegistry, compute_skills_revision
from core.skills.models import SkillEvent
from core.skills.runtime import apply_skill_invocation

MAX_ACTIVE_SKILLS = 3  # deprecated compatibility constant
MAX_TOTAL_SKILL_CHARS = 24000  # deprecated compatibility constant


def _skill_total_chars(content) -> int:  # deprecated compatibility helper
    return len(content.body) + sum(len(v) for v in content.reference_bodies.values())


@dataclass(slots=True)
class CommandResult:
    handled: bool
    output: str = ""


def is_skills_command(raw: str) -> bool:
    return raw.strip().startswith("/skills")


def execute_skills_command(raw: str, *, state, registry: SkillRegistry) -> CommandResult:
    parts = raw.strip().split()
    if len(parts) < 2:
        return CommandResult(True, "Usage: /skills list|show <id>|use <id>|off <id>|reload")

    subcmd = parts[1]

    if subcmd == "list":
        if not state.skill_catalog:
            return CommandResult(True, "(no skills found)")
        lines = []
        for skill_id, meta in sorted(state.skill_catalog.items()):
            line = f"- {skill_id}: {meta.description}"
            lines.append(line)
        return CommandResult(True, "\n".join(lines))

    if subcmd == "show" and len(parts) == 3:
        skill_id = parts[2]
        if skill_id not in state.skill_catalog:
            return CommandResult(True, f"Skill not found: {skill_id}")
        content = registry.load(skill_id)
        return CommandResult(True, content.body)

    if subcmd == "use" and len(parts) == 3:
        skill_id = parts[2]
        if skill_id not in state.skill_catalog:
            return CommandResult(True, f"Skill not found: {skill_id}")
        content = registry.load(skill_id)
        try:
            message = apply_skill_invocation(
                state=state,
                skill_id=skill_id,
                content=content,
                turn=0,
            )
        except ValueError as exc:
            return CommandResult(True, str(exc))
        state.conversation_messages.append(message)
        state.skill_events.append(
            SkillEvent(
                skill_id=skill_id,
                action="activated",
                source="user_command",
                conversation_index=len(state.conversation_messages) - 1,
            )
        )
        ref_count = len(content.reference_bodies)
        ref_chars = sum(len(v) for v in content.reference_bodies.values())
        sys.stdout.write(
            f"\033[36m[Skill] 激活 {skill_id}"
            f" ({ref_count} refs, {ref_chars:,} chars 内联)\033[0m\n"
        )
        return CommandResult(True, f"Loaded skill inline: {skill_id}")

    if subcmd == "off" and len(parts) == 3:
        return CommandResult(
            True,
            "Inline-loaded skills cannot be deactivated from history; start a new session if you need a clean context.",
        )

    if subcmd == "reload":
        if registry.skills_dir is None:
            return CommandResult(True, "No skills directory configured")
        state.skill_catalog = registry.discover(
            registry.skills_dir,
            working_dir=registry.working_dir,
        )
        state.skills_revision = compute_skills_revision(state.skill_catalog)
        state.skill_events.append(
            SkillEvent(
                skill_id="*",
                action="reload",
                source="user_command",
                conversation_index=len(state.conversation_messages),
            )
        )
        # Cache key includes skills_revision, so the old stable prompt
        # cache entry is naturally bypassed by the new revision.
        skill_count = len(state.skill_catalog)
        sys.stdout.write(
            f"\033[36m[Skill] 重新加载 skills 目录 ({skill_count} skills discovered)\033[0m\n"
        )
        return CommandResult(True, "Reloaded skills")

    return CommandResult(True, "Usage: /skills list|show <id>|use <id>|off <id>|reload")
