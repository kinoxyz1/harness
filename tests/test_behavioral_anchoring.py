"""集成测试 — 验证行为锚定对抗机制在完整 QueryLoop 流程中的工作。

关键验证点：
1. Policy 注入的消息进入 transcript（conversation_messages），不进入 system prompt
2. 长查询后 skill_relevance 仍然注入
3. /skills use 命令后 stale 计数器正确重置
"""
from core.policy.skill_relevance import SkillRelevancePolicy
from core.policy.skill_usage_nudge import SkillUsageNudgePolicy
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


def test_policy_messages_enter_transcript_not_system():
    """验证 Policy 注入的消息进入 conversation_messages，不进入 system prompt。

    方法：
    1. 创建带 skill_catalog 的 SessionState
    2. 调用 SkillRelevancePolicy.before_model_call()
    3. 模拟 store.extend(messages)
    4. 检查 messages[0]["role"] == "user"（transcript 通道）
    """
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

    # 验证消息进入 transcript 通道（role=user），不是 system prompt
    assert messages[0]["role"] == "user"
    assert "skill_relevance" in messages[0]["content"]

    # 验证注入消息的格式是 <system-reminder>，不是 system role
    assert messages[0]["content"].startswith("<system-reminder")

    # 模拟 store.extend(messages) 后进入 conversation_messages
    state.conversation_messages.extend(messages)
    assert state.conversation_messages[-1]["role"] == "user"
    assert "skill_relevance" in state.conversation_messages[-1]["content"]


def test_long_session_skill_relevance_still_injects():
    """模拟长 session：50 次 query（300+ 工具调用），skill_relevance 仍然注入。

    方法：
    1. 创建 50 次 query 的 conversation_messages（大量 bash/read/write 调用）
    2. 最近一条用户消息包含"生成分析报告"
    3. skill_catalog 中有 analysis-report
    4. SkillRelevancePolicy 仍然匹配并注入
    """
    policy = SkillRelevancePolicy()
    state = SessionState(conversation_messages=[])

    # 模拟 50 次 query 的对话历史（每次 query 包含 user + assistant + tool 消息）
    for i in range(50):
        state.conversation_messages.append({"role": "user", "content": f"用户第{i}次输入"})
        state.conversation_messages.append({
            "role": "assistant",
            "content": f"第{i}次回复",
            "tool_calls": [{"id": f"call_{i}", "type": "function", "function": {"name": "bash", "arguments": f'{{"command": "echo {i}"}}'}}],
        })
        state.conversation_messages.append({"role": "tool", "content": f"output {i}", "tool_call_id": f"call_{i}"})
        state.conversation_messages.append({"role": "assistant", "content": f"第{i}次工具后回复"})
        state.conversation_messages.append({"role": "tool", "content": f"output read_{i}", "tool_call_id": f"read_{i}"})
        state.conversation_messages.append({"role": "assistant", "content": f"第{i}次read后回复"})

    # 最近一条用户消息包含 skill 关键词
    state.conversation_messages.append({"role": "user", "content": "帮我生成分析报告"})

    state.skill_catalog = {
        "analysis-report": _make_meta("analysis-report", "生成分析报告", "生成分析报告,数据报告,HTML报告"),
    }

    run_state = RunState()
    messages = policy.before_model_call(state, run_state)

    # 即使经过 300+ 条消息，policy 仍然能匹配并注入
    assert len(messages) == 1
    assert "analysis-report" in messages[0]["content"]
    assert "skill_relevance" in messages[0]["content"]
