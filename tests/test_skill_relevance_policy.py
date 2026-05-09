from core.policy.skill_relevance import SkillRelevancePolicy
from core.session.state import SessionState
from core.skills.models import SkillMeta
from core.query.state import RunState
from pathlib import Path


def _make_meta(skill_id: str, description: str, when_to_use: str | None = None) -> SkillMeta:
    return SkillMeta(
        skill_id=skill_id,
        name=skill_id,
        description=description,
        when_to_use=when_to_use,
        skill_dir=Path("."),
        skill_file=Path("./SKILL.md"),
    )


def test_no_catalog_returns_empty():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[])
    run_state = RunState()
    assert policy.before_model_call(state, run_state) == []


def test_matching_skill_injects_reminder():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[
        {"role": "user", "content": "基于csv文件生成分析报告"},
    ])
    state.skill_catalog = {
        "analysis-report": _make_meta("analysis-report", "生成分析报告", "生成分析报告,数据报告,HTML报告"),
    }
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "analysis-report" in messages[0]["content"]
    assert "skill_relevance" in messages[0]["content"]


def test_already_invoked_skill_is_skipped():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[
        {"role": "user", "content": "生成分析报告"},
    ])
    state.skill_catalog = {
        "analysis-report": _make_meta("analysis-report", "生成分析报告", "分析报告"),
    }
    state.invoked_skills["analysis-report"] = ...  # 任意非 None 值表示已激活
    run_state = RunState()
    messages = policy.before_model_call(state, run_state)
    assert messages == []


def test_cooldown_prevents_repeated_reminder():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[
        {"role": "user", "content": "生成分析报告"},
    ])
    state.skill_catalog = {
        "analysis-report": _make_meta("analysis-report", "生成分析报告", "分析报告"),
    }
    run_state = RunState()

    # 第一次调用：注入
    messages1 = policy.before_model_call(state, run_state)
    assert len(messages1) == 1

    # 第二次调用（stale 没变）：冷却中，不注入
    messages2 = policy.before_model_call(state, run_state)
    assert messages2 == []


def test_no_matching_context_returns_empty():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[
        {"role": "user", "content": "帮我写一个 hello world 程序"},
    ])
    state.skill_catalog = {
        "analysis-report": _make_meta("analysis-report", "生成分析报告", "分析报告,数据报告"),
    }
    run_state = RunState()
    assert policy.before_model_call(state, run_state) == []
