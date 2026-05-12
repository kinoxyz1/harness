from core.query.reducers import apply_run_update, apply_session_update
from core.query.state import RunState
from core.session.state import SessionState
from core.tools import ToolRegistry
from core.tools.context import ToolInvocationOutcome, ToolUseContext, make_tool_message
from core.tools.runtime import ToolCall, ToolExecutorRuntime


class RecorderRenderer:
    def __init__(self) -> None:
        self.events = []

    def show_tool_call(self, name, args):
        self.events.append(("call", name))

    def show_tool_result(self, name, output):
        self.events.append(("result", name, output))

    def show_status(self, message):
        self.events.append(("status", message))


def _tool(name, *, readonly, content):
    class Module:
        SCHEMA = {"name": name, "input_schema": {"type": "object", "properties": {}}}
        READONLY = readonly
        ANNOTATIONS = {"readonly": readonly, "destructive": False, "idempotent": True, "concurrency_safe": readonly}

        @staticmethod
        def handle(args, context):
            return ToolInvocationOutcome(messages=[make_tool_message(context, content)])

    return Module


def test_parallel_tools_render_all_calls_before_any_result(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_tool("find_a", readonly=True, content="A"))
    registry.register(_tool("find_b", readonly=True, content="B"))
    context = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    context.bind_runtime(session_state=SessionState(conversation_messages=[]), skill_registry=None)
    renderer = RecorderRenderer()
    runtime = ToolExecutorRuntime(registry, context, renderer=renderer)

    runtime.execute_batch(
        [
            ToolCall(idx=0, name="find_a", call_id="call_0", args={}),
            ToolCall(idx=1, name="find_b", call_id="call_1", args={}),
        ],
        run_state=RunState(),
        apply_session_update=lambda update: apply_session_update(context.session_state, update),
        apply_run_update=apply_run_update,
    )

    assert renderer.events[0] == ("call", "find_a")
    assert renderer.events[1] == ("call", "find_b")
    assert {renderer.events[2][1], renderer.events[3][1]} == {"find_a", "find_b"}


def test_serial_tool_renders_call_then_result_before_next_tool(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_tool("write_a", readonly=False, content="write ok"))
    registry.register(_tool("write_b", readonly=False, content="write ok"))
    context = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    context.bind_runtime(session_state=SessionState(conversation_messages=[]), skill_registry=None)
    renderer = RecorderRenderer()
    runtime = ToolExecutorRuntime(registry, context, renderer=renderer)

    runtime.execute_batch(
        [
            ToolCall(idx=0, name="write_a", call_id="call_0", args={}),
            ToolCall(idx=1, name="write_b", call_id="call_1", args={}),
        ],
        run_state=RunState(),
        apply_session_update=lambda update: apply_session_update(context.session_state, update),
        apply_run_update=apply_run_update,
    )

    assert renderer.events[:4] == [
        ("call", "write_a"),
        ("result", "write_a", "write ok"),
        ("call", "write_b"),
        ("result", "write_b", "write ok"),
    ]
