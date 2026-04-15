"""测试 _parse_tool_calls 简化：只接受 ToolCall 实例和归一 dict。"""
import pytest
from core.query.loop import _parse_tool_calls
from core.tools.runtime import ToolCall


class TestParseNormalizedDict:
    def test_parses_normalized_tool_call_dict(self):
        raw = [
            {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
        ]
        calls = _parse_tool_calls(raw)
        assert len(calls) == 1
        assert calls[0].name == "bash"
        assert calls[0].call_id == "tu_1"
        assert calls[0].args == {"command": "pwd"}
        assert calls[0].idx == 0

    def test_parses_multiple_dicts(self):
        raw = [
            {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
            {"id": "tu_2", "name": "read_file", "args": {"path": "test.py"}},
        ]
        calls = _parse_tool_calls(raw)
        assert len(calls) == 2
        assert calls[1].name == "read_file"
        assert calls[1].idx == 1

    def test_handles_missing_args_gracefully(self):
        raw = [{"id": "tu_1", "name": "bash"}]
        calls = _parse_tool_calls(raw)
        assert calls[0].args == {}

    def test_handles_non_dict_args(self):
        raw = [{"id": "tu_1", "name": "bash", "args": "not a dict"}]
        calls = _parse_tool_calls(raw)
        assert calls[0].args == {}


class TestParseToolCallInstance:
    def test_passes_through_toolcall_instances(self):
        tc = ToolCall(idx=0, name="bash", call_id="tu_1", args={"command": "pwd"})
        calls = _parse_tool_calls([tc])
        assert len(calls) == 1
        assert calls[0] is tc

    def test_generates_fallback_call_id(self):
        raw = [{"name": "bash", "args": {}}]
        calls = _parse_tool_calls(raw)
        assert calls[0].call_id.startswith("call_")
