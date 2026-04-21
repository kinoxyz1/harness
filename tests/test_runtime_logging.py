from __future__ import annotations

from rich.console import Console

from core.query.reducers import apply_run_update, apply_session_update
from core.query.state import RunState
from core.session.state import SessionState, TodoItem
from core.shared.run_options import RunDisplayOptions
from core.tools import runtime as runtime_mod
from core.tools.context import ToolInvocationOutcome, ToolUseContext, make_tool_message
from core.tools.runtime import ToolCall, ToolExecutorRuntime
from core.ui.renderer import RichRenderer


class FakeRegistry:
    def is_readonly(self, name: str) -> bool:
        return True

    def execute(self, name: str, args: dict, context: ToolUseContext) -> ToolInvocationOutcome:
        return ToolInvocationOutcome(
            messages=[make_tool_message(context, "\n".join(f"{i}\tline {i}" for i in range(1, 13)))],
        )


class FakeRegistryReturningTodoSuccess:
    def is_readonly(self, name: str) -> bool:
        return False

    def execute(self, name: str, args: dict, context: ToolUseContext) -> ToolInvocationOutcome:
        return ToolInvocationOutcome(messages=[make_tool_message(context, "Todo plan updated successfully.")])


class FakeWriteRegistry:
    def is_readonly(self, name: str) -> bool:
        return False

    def execute(self, name: str, args: dict, context: ToolUseContext) -> ToolInvocationOutcome:
        return ToolInvocationOutcome(messages=[make_tool_message(context, "write ok")])


def _execute_batch(runtime: ToolExecutorRuntime, calls: list[ToolCall]):
    session_state = SessionState(conversation_messages=[])
    return runtime.execute_batch(
        calls,
        run_state=RunState(),
        apply_session_update=lambda update: apply_session_update(session_state, update),
        apply_run_update=apply_run_update,
    )


class FakeRenderer:
    def __init__(self) -> None:
        self.tool_calls: list[tuple[str, dict]] = []
        self.tool_results: list[tuple[str, str]] = []
        self.status_calls: list[str] = []
    def show_tool_call(self, name: str, args: dict) -> None:
        self.tool_calls.append((name, args))

    def show_tool_result(self, name: str, output: str) -> None:
        self.tool_results.append((name, output))

    def show_status(self, message: str) -> None:
        self.status_calls.append(message)

def test_runtime_emits_tool_call_and_result_to_renderer() -> None:
    renderer = FakeRenderer()
    runtime = ToolExecutorRuntime(
        FakeRegistry(),
        ToolUseContext(working_dir=".", max_turns=5),
        renderer=renderer,
    )

    _execute_batch(runtime, [
        ToolCall(idx=0, name="read_file", call_id="call_1", args={"path": "README.md"})
    ])

    assert renderer.tool_calls == [("read_file", {"path": "README.md"})]
    assert len(renderer.tool_results) == 1
    assert renderer.tool_results[0][0] == "read_file"
    assert "1\tline 1" in renderer.tool_results[0][1]


def test_runtime_compact_mode_hides_internal_trace(capsys) -> None:
    renderer = FakeRenderer()
    runtime = ToolExecutorRuntime(
        FakeRegistry(),
        ToolUseContext(working_dir=".", max_turns=5),
        display=RunDisplayOptions(),
        renderer=renderer,
    )

    _execute_batch(runtime, [
        ToolCall(idx=0, name="read_file", call_id="call_1", args={"path": "README.md"})
    ])

    captured = capsys.readouterr()

    assert "[Runtime]" not in captured.out
    assert renderer.tool_calls == [("read_file", {"path": "README.md"})]


def test_runtime_debug_mode_keeps_internal_trace(capsys) -> None:
    renderer = FakeRenderer()
    runtime = ToolExecutorRuntime(
        FakeRegistry(),
        ToolUseContext(working_dir=".", max_turns=5),
        display=RunDisplayOptions(runtime_trace="debug"),
        renderer=renderer,
    )

    _execute_batch(runtime, [
        ToolCall(idx=0, name="read_file", call_id="call_1", args={"path": "README.md"})
    ])

    captured = capsys.readouterr()

    assert "[Runtime] 收到 1 个 tool_call" in captured.out


def test_runtime_compact_mode_reports_long_running_status_once(monkeypatch) -> None:
    class FakeThread:
        def __init__(self, *, target) -> None:
            self._target = target
            self._polls = 0

        def start(self) -> None:
            self._target()

        def join(self, timeout=None) -> None:
            self._polls += 1

        def is_alive(self) -> bool:
            return self._polls < 3

    time_values = [100.0, 100.0, 101.0, 102.0, 102.0, 102.5, 102.5]
    time_index = {"value": 0}

    def fake_time() -> float:
        idx = time_index["value"]
        time_index["value"] += 1
        return time_values[min(idx, len(time_values) - 1)]

    monkeypatch.setattr(runtime_mod.threading, "Thread", FakeThread)
    monkeypatch.setattr(runtime_mod.time, "time", fake_time)

    renderer = FakeRenderer()
    runtime = ToolExecutorRuntime(
        FakeWriteRegistry(),
        ToolUseContext(working_dir=".", max_turns=5),
        display=RunDisplayOptions(),
        renderer=renderer,
    )

    _execute_batch(runtime, [
        ToolCall(idx=0, name="bash", call_id="call_1", args={"command": "echo ok"})
    ])

    assert renderer.status_calls == ["bash 执行中... 2s"]


def test_runtime_compact_mode_skips_generic_todo_events_after_query_loop_migration(tmp_path) -> None:
    renderer = FakeRenderer()
    context = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    context.bind_runtime(session_state=SessionState(conversation_messages=[]))
    runtime = ToolExecutorRuntime(
        FakeRegistryReturningTodoSuccess(),
        context,
        display=RunDisplayOptions(),
        renderer=renderer,
    )

    _execute_batch(runtime, [ToolCall(idx=0, name="todo", call_id="toolu_todo", args={"items": []})])

    assert renderer.tool_calls == []
    assert renderer.tool_results == []


def test_runtime_debug_mode_keeps_generic_todo_events_after_query_loop_migration(tmp_path) -> None:
    renderer = FakeRenderer()
    context = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    context.bind_runtime(session_state=SessionState(conversation_messages=[]))
    runtime = ToolExecutorRuntime(
        FakeRegistryReturningTodoSuccess(),
        context,
        display=RunDisplayOptions(runtime_trace="debug"),
        renderer=renderer,
    )

    _execute_batch(runtime, [ToolCall(idx=0, name="todo", call_id="toolu_todo", args={"items": []})])

    assert renderer.tool_calls == [("todo", {"items": []})]
    assert renderer.tool_results == [("todo", "Todo plan updated successfully.")]


def test_renderer_prefers_active_form_for_in_progress_items() -> None:
    console = Console(record=True, width=120)
    renderer = RichRenderer(console)

    renderer.show_progress([
        TodoItem(
            content="Cross-check findings",
            active_form="Cross-checking findings",
            status="in_progress",
            workflow_ref="2.5",
        )
    ])

    output = console.export_text()

    assert "Cross-checking findings" in output
    assert "Cross-check findings" not in output


def test_renderer_formats_human_friendly_tool_labels() -> None:
    console = Console(record=True, width=120)
    renderer = RichRenderer(console)

    renderer.show_tool_call("skill", {"skill": "analysis-report"})
    renderer.show_tool_call("read_file", {"path": "/tmp/data/TEST_DATA.csv"})
    renderer.show_tool_call("bash", {"description": "分析 CSV 结构", "command": "python inspect.py"})
    renderer.show_tool_call("find", {"pattern": "**/*.py"})

    output = console.export_text()

    assert "Skill(analysis-report)" in output
    assert "Read(TEST_DATA.csv)" in output
    assert "Bash(分析 CSV 结构)" in output
    assert "Find(**/*.py)" in output


def test_renderer_summarizes_skill_and_read_results_compactly() -> None:
    console = Console(record=True, width=120)
    renderer = RichRenderer(console)

    renderer.show_tool_result(
        "skill",
        "Skill loaded: analysis-report. Re-evaluate your next action using the injected skill guidance.",
    )
    renderer.show_tool_result("read_file", "1\talpha\n2\tbeta\n3\tgamma")
    renderer.show_tool_result("find", "core/ui/renderer.py\ntests/test_runtime_logging.py")

    output = console.export_text()

    assert "已加载 skill，等待重新规划" in output
    assert "已读取文件内容，预览 3 行" in output
    assert "已找到 2 个匹配文件" in output


def test_renderer_marks_runtime_truncation_in_read_file_summary() -> None:
    console = Console(record=True, width=120)
    renderer = RichRenderer(console)

    renderer.show_tool_result(
        "read_file",
        "1\talpha\n2\tbeta\n\n... (输出已截断，原始 50000 字符，显示前 30000 字符)",
    )

    output = console.export_text()

    assert "runtime 截断" in output
    assert "不是文件只有这些内容" in output


def test_renderer_marks_tool_managed_read_file_paging_in_summary() -> None:
    console = Console(record=True, width=120)
    renderer = RichRenderer(console)

    renderer.show_tool_result(
        "read_file",
        "1\talpha\n2\tbeta\n\n(文件较大，已显示第 1-2 行，共 725 行；继续读取请使用 offset=3)",
    )

    output = console.export_text()

    assert "已读取文件内容，预览 2 行" in output
    assert "继续用 offset" in output


def test_renderer_marks_runtime_truncation_in_generic_preview() -> None:
    console = Console(record=True, width=120)
    renderer = RichRenderer(console)

    renderer.show_tool_result(
        "bash",
        "header\nvalue\n\n... (输出已截断，原始 50000 字符，显示前 30000 字符)",
    )

    output = console.export_text()

    assert "runtime 截断" in output
    assert "不是文件只有这些内容" in output


def test_renderer_renders_bracket_labels_and_results_literally() -> None:
    console = Console(record=True, width=120)
    renderer = RichRenderer(console)

    renderer.show_tool_call("find", {"pattern": "[ab]*.py"})
    renderer.show_tool_call("bash", {"description": "[red]oops"})
    renderer.show_tool_result("bash", "[red]oops[/red]\nFind([ab]*.py)")

    output = console.export_text()

    assert "Find([ab]*.py)" in output
    assert "Bash([red]oops)" in output
    assert "[red]oops[/red]" in output
    assert "Find([ab]*.py)" in output


def test_renderer_result_summary_falls_back_to_preview_for_non_matching_output() -> None:
    console = Console(record=True, width=120)
    renderer = RichRenderer(console)

    renderer.show_tool_result("skill", "Skill loading: analysis-report")
    renderer.show_tool_result("read_file", "alpha\nbeta")
    renderer.show_tool_result("find", "未找到匹配 '**/*.py' 的文件")

    output = console.export_text()

    assert "Skill loading: analysis-report" in output
    assert "alpha" in output
    assert "beta" in output
    assert "未找到匹配 '**/*.py' 的文件" in output
    assert "已加载 skill，等待重新规划" not in output
    assert "已读取文件内容，预览" not in output


def test_renderer_hides_unknown_completion_elapsed() -> None:
    console = Console(record=True, width=120)
    renderer = RichRenderer(console)

    renderer.show_completion_summary(completed=2, total=2, elapsed=0.0)

    output = console.export_text()

    assert "完成: 2/2 个任务" in output
    assert "耗时:" not in output
