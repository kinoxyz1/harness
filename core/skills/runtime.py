from __future__ import annotations

from core.skills.models import InvokedSkillRecord, SkillContent


def build_skill_runtime_body(skill_id: str, content: SkillContent) -> str:
    """将 skill 内容渲染为运行时 XML 字符串。

    生成的 XML 包含 skill 指令体和引用文件（如有），
    后续会存储到 InvokedSkillRecord.content 中供 PromptAssembler 读取。

    Args:
        skill_id: skill 标识符。
        content: 加载后的 skill 内容，包含 body、reference_bodies 等。

    Returns:
        完整的 <skill-runtime> XML 字符串。
    """
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
    return "\n".join(lines)


def ensure_inline_skill_budget(*, state, new_content: str, max_chars: int = 24_000) -> None:
    """检查 inline skill 字符预算，超出则抛出 ValueError。

    计算逻辑：累加 state.invoked_skills 中所有已有 record 的 content 长度，
    加上即将新增的 new_content 长度，与 max_chars 比较。

    Args:
        state: 会话状态，包含 invoked_skills。
        new_content: 即将新增的 skill 渲染内容。
        max_chars: 最大允许的累计字符数，默认 24_000。

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
