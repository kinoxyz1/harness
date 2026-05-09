from core.policy.skill_usage_nudge import SkillUsageNudgePolicy
from core.session.state import SessionState
from core.skills.models import SkillMeta, InvokedSkillRecord
from core.query.state import RunState
from pathlib import Path


def _make_meta(skill_id: str) -> SkillMeta:
    return SkillMeta(
        skill_id=skill_id, name=skill_id, description=f"{skill_id} desc",
        when_to_use=None, skill_dir=Path("."), skill_file=Path("./SKILL.md"),
    )


def test_no_catalog_returns_empty():
    policy = SkillUsageNudgePolicy()
    state = SessionState(conversation_messages=[])
    run_state = RunState()
    assert policy.before_model_call(state, run_state) == []


def test_nudge_fires_after_stale_threshold():
    policy = SkillUsageNudgePolicy()
    state = SessionState(conversation_messages=[])
    state.skill_catalog = {"analysis-report": _make_meta("analysis-report")}
    # 模拟 6 次 query 没有 skill 激活
    state.queries_since_skill_activation = 5  # before_model_call 会 +1 = 6
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    assert len(messages) == 1
    assert "skill_nudge" in messages[0]["content"]


def test_nudge_does_not_fire_below_threshold():
    policy = SkillUsageNudgePolicy()
    state = SessionState(conversation_messages=[])
    state.skill_catalog = {"analysis-report": _make_meta("analysis-report")}
    state.queries_since_skill_activation = 3  # +1 = 4, < 6
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    assert messages == []


def test_skills_use_command_resets_counter():
    """模拟 /skills use 命令：直接写入 invoked_skills，不经过 tool batch。"""
    policy = SkillUsageNudgePolicy()
    state = SessionState(conversation_messages=[])
    state.skill_catalog = {"analysis-report": _make_meta("analysis-report")}
    state.queries_since_skill_activation = 5

    # 模拟 /skills use 命令直接激活 skill
    state.invoked_skills["analysis-report"] = InvokedSkillRecord(
        skill_id="analysis-report",
        skill_path="./SKILL.md",
        content_digest="abc",
        content="...",
        invoked_at_turn=0,
    )
    # last_known_skill_keys 仍是空集 = 快照对比会检测到变化

    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    # 检测到新 skill → 重置计数器 → 不触发 nudge
    assert messages == []
    assert state.queries_since_skill_activation == 0


def test_all_skills_invoked_no_nudge():
    policy = SkillUsageNudgePolicy()
    state = SessionState(conversation_messages=[])
    state.skill_catalog = {"analysis-report": _make_meta("analysis-report")}
    state.invoked_skills["analysis-report"] = InvokedSkillRecord(
        skill_id="analysis-report", skill_path="./SKILL.md",
        content_digest="abc", content="...", invoked_at_turn=0,
    )
    state.last_known_skill_keys = {"analysis-report"}
    state.queries_since_skill_activation = 10
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    # 所有 skill 都已激活，uninvited 为空
    assert messages == []
