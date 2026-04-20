from __future__ import annotations

from core.skills.models import InvokedSkillRecord, SkillContent


def build_skill_runtime_body(skill_id: str, content: SkillContent) -> str:
    """将 skill 内容渲染为运行时 XML 字符串。

    生成的 XML 包含 skill 指令体和引用文件（如有），
    后续会存储到 InvokedSkillRecord.content 中供 PromptAssembler 读取。

    遵循 Claude Code 的模式：注入 skill 目录路径，使模型可以用 Read 工具
    按需读取 skill 目录下的其他文件。

    Args:
        skill_id: skill 标识符。
        content: 加载后的 skill 内容，包含 body、reference_bodies 等。

    Returns:
        完整的 <skill-runtime> XML 字符串。
    """
    skill_dir = str(content.meta.skill_dir)
    lines = [
        "<skill-runtime>",
        f'  <skill id="{skill_id}" source="local-inline">',
        f"    Base directory for this skill: {skill_dir}",
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
    return "\n".join(lines)


def ensure_inline_skill_budget(*, state, new_content: str, max_chars: int = 500_000) -> None:
    """检查 inline skill 字符预算。

    大模型上下文通常为 200K tokens（约 800K 字符），skill 内容占比极小，
    不应人为限制。默认预算设为 500K 字符，仅在极端情况下保护。

    Args:
        state: 会话状态，包含 invoked_skills。
        new_content: 即将新增的 skill 渲染内容。
        max_chars: 最大允许的累计字符数，默认 500_000。

    Raises:
        ValueError: 超出预算时。
    """
    used_chars = sum(len(rec.content) for rec in state.invoked_skills.values())
    if used_chars + len(new_content) > max_chars:
        raise ValueError(f"Inline skill budget exceeded: {used_chars + len(new_content)} > {max_chars}")


def apply_skill_invocation(*, state, skill_id: str, content: SkillContent, turn: int) -> InvokedSkillRecord:
    """激活一个 skill，将其记录到 state.invoked_skills 中。

    不再向 conversation_messages 注入任何消息。skill 的运行时内容由
    PromptAssembler.build_active_skill_messages() 从 state.invoked_skills 实时渲染。

    Args:
        state: 会话状态，invoked_skills 字典会被更新。
        skill_id: 要激活的 skill 标识符。
        content: 加载后的 skill 内容。
        turn: 激活时的轮次编号。

    Returns:
        创建的 InvokedSkillRecord，已存入 state.invoked_skills。

    Raises:
        ValueError: 超出字符预算时。
    """
    body = build_skill_runtime_body(skill_id, content)
    ensure_inline_skill_budget(state=state, new_content=body)
    record = InvokedSkillRecord(
        skill_id=skill_id,
        skill_path=str(content.meta.skill_file),
        content_digest=content.content_digest,
        content=body,
        invoked_at_turn=turn,
    )
    state.invoked_skills[skill_id] = record
    return record
