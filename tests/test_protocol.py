"""测试消息规范化层：Anthropic 协议转换。"""
import pytest
from core.llm.protocol import normalize_messages


class TestSystemExtraction:
    def test_extracts_system_to_separate_return(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = normalize_messages(messages)
        assert system == "You are helpful."
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_merges_multiple_system_messages(self):
        messages = [
            {"role": "system", "content": "Part 1"},
            {"role": "system", "content": "Part 2"},
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = normalize_messages(messages)
        assert system == "Part 1\n\nPart 2"
        assert len(msgs) == 1

    def test_no_system_returns_empty_string(self):
        messages = [
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = normalize_messages(messages)
        assert system == ""

    def test_empty_system_message_skipped(self):
        messages = [
            {"role": "system", "content": ""},
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = normalize_messages(messages)
        assert system == ""


class TestAssistantToolCalls:
    def test_converts_tool_calls_to_tool_use_blocks(self):
        messages = [
            {"role": "user", "content": "Run pwd"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
                ],
            },
        ]
        system, msgs = normalize_messages(messages)
        assert len(msgs) == 3
        assistant_msg = msgs[1]
        assert assistant_msg["role"] == "assistant"
        assert isinstance(assistant_msg["content"], list)
        assert assistant_msg["content"][0] == {
            "type": "tool_use",
            "id": "tu_1",
            "name": "bash",
            "input": {"command": "pwd"},
        }
        assert msgs[2] == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "(cancelled)",
                },
            ],
        }

    def test_assistant_with_text_and_tool_calls(self):
        messages = [
            {"role": "user", "content": "Do it"},
            {
                "role": "assistant",
                "content": "Let me check",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "ls"}},
                ],
            },
        ]
        system, msgs = normalize_messages(messages)
        content = msgs[1]["content"]
        assert len(content) == 2
        assert content[0] == {"type": "text", "text": "Let me check"}
        assert content[1]["type"] == "tool_use"


class TestToolResultConversion:
    def test_converts_tool_messages_to_user_tool_result(self):
        messages = [
            {"role": "user", "content": "Run pwd"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tu_1", "name": "bash", "args": {"command": "pwd"}}],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "/home/user"},
        ]
        system, msgs = normalize_messages(messages)
        # Last message should be user with tool_result block
        last = msgs[-1]
        assert last["role"] == "user"
        assert isinstance(last["content"], list)
        assert last["content"][0] == {
            "type": "tool_result",
            "tool_use_id": "tu_1",
            "content": "/home/user",
        }

    def test_merges_consecutive_tool_messages(self):
        messages = [
            {"role": "user", "content": "Check"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
                    {"id": "tu_2", "name": "bash", "args": {"command": "ls"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "/home"},
            {"role": "tool", "tool_call_id": "tu_2", "content": "file1.py"},
        ]
        system, msgs = normalize_messages(messages)
        # Two tool messages should merge into one user message
        last = msgs[-1]
        assert last["role"] == "user"
        assert len(last["content"]) == 2
        assert last["content"][0]["tool_use_id"] == "tu_1"
        assert last["content"][1]["tool_use_id"] == "tu_2"


class TestUnclosedToolCalls:
    def test_inserts_cancelled_placeholder_for_unclosed(self):
        messages = [
            {"role": "user", "content": "Do stuff"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
                    {"id": "tu_2", "name": "bash", "args": {"command": "ls"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "/home"},
        ]
        system, msgs = normalize_messages(messages)
        # tu_2 has no tool result — should get (cancelled) placeholder
        last = msgs[-1]
        assert last["role"] == "user"
        tool_results = [b for b in last["content"] if b["type"] == "tool_result"]
        ids = {tr["tool_use_id"] for tr in tool_results}
        assert "tu_1" in ids
        assert "tu_2" in ids
        tu2_result = next(tr for tr in tool_results if tr["tool_use_id"] == "tu_2")
        assert tu2_result["content"] == "(cancelled)"

    def test_drops_orphaned_tool_result(self):
        messages = [
            {"role": "user", "content": "Do stuff"},
            {"role": "tool", "tool_call_id": "tu_missing", "content": "orphan"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "/home"},
        ]

        system, msgs = normalize_messages(messages)

        assert system == ""
        assert msgs == [
            {"role": "user", "content": "Do stuff"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "bash",
                        "input": {"command": "pwd"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "/home",
                    },
                ],
            },
        ]

    def test_drops_tool_result_for_later_duplicate_tool_use_id(self):
        messages = [
            {"role": "user", "content": "First"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "/first"},
            {"role": "user", "content": "Second"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "ls"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "/second"},
        ]

        system, msgs = normalize_messages(messages)

        assert system == ""
        assert msgs == [
            {"role": "user", "content": "First"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "bash",
                        "input": {"command": "pwd"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "/first",
                    },
                    {"type": "text", "text": "Second"},
                ],
            },
        ]


class TestCompactMetaNormalization:
    @pytest.mark.parametrize(
        ("role", "expected_message"),
        [
            (
                "meta_compact_summary",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "compact marker"}],
                },
            ),
            (
                "meta_compact_boundary",
                {"role": "user", "content": "compact marker"},
            ),
            (
                "meta_runtime_restore",
                {"role": "user", "content": "compact marker"},
            ),
        ],
    )
    def test_compact_meta_roles_normalize_provider_safe(self, role, expected_message):
        messages = [
            {"role": role, "content": "compact marker"},
        ]

        system, msgs = normalize_messages(messages)

        assert system == ""
        assert msgs == [expected_message]


class TestConsecutiveRoleMerge:
    def test_merges_consecutive_user_strings(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "World"},
        ]
        system, msgs = normalize_messages(messages)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello\nWorld"

    def test_no_reasoning_content_leaked(self):
        messages = [
            {"role": "assistant", "content": "Hi", "reasoning_content": "thinking..."},
        ]
        system, msgs = normalize_messages(messages)
        assistant = msgs[0]
        # Anthropic format: content is list of blocks
        assert isinstance(assistant["content"], list)
        text_blocks = [b for b in assistant["content"] if b["type"] == "text"]
        assert len(text_blocks) == 1
        assert text_blocks[0]["text"] == "Hi"
        # No reasoning_content key leaked
        assert "reasoning_content" not in assistant

    def test_does_not_modify_original(self):
        original = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello", "tool_calls": [{"id": "tu_1", "name": "bash", "args": {"command": "pwd"}}]},
            {"role": "tool", "tool_call_id": "tu_1", "content": "/home"},
        ]
        import copy
        snapshot = copy.deepcopy(original)
        normalize_messages(original)
        assert original == snapshot
