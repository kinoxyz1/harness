from __future__ import annotations

from rich.console import Console

from core.session.state import SessionState, TodoItem, TodoState
from core.tools.builtin import todo as todo_mod
from core.tools.context import ToolResult, ToolUseContext
from core.tools.runtime import ToolCall, ToolExecutorRuntime
from core.ui.renderer import RichRenderer


class FakeRegistry:
    def is_readonly(self, name: str) -> bool:
        return True

    def execute(self, name: str, args: dict, context: ToolUseContext) -> ToolResult:
        return ToolResult(
            output="\n".join(f"{i}\tline {i}" for i in range(1, 13)),
            success=True,
        )


class FakeRegistryReturningTodoSuccess:
    def is_readonly(self, name: str) -> bool:
        return False

    def execute(self, name: str, args: dict, context: ToolUseContext) -> ToolResult:
        return ToolResult(output="Todo plan updated successfully.", success=True)


class FakeRenderer:
    def __init__(self) -> None:
        self.tool_calls: list[tuple[str, dict]] = []
        self.tool_results: list[tuple[str, str]] = []
        self.progress_calls: list[list[TodoItem]] = []
        self.completion_calls: list[tuple[int, int]] = []

    def show_tool_call(self, name: str, args: dict) -> None:
        self.tool_calls.append((name, args))

    def show_tool_result(self, name: str, output: str) -> None:
        self.tool_results.append((name, output))

    def show_progress(self, items: list[TodoItem]) -> None:
        self.progress_calls.append(items)

    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None:
        self.completion_calls.append((completed, total))


def test_runtime_emits_tool_call_and_result_to_renderer() -> None:
    renderer = FakeRenderer()
    runtime = ToolExecutorRuntime(
        FakeRegistry(),
        ToolUseContext(working_dir=".", max_turns=5),
        renderer=renderer,
    )

    runtime.execute_batch([
        ToolCall(idx=0, name="read_file", call_id="call_1", args={"path": "README.md"})
    ])

    assert renderer.tool_calls == [("read_file", {"path": "README.md"})]
    assert len(renderer.tool_results) == 1
    assert renderer.tool_results[0][0] == "read_file"
    assert "1\tline 1" in renderer.tool_results[0][1]


def test_runtime_renders_active_form_after_todo_write(tmp_path) -> None:
    renderer = FakeRenderer()
    context = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    todo_mod._latest_todo_state = TodoState(  # type: ignore[attr-defined]
        items=[
            TodoItem(
                content="Legacy global item",
                active_form="Legacy global item",
                status="in_progress",
            )
        ]
    )
    context.bind_runtime(session_state=SessionState(
        conversation_messages=[],
        todo_state=TodoState(
            items=[
                TodoItem(
                    content="Cross-check findings",
                    active_form="Cross-checking findings",
                    status="in_progress",
                    workflow_ref="2.5",
                )
            ]
        ),
    ))
    runtime = ToolExecutorRuntime(FakeRegistryReturningTodoSuccess(), context, renderer=renderer)

    runtime.execute_batch([ToolCall(idx=0, name="todo", call_id="toolu_todo", args={"items": []})])

    assert renderer.progress_calls[0][0].active_form == "Cross-checking findings"


def test_renderer_shows_completion_summary_when_active_plan_clears(tmp_path) -> None:
    renderer = FakeRenderer()
    context = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    todo_mod._latest_todo_state = TodoState(  # type: ignore[attr-defined]
        items=[
            TodoItem(
                content="Legacy global item",
                active_form="Legacy global item",
                status="in_progress",
            )
        ]
    )
    context.bind_runtime(session_state=SessionState(
        conversation_messages=[],
        todo_state=TodoState(
            items=[],
            last_completed_items=[
                TodoItem(
                    content="Verify report completeness",
                    active_form="Verifying report completeness",
                    status="completed",
                    workflow_ref="4",
                )
            ],
        ),
    ))
    runtime = ToolExecutorRuntime(FakeRegistryReturningTodoSuccess(), context, renderer=renderer)

    runtime.execute_batch([ToolCall(idx=0, name="todo", call_id="toolu_todo", args={"items": []})])

    assert renderer.completion_calls == [(1, 1)]


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
