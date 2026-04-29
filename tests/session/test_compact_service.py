from core.session.compact_service import (
    MICROCOMPACT_PLACEHOLDER,
    SUMMARY_SYSTEM_PROMPT,
    TOOL_RESULT_PLACEHOLDER,
    apply_time_based_microcompact,
    apply_tool_result_budget,
    build_runtime_restore_messages,
    summarize_and_compact,
)
from core.llm.protocol import normalize_messages
from core.session.state import SessionState
from core.session.state import TodoItem
from core.skills.models import InvokedSkillRecord
from core.tools.context import FileState


class FakeSummaryResponse:
    def __init__(self, content: str):
        self.content = content


class FakeSummaryGateway:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.last_call = None

    def call_once(self, messages, *, system="", tools=None, request_options=None):
        self.last_call = {
            "messages": messages,
            "system": system,
            "tools": tools,
            "request_options": request_options,
        }
        return FakeSummaryResponse(self.response_text)


def test_apply_tool_result_budget_reuses_stable_placeholder() -> None:
    state = SessionState(conversation_messages=[])
    messages = [
        {"role": "user", "content": "keep"},
        {"role": "tool", "tool_call_id": "toolu_big", "content": "x" * 400},
    ]

    compacted = apply_tool_result_budget(
        messages,
        state=state,
        per_message_token_limit=10,
    )

    assert compacted == [
        {"role": "user", "content": "keep"},
        {"role": "tool", "tool_call_id": "toolu_big", "content": TOOL_RESULT_PLACEHOLDER},
    ]
    assert state.compact_state["tool_result_replacements"] == {
        "toolu_big": TOOL_RESULT_PLACEHOLDER,
    }

    rerun = apply_tool_result_budget(
        [
            {"role": "tool", "tool_call_id": "toolu_big", "content": "short"},
            {"role": "tool", "tool_call_id": "toolu_other", "content": "ok"},
        ],
        state=state,
        per_message_token_limit=1_000,
    )

    assert rerun == [
        {"role": "tool", "tool_call_id": "toolu_big", "content": TOOL_RESULT_PLACEHOLDER},
        {"role": "tool", "tool_call_id": "toolu_other", "content": "ok"},
    ]


def test_apply_time_based_microcompact_compacts_only_old_compactable_results() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_old", "name": "read_file", "args": {"path": "old.txt"}}],
            "_meta": {"created_at": 100.0},
        },
        {
            "role": "tool",
            "tool_call_id": "toolu_old",
            "content": "old content",
            "_meta": {"created_at": 110.0},
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_recent", "name": "find", "args": {"pattern": "needle"}}],
            "_meta": {"created_at": 150.0},
        },
        {
            "role": "tool",
            "tool_call_id": "toolu_recent",
            "content": "recent content",
            "_meta": {"created_at": 190.0},
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_bash", "name": "bash", "args": {"command": "pwd"}}],
            "_meta": {"created_at": 195.0},
        },
        {
            "role": "tool",
            "tool_call_id": "toolu_bash",
            "content": "bash output",
            "_meta": {"created_at": 200.0},
        },
    ]

    compacted = apply_time_based_microcompact(
        messages,
        age_cutoff_seconds=50,
        keep_recent_trajectories=1,
    )

    assert compacted[1]["content"] == MICROCOMPACT_PLACEHOLDER
    assert compacted[3]["content"] == "recent content"
    assert compacted[5]["content"] == "bash output"


def test_apply_time_based_microcompact_ignores_unstamped_messages_when_finding_newest() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_old", "name": "read_file", "args": {"path": "old.txt"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "toolu_old",
            "content": "old content",
            "_meta": {"created_at": 100.0},
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_recent", "name": "find", "args": {"pattern": "needle"}}],
            "_meta": {"created_at": 180.0},
        },
        {
            "role": "tool",
            "tool_call_id": "toolu_recent",
            "content": "recent content",
            "_meta": {"created_at": 200.0},
        },
    ]

    compacted = apply_time_based_microcompact(
        messages,
        age_cutoff_seconds=50,
        keep_recent_trajectories=1,
    )

    assert compacted[1]["content"] == MICROCOMPACT_PLACEHOLDER
    assert compacted[3]["content"] == "recent content"


def test_build_runtime_restore_messages_uses_runtime_state_sources() -> None:
    state = SessionState(conversation_messages=[])
    state.todo_state.items = [
        TodoItem(content="Review compact path", active_form="Review compact path", status="in_progress"),
        TodoItem(content="Add tests", active_form="Add tests", status="pending"),
    ]
    state.invoked_skills["skill-a"] = InvokedSkillRecord(
        skill_id="skill-a",
        skill_path="/skills/skill-a/SKILL.md",
        content_digest="digest-a",
        content="Skill A body",
        invoked_at_turn=2,
    )
    state.invoked_skills["skill-b"] = InvokedSkillRecord(
        skill_id="skill-b",
        skill_path="/skills/skill-b/SKILL.md",
        content_digest="digest-b",
        content="Skill B body",
        invoked_at_turn=5,
    )
    state.read_file_state["/tmp/older.py"] = FileState(content="old", timestamp=10.0)
    state.read_file_state["/tmp/newer.py"] = FileState(
        content="new",
        timestamp=20.0,
        offset=101,
        limit=50,
        total_lines=725,
    )

    messages = build_runtime_restore_messages(state)

    assert [message["role"] for message in messages] == [
        "meta_runtime_restore",
        "meta_runtime_restore",
        "meta_runtime_restore",
        "meta_runtime_restore",
    ]
    assert [message["kind"] for message in messages] == [
        "todo_restore",
        "skills_restore",
        "file_runtime",
        "file_runtime",
    ]
    assert "Review compact path" in messages[0]["content"]
    assert "skill-a" in messages[1]["content"]
    assert "skill-b" in messages[1]["content"]
    assert "/tmp/newer.py" in messages[2]["content"]
    assert "full_read=false" in messages[2]["content"]
    assert "start_line=101" in messages[2]["content"]
    assert "end_line=150" in messages[2]["content"]
    assert "total_lines=725" in messages[2]["content"]
    assert "/tmp/older.py" in messages[3]["content"]
    assert "full_read=true" in messages[3]["content"]


def test_summarize_and_compact_rewrites_transcript_and_restores_runtime() -> None:
    state = SessionState(conversation_messages=[])
    state.todo_state.items = [
        TodoItem(content="Keep runtime truth", active_form="Keep runtime truth", status="in_progress")
    ]
    state.invoked_skills["compact-skill"] = InvokedSkillRecord(
        skill_id="compact-skill",
        skill_path="/skills/compact-skill/SKILL.md",
        content_digest="digest",
        content="Compact skill body",
        invoked_at_turn=3,
    )
    state.read_file_state["/tmp/current.py"] = FileState(
        content="print('x')",
        timestamp=30.0,
        offset=5,
        limit=10,
        total_lines=200,
    )
    gateway = FakeSummaryGateway("Primary Request and Intent: compact the transcript")
    messages = [
        {"role": "user", "content": "m0"},
        {"role": "assistant", "content": "m1"},
        {"role": "user", "content": "m2"},
        {"role": "assistant", "content": "m3"},
    ]

    compacted = summarize_and_compact(
        messages,
        state=state,
        summary_gateway=gateway,
        keep_last_messages=2,
    )

    assert gateway.last_call is not None
    assert compacted[0] == {
        "role": "meta_compact_boundary",
        "kind": "compact_boundary",
        "content": "reason=summary_compact;summarized_messages=2",
    }
    assert compacted[1] == {
        "role": "meta_compact_summary",
        "kind": "compact_summary",
        "content": "Primary Request and Intent: compact the transcript",
    }
    assert compacted[2:4] == [
        {"role": "user", "content": "m2"},
        {"role": "assistant", "content": "m3"},
    ]


def test_summarize_and_compact_preserves_latest_tool_results_through_normalization() -> None:
    state = SessionState(conversation_messages=[])
    gateway = FakeSummaryGateway("Primary Request and Intent: compact the transcript")
    messages = [
        {"role": "user", "content": "m0"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "toolu_old", "name": "read_file", "args": {"path": "old.txt"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_old", "content": "old result"},
        {"role": "user", "content": "m1"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "toolu_latest", "name": "read_file", "args": {"path": "latest.txt"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_latest", "content": "latest result"},
        {"role": "user", "content": "tail"},
    ]

    compacted = summarize_and_compact(
        messages,
        state=state,
        summary_gateway=gateway,
        keep_last_messages=2,
    )
    _, normalized = normalize_messages(compacted)

    assert any(
        block.get("type") == "tool_result"
        and block.get("tool_use_id") == "toolu_latest"
        and block.get("content") == "latest result"
        for message in normalized
        if message["role"] == "user"
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
    )
    assert [message["role"] for message in compacted] == [
        "meta_compact_boundary",
        "meta_compact_summary",
        "assistant",
        "tool",
        "user",
    ]


def test_summarize_and_compact_repeated_pass_keeps_real_working_set_without_duplicate_restore() -> None:
    state = SessionState(conversation_messages=[])
    state.todo_state.items = [
        TodoItem(content="Keep runtime truth", active_form="Keep runtime truth", status="in_progress")
    ]
    state.invoked_skills["compact-skill"] = InvokedSkillRecord(
        skill_id="compact-skill",
        skill_path="/skills/compact-skill/SKILL.md",
        content_digest="digest",
        content="Compact skill body",
        invoked_at_turn=3,
    )
    state.read_file_state["/tmp/current.py"] = FileState(content="print('x')", timestamp=30.0)
    first_gateway = FakeSummaryGateway("Primary Request and Intent: first compact")
    second_gateway = FakeSummaryGateway("Primary Request and Intent: second compact")
    messages = [
        {"role": "user", "content": "older user"},
        {"role": "assistant", "content": "older assistant"},
        {"role": "user", "content": "recent user"},
        {"role": "assistant", "content": "recent assistant"},
    ]

    first_compacted = summarize_and_compact(
        messages,
        state=state,
        summary_gateway=first_gateway,
        keep_last_messages=2,
    )
    second_compacted = summarize_and_compact(
        first_compacted,
        state=state,
        summary_gateway=second_gateway,
        keep_last_messages=2,
    )

    assert second_gateway.last_call is not None
    assert second_gateway.last_call["messages"] == [
        first_compacted[0],
        first_compacted[1],
    ]
    assert [message["role"] for message in second_compacted] == [
        "meta_compact_boundary",
        "meta_compact_summary",
        "user",
        "assistant",
        "meta_runtime_restore",
        "meta_runtime_restore",
        "meta_runtime_restore",
    ]
    assert second_compacted[2:4] == [
        {"role": "user", "content": "recent user"},
        {"role": "assistant", "content": "recent assistant"},
    ]
    assert len([message for message in second_compacted if message.get("kind") == "todo_restore"]) == 1
    assert len([message for message in second_compacted if message.get("kind") == "skills_restore"]) == 1
    assert len([message for message in second_compacted if message.get("kind") == "file_runtime"]) == 1


def test_summarize_and_compact_calls_gateway_with_compact_request_options() -> None:
    state = SessionState(conversation_messages=[])
    gateway = FakeSummaryGateway("Primary Request and Intent: summary")
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "new"},
    ]

    summarize_and_compact(
        messages,
        state=state,
        summary_gateway=gateway,
        keep_last_messages=1,
    )

    assert gateway.last_call is not None
    assert gateway.last_call["messages"] == [{"role": "user", "content": "old"}]
    assert gateway.last_call["system"] == SUMMARY_SYSTEM_PROMPT
    assert gateway.last_call["tools"] is None
    assert gateway.last_call["request_options"].query_source == "compact"
    assert gateway.last_call["request_options"].thinking_mode == "disabled"
    assert gateway.last_call["request_options"].max_output_tokens == 1200
