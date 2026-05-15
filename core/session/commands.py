from __future__ import annotations

import sys
from datetime import datetime
from dataclasses import dataclass

from core.skills import SkillRegistry, compute_skills_revision
from core.skills.models import SkillEvent
from core.skills.runtime import apply_skill_invocation

@dataclass(slots=True)
class CommandResult:
    handled: bool
    output: str = ""
    resume_session_id: str | None = None
    transcript_messages: list[dict] | None = None


def is_skills_command(raw: str) -> bool:
    return raw.strip().startswith("/skills")


def execute_skills_command(raw: str, *, state, registry: SkillRegistry) -> CommandResult:
    """执行 /skills 子命令。

    支持的子命令：
    - list: 列出所有已发现的 skill
    - show <id>: 显示 skill 的完整内容
    - use <id>: 激活一个 skill（记录到 state.invoked_skills，不污染 transcript）
    - off <id>: 提示 inline skill 无法停用
    - reload: 重新扫描 skills 目录并更新 revision

    Args:
        raw: 完整的命令字符串。
        state: 会话状态。
        registry: Skill 注册器，用于加载 skill 内容。

    Returns:
        CommandResult(handled, output) — handled 恒为 True，output 为可读文本。
    """
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
            apply_skill_invocation(
                state=state,
                skill_id=skill_id,
                content=content,
                turn=0,
            )
        except ValueError as exc:
            return CommandResult(True, str(exc))
        state.skill_events.append(
            SkillEvent(
                skill_id=skill_id,
                action="activated",
                source="user_command",
                conversation_index=len(state.conversation_messages),
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


def is_resume_command(raw: str) -> bool:
    return raw.strip().startswith("/resume")


def execute_resume_command(raw: str, *, session_db) -> CommandResult:
    def _format_timestamp(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    parts = raw.strip().split()
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "list"):
        if session_db is None:
            return CommandResult(True, "SessionDB not available.")
        sessions = [item for item in session_db.list_sessions(limit=20) if item.get("message_count", 0) > 0]
        if not sessions:
            return CommandResult(True, "No previous sessions found.")
        lines = ["Recent sessions (most recently active first):"]
        for item in sessions:
            sid = item["id"]
            stamp = _format_timestamp(float(item.get("updated_at", 0.0)))
            preview = str(item.get("preview", "")).strip()
            suffix = f" — {preview}" if preview else ""
            lines.append(f"- {sid} ({item['message_count']} messages, last active {stamp}){suffix}")
        lines.append("")
        lines.append("Use /resume <session_id> to restore a session.")
        return CommandResult(True, "\n".join(lines))

    if len(parts) == 2:
        transcript_messages: list[dict] = []
        if session_db is not None:
            messages = session_db.get_messages(parts[1])
            if not messages:
                snapshot = session_db.load_session_snapshot(parts[1])
                if snapshot is not None:
                    messages = list(snapshot.get("conversation_messages", []))
            transcript_messages = list(messages)
        return CommandResult(
            handled=True,
            output=f"Resuming session {parts[1]}...",
            resume_session_id=parts[1],
            transcript_messages=transcript_messages,
        )

    return CommandResult(True, "Usage: /resume | /resume list | /resume <session_id>")
