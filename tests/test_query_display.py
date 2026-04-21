from __future__ import annotations

from types import SimpleNamespace

from core.llm.response import ModelResponse
from core.query.loop import QueryLoop
from core.query.result import StopReason
from core.session.state import SessionState, TodoItem, TodoState
from core.session.store import SessionStore
from core.session.view_builder import ModelInputView
from core.tools.context import (
    RunUpdate,
    RunUpdateKind,
    SessionUpdate,
    SessionUpdateKind,
    ToolOutcomeStatus,
)
from core.tools.runtime import ToolBatchResult


class FakeRenderer:
    def __init__(self) -> None:
        self.assistant_calls: list[str | None] = []
        self.status_calls: list[str] = []
        self.progress_calls: list[list[TodoItem]] = []
        self.current_todo_calls: list[tuple[TodoItem, int, int]] = []
        self.completion_calls: list[tuple[int, int, float]] = []

    def show_thinking(self, title: str, reasoning: str) -> None:
        return None

    def show_assistant(self, content: str | None) -> None:
        self.assistant_calls.append(content)

    def show_status(self, message: str) -> None:
        self.status_calls.append(message)

    def show_progress(self, items: list[TodoItem]) -> None:
        self.progress_calls.append(items)

    def show_current_todo(self, item: TodoItem, completed: int, total: int) -> None:
        self.current_todo_calls.append((item, completed, total))

    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None:
        self.completion_calls.append((completed, total, elapsed))


class FakeViewBuilder:
    def build(self, state: SessionState, *, run_state=None, prompt_assembler=None, working_dir=".", project_root=None, transcript_char_budget=None) -> ModelInputView:
        return ModelInputView(system="SYSTEM", messages=list(state.conversation_messages), tools=None)


class FakeModelGateway:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)

    def call_once(self, messages, *, system="", tools):
        return self._responses.pop(0)


def _success_batch(*tool_names: str) -> ToolBatchResult:
    return ToolBatchResult(
        messages=[
            {"role": "tool", "tool_call_id": f"tool_{idx}", "content": f"{name} ok"}
            for idx, name in enumerate(tool_names)
        ],
        tool_names=list(tool_names),
        tool_statuses=[ToolOutcomeStatus.SUCCESS for _ in tool_names],
        session_updates=[],
        run_updates=[],
    )


class FakeToolRuntime:
    def __init__(self, batches: list[ToolBatchResult]) -> None:
        self._batches = list(batches)

    def execute_batch(self, tool_calls, *, run_state, apply_session_update, apply_run_update):
        return self._batches.pop(0)


def _apply_batch_updates(batch: ToolBatchResult, run_state, apply_session_update, apply_run_update) -> None:
    for update in batch.run_updates:
        apply_run_update(run_state, update)
    for update in batch.session_updates:
        apply_session_update(update)


class FakePolicyRunner:
    def before_model_call(self, session_state, state):
        return []

    def after_tool_batch(self, session_state, state, batch):
        return []

    def should_stop(self, session_state, state):
        return None


class FakeRecovery:
    def handle(self, model_resp, state):
        return SimpleNamespace(should_continue=False, follow_up_messages=[])


def test_query_loop_shows_assistant_update_for_tool_turn() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()
    gateway = FakeModelGateway(
        responses=[
            ModelResponse(
                content="先读取仓库结构，然后继续。",
                tool_calls=[{"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}}],
                finish_reason="tool_use",
            ),
            ModelResponse(content="完成", finish_reason="end_turn"),
        ]
    )
    runtime = FakeToolRuntime([_success_batch("read_file")])

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=runtime,
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.assistant_calls == ["先读取仓库结构，然后继续。"]
    assert renderer.status_calls == []


def test_query_loop_shows_ui_only_fallback_for_empty_tool_turn() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()
    gateway = FakeModelGateway(
        responses=[
            ModelResponse(
                content="  ",
                tool_calls=[
                    {"id": "toolu_skill", "name": "skill", "args": {"skill": "analysis-report"}},
                    {"id": "toolu_todo", "name": "todo", "args": {"items": []}},
                    {"id": "toolu_read", "name": "read_file", "args": {"path": "README.md"}},
                ],
                finish_reason="tool_use",
            ),
            ModelResponse(content="完成", finish_reason="end_turn"),
        ]
    )
    runtime = FakeToolRuntime([_success_batch("skill", "todo", "read_file")])

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=runtime,
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    fallback = "先加载 analysis-report skill；然后更新计划；然后读取 README.md。"

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.assistant_calls == []
    assert renderer.status_calls == [fallback]
    assert all(
        message.get("content") != fallback
        for message in session_state.conversation_messages
        if message.get("role") == "assistant"
    )


def test_query_loop_composes_fallback_for_three_tools() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()
    gateway = FakeModelGateway(
        responses=[
            ModelResponse(
                content="",
                tool_calls=[
                    {"id": "toolu_1", "name": "bash", "args": {"command": "ls"}},
                    {"id": "toolu_2", "name": "find", "args": {"pattern": "QueryLoop"}},
                    {"id": "toolu_3", "name": "write_file", "args": {"path": "tmp.txt", "content": "ok"}},
                ],
                finish_reason="tool_use",
            ),
            ModelResponse(content="完成", finish_reason="end_turn"),
        ]
    )
    runtime = FakeToolRuntime([_success_batch("bash", "find", "write_file")])

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=runtime,
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.status_calls == ["先执行: ls；然后搜索: QueryLoop；然后写入 tmp.txt。"]


def test_query_loop_composes_fallback_across_skill_and_following_tools() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()
    gateway = FakeModelGateway(
        responses=[
            ModelResponse(
                content="",
                tool_calls=[
                    {"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}},
                    {"id": "toolu_2", "name": "skill", "args": {"skill": "analysis-report"}},
                    {"id": "toolu_3", "name": "todo", "args": {"items": []}},
                ],
                finish_reason="tool_use",
            ),
            ModelResponse(content="完成", finish_reason="end_turn"),
        ]
    )
    runtime = FakeToolRuntime([_success_batch("read_file", "skill", "todo")])

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=runtime,
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.status_calls == ["先读取 README.md；然后加载 analysis-report skill；然后更新计划。"]


def test_query_loop_composes_fallback_for_single_tool() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()
    gateway = FakeModelGateway(
        responses=[
            ModelResponse(
                content="",
                tool_calls=[
                    {"id": "toolu_1", "name": "todo", "args": {"items": []}},
                ],
                finish_reason="tool_use",
            ),
            ModelResponse(content="完成", finish_reason="end_turn"),
        ]
    )
    runtime = FakeToolRuntime([_success_batch("todo")])

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=runtime,
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.status_calls == ["先更新计划。"]


def test_query_loop_renders_full_todo_plan_once_then_current_focus() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()

    class TodoWritingRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def execute_batch(self, tool_calls, *, run_state, apply_session_update, apply_run_update):
            self.calls += 1
            items = [
                TodoItem(
                    content="读取并解析 CSV 数据",
                    active_form="读取并解析 CSV 数据",
                    status="in_progress",
                    workflow_ref="1",
                ),
                TodoItem(
                    content="进行信息提取与模式发现",
                    active_form="进行信息提取与模式发现",
                    status="pending",
                    workflow_ref="2",
                ),
            ]
            batch = ToolBatchResult(
                messages=[{"role": "tool", "tool_call_id": "tool_0", "content": "todo ok"}],
                tool_names=["todo"],
                tool_statuses=[ToolOutcomeStatus.SUCCESS],
                session_updates=[
                    SessionUpdate(
                        kind=SessionUpdateKind.SET_TODO_ITEMS,
                        payload={"items": items, "last_write_turn": self.calls},
                    )
                ],
                run_updates=[RunUpdate(kind=RunUpdateKind.RESET_TODO_TURN_COUNTER, payload={})],
            )
            _apply_batch_updates(batch, run_state, apply_session_update, apply_run_update)
            return batch

    gateway = FakeModelGateway(
        responses=[
            ModelResponse(
                content="先建立执行计划。",
                tool_calls=[{"id": "toolu_todo_1", "name": "todo", "args": {"items": []}}],
                finish_reason="tool_use",
            ),
            ModelResponse(
                content="继续按计划推进。",
                tool_calls=[{"id": "toolu_todo_2", "name": "todo", "args": {"items": []}}],
                finish_reason="tool_use",
            ),
            ModelResponse(content="完成", finish_reason="end_turn"),
        ]
    )

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=TodoWritingRuntime(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert len(renderer.progress_calls) == 1
    assert len(renderer.current_todo_calls) == 1
    current_item, completed, total = renderer.current_todo_calls[0]
    assert current_item.content == "读取并解析 CSV 数据"
    assert completed == 0
    assert total == 2


def test_query_loop_renders_completion_summary_when_todo_plan_clears() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()

    class CompletingTodoRuntime:
        def execute_batch(self, tool_calls, *, run_state, apply_session_update, apply_run_update):
            items = [
                TodoItem(
                    content="验证报告完整性",
                    active_form="验证报告完整性",
                    status="completed",
                    workflow_ref="4",
                )
            ]
            batch = ToolBatchResult(
                messages=[{"role": "tool", "tool_call_id": "tool_0", "content": "todo ok"}],
                tool_names=["todo"],
                tool_statuses=[ToolOutcomeStatus.SUCCESS],
                session_updates=[
                    SessionUpdate(
                        kind=SessionUpdateKind.SET_TODO_ITEMS,
                        payload={"items": items, "last_write_turn": 1},
                    )
                ],
                run_updates=[RunUpdate(kind=RunUpdateKind.RESET_TODO_TURN_COUNTER, payload={})],
            )
            _apply_batch_updates(batch, run_state, apply_session_update, apply_run_update)
            return batch

    gateway = FakeModelGateway(
        responses=[
            ModelResponse(
                content="收尾并完成计划。",
                tool_calls=[{"id": "toolu_todo_done", "name": "todo", "args": {"items": []}}],
                finish_reason="tool_use",
            ),
            ModelResponse(content="完成", finish_reason="end_turn"),
        ]
    )

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=CompletingTodoRuntime(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert len(renderer.completion_calls) == 1
    completed, total, elapsed = renderer.completion_calls[0]
    assert completed == 1
    assert total == 1
    assert elapsed >= 0.0


def test_query_loop_renders_full_plan_again_after_clear_without_completion_snapshot() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()

    plan_items = [
        TodoItem(
            content="读取并解析 CSV 数据",
            active_form="读取并解析 CSV 数据",
            status="in_progress",
            workflow_ref="1",
        ),
        TodoItem(
            content="进行信息提取与模式发现",
            active_form="进行信息提取与模式发现",
            status="pending",
            workflow_ref="2",
        ),
    ]

    class ClearingTodoRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def execute_batch(self, tool_calls, *, run_state, apply_session_update, apply_run_update):
            self.calls += 1
            if self.calls == 2:
                items: list[TodoItem] = []
            else:
                items = [
                    TodoItem(
                        content=item.content,
                        active_form=item.active_form,
                        status=item.status,
                        workflow_ref=item.workflow_ref,
                    )
                    for item in plan_items
                ]
            batch = ToolBatchResult(
                messages=[{"role": "tool", "tool_call_id": "tool_0", "content": "todo ok"}],
                tool_names=["todo"],
                tool_statuses=[ToolOutcomeStatus.SUCCESS],
                session_updates=[
                    SessionUpdate(
                        kind=SessionUpdateKind.SET_TODO_ITEMS,
                        payload={"items": items, "last_write_turn": self.calls},
                    )
                ],
                run_updates=[RunUpdate(kind=RunUpdateKind.RESET_TODO_TURN_COUNTER, payload={})],
            )
            _apply_batch_updates(batch, run_state, apply_session_update, apply_run_update)
            return batch

    gateway = FakeModelGateway(
        responses=[
            ModelResponse(
                content="先建立执行计划。",
                tool_calls=[{"id": "toolu_todo_1", "name": "todo", "args": {"items": []}}],
                finish_reason="tool_use",
            ),
            ModelResponse(
                content="清空当前计划。",
                tool_calls=[{"id": "toolu_todo_2", "name": "todo", "args": {"items": []}}],
                finish_reason="tool_use",
            ),
            ModelResponse(
                content="重新建立同一份计划。",
                tool_calls=[{"id": "toolu_todo_3", "name": "todo", "args": {"items": []}}],
                finish_reason="tool_use",
            ),
            ModelResponse(content="完成", finish_reason="end_turn"),
        ]
    )

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=ClearingTodoRuntime(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert len(renderer.progress_calls) == 2
    assert renderer.current_todo_calls == []
