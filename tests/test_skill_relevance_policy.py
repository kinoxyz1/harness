import os
from unittest.mock import MagicMock, patch

from core.policy.skill_relevance import SkillRelevancePolicy
from core.session.state import SessionState
from core.skills.models import SkillMeta
from core.query.state import RunState
from core.llm.client import ModelGateway, ModelResponse
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
    assert "阻塞要求" not in messages[0]["content"]
    assert "必须先调用 skill 工具" not in messages[0]["content"]
    assert "可能与当前任务相关" in messages[0]["content"]


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


def test_llm_match_disabled_uses_keywords():
    """SKILL_LLM_MATCH=false (default) → keyword matching, no LLM call."""
    gateway = MagicMock(spec=ModelGateway)
    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", False):
        policy = SkillRelevancePolicy(model_gateway=gateway)
        assert policy._use_llm is False
        # Verify keyword matching still works
        state = SessionState(conversation_messages=[
            {"role": "user", "content": "基于csv文件生成分析报告"},
        ])
        state.skill_catalog = {
            "analysis-report": _make_meta("analysis-report", "生成分析报告", "生成分析报告,数据报告,HTML报告"),
        }
        messages = policy.before_model_call(state, RunState())
        assert len(messages) == 1
        # Gateway should NOT have been called
        gateway.call_once.assert_not_called()


def test_llm_match_enabled_dispatches_to_llm():
    """SKILL_LLM_MATCH=true → LLM classification call."""
    mock_resp = ModelResponse(
        content="analysis-report",
        tool_calls=[],
        finish_reason="end_turn",
        prompt_tokens=100,
        completion_tokens=5,
        reasoning="",
        reasoning_signature="",
    )
    gateway = MagicMock(spec=ModelGateway)
    gateway.call_once.return_value = mock_resp

    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=gateway)
        assert policy._use_llm is True

        state = SessionState(conversation_messages=[
            {"role": "user", "content": "帮我看看这个项目的数据"},
        ])
        state.skill_catalog = {
            "analysis-report": _make_meta("analysis-report", "生成分析报告", "生成分析报告,数据报告"),
        }
        messages = policy.before_model_call(state, RunState())
        assert len(messages) == 1
        assert "analysis-report" in messages[0]["content"]
        gateway.call_once.assert_called_once()


def test_llm_match_returns_none():
    """LLM returns NONE → no nudge injected."""
    mock_resp = ModelResponse(
        content="NONE",
        tool_calls=[],
        finish_reason="end_turn",
        prompt_tokens=100,
        completion_tokens=2,
        reasoning="",
        reasoning_signature="",
    )
    gateway = MagicMock(spec=ModelGateway)
    gateway.call_once.return_value = mock_resp

    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=gateway)
        state = SessionState(conversation_messages=[
            {"role": "user", "content": "今天天气怎么样"},
        ])
        state.skill_catalog = {
            "analysis-report": _make_meta("analysis-report", "生成分析报告", "分析报告"),
        }
        messages = policy.before_model_call(state, RunState())
        assert messages == []
        gateway.call_once.assert_called_once()


def test_llm_match_failure_silently_degrades():
    """LLM call raises → silent fallback, no nudge, no crash."""
    gateway = MagicMock(spec=ModelGateway)
    gateway.call_once.side_effect = RuntimeError("API error")

    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=gateway)
        state = SessionState(conversation_messages=[
            {"role": "user", "content": "帮我分析这个项目"},
        ])
        state.skill_catalog = {
            "analysis-report": _make_meta("analysis-report", "生成分析报告", "分析报告"),
        }
        # Should NOT raise, just return empty
        messages = policy.before_model_call(state, RunState())
        assert messages == []


def test_llm_match_semantic_match_succeeds():
    """User says '看看...项目' → LLM matches code-explorer even though keywords wouldn't."""
    mock_resp = ModelResponse(
        content="code-explorer",
        tool_calls=[],
        finish_reason="end_turn",
        prompt_tokens=150,
        completion_tokens=5,
        reasoning="",
        reasoning_signature="",
    )
    gateway = MagicMock(spec=ModelGateway)
    gateway.call_once.return_value = mock_resp

    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=gateway)
        state = SessionState(conversation_messages=[
            {"role": "user", "content": "帮我看看最近很火的gstack项目是干什么的"},
        ])
        state.skill_catalog = {
            "code-explorer": _make_meta(
                "code-explorer",
                "Explore codebase",
                "探索项目, 理解代码库, 分析仓库, 看看这个项目",
            ),
        }
        messages = policy.before_model_call(state, RunState())
        assert len(messages) == 1
        assert "code-explorer" in messages[0]["content"]


def test_no_gateway_disables_llm():
    """model_gateway=None → always uses keywords regardless of config."""
    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=None)
        assert policy._use_llm is False


def test_extract_current_user_context_ignores_assistant_history():
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[
        {"role": "user", "content": "帮我看看 gstack 是做什么的"},
        {"role": "assistant", "content": "skill creator 可以用来 create a new skill and extend capabilities"},
        {"role": "user", "content": "这个项目的 23 个 skill 是 subagent 吗？"},
    ])

    assert policy._extract_recent_context(state.conversation_messages) == "这个项目的 23 个 skill 是 subagent 吗？".lower()


def test_llm_match_for_skill_creator_requires_explicit_create_intent():
    mock_resp = ModelResponse(
        content="skill-creator",
        tool_calls=[],
        finish_reason="end_turn",
        prompt_tokens=150,
        completion_tokens=5,
        reasoning="",
        reasoning_signature="",
    )
    gateway = MagicMock(spec=ModelGateway)
    gateway.call_once.return_value = mock_resp

    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=gateway)
        state = SessionState(conversation_messages=[
            {"role": "user", "content": "这个项目的 23 个 skill，运行起来是不同的 subagent 吗？"},
        ])
        state.skill_catalog = {
            "skill-creator": _make_meta(
                "skill-creator",
                "Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends Codex's capabilities with specialized knowledge, workflows, or tool integrations.",
            ),
        }

        assert policy.before_model_call(state, RunState()) == []


def test_llm_match_for_skill_creator_allows_explicit_create_intent():
    mock_resp = ModelResponse(
        content="skill-creator",
        tool_calls=[],
        finish_reason="end_turn",
        prompt_tokens=150,
        completion_tokens=5,
        reasoning="",
        reasoning_signature="",
    )
    gateway = MagicMock(spec=ModelGateway)
    gateway.call_once.return_value = mock_resp

    with patch("core.policy.skill_relevance.SKILL_LLM_MATCH", True):
        policy = SkillRelevancePolicy(model_gateway=gateway)
        state = SessionState(conversation_messages=[
            {"role": "user", "content": "帮我创建一个新的 weather skill，并写好 SKILL.md"},
        ])
        state.skill_catalog = {
            "skill-creator": _make_meta(
                "skill-creator",
                "Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends Codex's capabilities with specialized knowledge, workflows, or tool integrations.",
            ),
        }

        messages = policy.before_model_call(state, RunState())
        assert len(messages) == 1
        assert "skill-creator" in messages[0]["content"]
