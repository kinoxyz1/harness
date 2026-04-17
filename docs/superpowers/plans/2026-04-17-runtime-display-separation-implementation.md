# Runtime Display Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不引入第二条控制平面、也不污染 `conversation_messages` 的前提下，把默认终端显示重构为 `Assistant Update + Todo State + Compact Tool Event + Progress/Outcome`，并把内部 runtime trace 收束到 `debug` 模式。

**Architecture:** 这次实现严格复用现有 `QueryLoop -> ToolExecutorRuntime -> Renderer -> SessionState.todo_state` 主链路。`Assistant Update` 继续来自 assistant 文本，由 `QueryLoop` 在带 `tool_calls` 的回合中也负责显示；`Todo State` 的权威数据继续只放在 `SessionState.todo_state`，但“本轮是否已经展示过完整计划”放到 `RunState` 做 run-scoped 快照；普通工具证据与进行中状态继续复用现有 `Renderer` 接口，`ToolExecutorRuntime` 只触发 UI-only 渲染，不写 transcript。默认 `compact` 模式隐藏 `[Runtime] ...` 调度日志，`debug` 模式保留。

**Tech Stack:** Python 3.12、pytest、dataclasses、typing.Literal、现有 `QueryLoop` / `ToolExecutorRuntime` / `RichRenderer` / `SessionState`

---

## File Structure

### New Files

- `tests/test_runtime_display_state.py`
  Responsibility: 锁定 `RunDisplayOptions` 与 `RunState` 的新增 display 字段和默认值。
- `tests/test_query_display.py`
  Responsibility: 覆盖 QueryLoop 的 assistant update 展示、静态 fallback、fallback 不写 transcript、todo 计划视图去重与完成总结。

### Modified Files

- `core/shared/run_options.py`
  Responsibility: 把单一 `quiet` 扩展为 `quiet + runtime_trace`，并锁定 `Literal["compact", "debug"]`。
- `core/query/state.py`
  Responsibility: 增加 run-scoped 的 `last_displayed_todo_items`，用于 todo 计划视图去重。
- `core/query/loop.py`
  Responsibility: 在 tool 回合显示 assistant content；当 content 为空时生成 UI-only fallback；在成功 todo 写入后根据 `RunState` + `SessionState.todo_state` 决定显示完整计划、当前聚焦项或完成总结。
- `core/tools/runtime.py`
  Responsibility: 把调度 trace 收束到 `debug` 模式；compact 模式只触发 tool event / progress；停止 runtime 直接渲染 todo 计划视图；只在 debug 模式保留机械的 `todo` 工具事件。
- `core/ui/renderer.py`
  Responsibility: 把工具调用标签与结果摘要升级为稳定的人类可读规则，避免直接回显原始参数和大段输出预览。
- `core/tools/builtin/skill.py`
  Responsibility: 去掉绕过 display mode 的直接 stdout 输出，让 `skill` 的可见反馈统一走 runtime + renderer。
- `tests/test_runtime_logging.py`
  Responsibility: 覆盖 compact/debug 分层、tool label / result summary，以及 todo 迁移前后的 runtime 级回归；Task 5 会在这里删除过时的 runtime todo 渲染测试并补上新的 debug/compact 规则。
- `tests/test_query_logging.py`
  Responsibility: 保留 reasoning 渲染测试，并补一条“tool 回合 assistant content 也会显示”的轻量回归。
- `tests/session/test_skill_tool.py`
  Responsibility: 覆盖 `skill` tool 不再直接向 stdout 打 `[Skill] ...`。

## Implementation Notes Locked In Before Coding

- `Renderer` protocol 不新增方法；`Progress / Outcome` 继续只复用 `show_status()`、`show_tool_result()`、`show_progress()`、`show_current_todo()`、`show_completion_summary()`。
- `fallback` 必须是静态模板映射，只用于 UI 展示，绝不能写入 `conversation_messages`。
- `todo` 的权威状态仍然只有 `SessionState.todo_state`；`RunState.last_displayed_todo_items` 只是当前 run 内的展示快照。
- `ToolExecutorRuntime` 仍然不负责 transcript 管理；tool event、fallback、progress、runtime trace 都不能回写到会话消息。
- `todo` 在 `compact` 模式下不能继续走 `show_tool_call()/show_tool_result()` 的通用路径，否则会和计划视图双重展示；`debug` 模式可以保留机械事件。
- `core/tools/builtin/skill.py` 当前的 stdout 输出属于显示层泄漏，必须删除；`core/session/commands.py` 的 `/skills` 命令输出不在本次 runtime display separation 范围内。
- Task 3 对 `core/tools/builtin/skill.py` 只能做最小修改：保留 `SCHEMA`、`READONLY`、`ANNOTATIONS`、`handle()` 和 `ToolResult` 结构，只删除 `import sys`、`ref_count/ref_chars` 统计以及 `sys.stdout.write(...)`。
- Task 4 只处理 runtime trace 分层与 compact 进行中状态，不动 todo 渲染路径；Task 5 再把 “删除 runtime todo 渲染 + QueryLoop 接管 + 测试迁移” 作为一个原子提交完成，避免中间态让 todo 在 compact 模式下不可见。

---

### Task 1: 建立 display mode 与 todo 展示快照的基础状态

**Files:**
- Create: `tests/test_runtime_display_state.py`
- Modify: `core/shared/run_options.py`
- Modify: `core/query/state.py`

- [ ] **Step 1: 先写 failing tests，锁定 display primitives 的默认行为**

```python
# tests/test_runtime_display_state.py
from core.query.state import RunState
from core.shared.run_options import RunDisplayOptions


def test_run_display_options_defaults_to_compact_trace() -> None:
    options = RunDisplayOptions()

    assert options.quiet is False
    assert options.runtime_trace == "compact"


def test_run_display_options_accepts_debug_trace() -> None:
    options = RunDisplayOptions(runtime_trace="debug")

    assert options.runtime_trace == "debug"


def test_run_state_starts_without_todo_display_snapshot() -> None:
    state = RunState()

    assert state.last_displayed_todo_items is None
```

- [ ] **Step 2: 运行状态测试，确认当前缺少这些字段**

Run: `pytest tests/test_runtime_display_state.py -q`
Expected:
- `TypeError` 或 `AttributeError`，因为 `RunDisplayOptions` 还没有 `runtime_trace`
- `AttributeError`，因为 `RunState` 还没有 `last_displayed_todo_items`

- [ ] **Step 3: 最小实现 display primitives**

```python
# core/shared/run_options.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RunDisplayOptions:
    """控制一次运行过程中的中间显示。"""

    quiet: bool = False
    runtime_trace: Literal["compact", "debug"] = "compact"
```

```python
# core/query/state.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.session.state import TodoItem


@dataclass(slots=True)
class RunState:
    turn_count: int = 0
    empty_retry_count: int = 0
    stop_reason: str | None = None
    last_model_response: Any | None = None
    tool_calls_executed: int = 0
    files_modified: list[str] = field(default_factory=list)
    usage_delta: dict[str, int] = field(default_factory=dict)
    allowed_tools_override: set[str] | None = None
    model_override: str | None = None
    effort_override: str | None = None
    barrier_reason: str | None = None
    todo_replan_required: bool = False
    todo_replan_reason: str | None = None
    assistant_turns_since_todo: int = 0
    last_displayed_todo_items: list["TodoItem"] | None = None
```

- [ ] **Step 4: 回跑状态测试**

Run: `pytest tests/test_runtime_display_state.py -q`
Expected: `3 passed`

- [ ] **Step 5: 提交 display primitives**

```bash
git add core/shared/run_options.py core/query/state.py tests/test_runtime_display_state.py
git commit -m "feat: add runtime display state primitives"
```

---

### Task 2: 让 QueryLoop 在 tool 回合展示 assistant update，并在缺文案时给 UI-only fallback

**Files:**
- Create: `tests/test_query_display.py`
- Modify: `core/query/loop.py`
- Modify: `tests/test_query_logging.py`

- [ ] **Step 1: 写 failing tests，锁定 assistant update、fallback 和 transcript 边界**

```python
# tests/test_query_display.py
from __future__ import annotations

from types import SimpleNamespace

from core.llm.response import ModelResponse
from core.query.loop import QueryLoop
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageView
from core.tools.runtime import ToolBatchResult


class FakeRenderer:
    def __init__(self) -> None:
        self.assistant_calls: list[str] = []
        self.status_calls: list[str] = []
        self.thinking_calls: list[tuple[str, str]] = []
        self.progress_calls = []
        self.current_todo_calls = []
        self.completion_calls = []

    def show_assistant(self, content: str | None) -> None:
        if content:
            self.assistant_calls.append(content)

    def show_status(self, message: str) -> None:
        self.status_calls.append(message)

    def show_thinking(self, title: str, reasoning: str) -> None:
        self.thinking_calls.append((title, reasoning))

    def show_progress(self, items) -> None:
        self.progress_calls.append(items)

    def show_current_todo(self, item, completed: int, total: int) -> None:
        self.current_todo_calls.append((item, completed, total))

    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None:
        self.completion_calls.append((completed, total, elapsed))


class FakeViewBuilder:
    def build(self, state: SessionState, *, run_state=None) -> MessageView:
        return MessageView(messages=list(state.conversation_messages), tools=None)


class FakeModelGateway:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)

    def call_once(self, messages, *, tools):
        return self._responses.pop(0)


class FakeToolRuntime:
    def __init__(self, batch: ToolBatchResult) -> None:
        self.batch = batch
        self.calls = []

    def execute_batch(self, tool_calls):
        self.calls.append(tool_calls)
        return self.batch


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


def _success_batch(*tool_names: str) -> ToolBatchResult:
    return ToolBatchResult(
        tool_results=[],
        files_modified=[],
        tool_names=list(tool_names),
        tool_successes=[True for _ in tool_names],
        injected_messages=[],
        context_patches=[],
        barrier=None,
    )


def test_query_loop_shows_assistant_update_for_tool_turn() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()
    gateway = FakeModelGateway(
        [
            ModelResponse(
                content="先读取 CSV 的表头和样例。",
                tool_calls=[{"id": "toolu_read", "name": "read_file", "args": {"path": "TEST_DATA.csv"}}],
            ),
            ModelResponse(content="done"),
        ]
    )

    QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=FakeToolRuntime(_success_batch("read_file")),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert renderer.assistant_calls == ["先读取 CSV 的表头和样例。"]
    assert renderer.status_calls == []


def test_query_loop_shows_ui_only_fallback_for_empty_tool_turn() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()
    gateway = FakeModelGateway(
        [
            ModelResponse(
                content="",
                tool_calls=[{"id": "toolu_read", "name": "read_file", "args": {"path": "TEST_DATA.csv"}}],
            ),
            ModelResponse(content="done"),
        ]
    )

    QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=FakeToolRuntime(_success_batch("read_file")),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert renderer.assistant_calls == []
    assert renderer.status_calls == ["先读取文件内容。"]
    assert all(
        message.get("content") != "先读取文件内容。"
        for message in session_state.conversation_messages
    )


def test_query_loop_composes_fallback_for_three_tools() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()
    gateway = FakeModelGateway(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    {"id": "toolu_skill", "name": "skill", "args": {"skill": "analysis-report"}},
                    {"id": "toolu_todo", "name": "todo", "args": {"items": []}},
                    {"id": "toolu_read", "name": "read_file", "args": {"path": "TEST_DATA.csv"}},
                ],
            ),
            ModelResponse(content="done"),
        ]
    )

    QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=gateway,
        tool_runtime=FakeToolRuntime(_success_batch("skill", "todo", "read_file")),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert renderer.status_calls == ["先加载 skill，再重新评估下一步；然后更新当前计划；然后读取文件内容。"]
```

- [ ] **Step 2: 运行 QueryLoop display tests，确认当前行为不满足要求**

Run: `pytest tests/test_query_display.py -q`
Expected:
- tool 回合 assistant content 不会显示，`show_assistant` 断言失败
- 空 content 回合没有 fallback，`show_status` 断言失败
- fallback transcript 边界断言当前没有对应保护

- [ ] **Step 3: 在 QueryLoop 中加入 assistant update 与静态 fallback helper**

```python
# core/query/loop.py
from __future__ import annotations

from core.query.result import QueryResult, StopReason
from core.query.state import RunState
from core.tools.runtime import ToolBatchResult, ToolCall


def _tool_fallback_fragment(name: str) -> str | None:
    mapping = {
        "skill": "先加载 skill，再重新评估下一步。",
        "todo": "先更新当前计划。",
        "read_file": "先读取文件内容。",
        "bash": "先执行命令并查看结果。",
        "find": "先搜索相关文件。",
        "edit_file": "先修改目标文件。",
        "write_file": "先写入目标文件。",
    }
    return mapping.get(name)


def _build_tool_fallback_status(tool_calls: list[ToolCall]) -> str | None:
    fragments = [
        fragment
        for call in tool_calls
        if (fragment := _tool_fallback_fragment(call.name)) is not None
    ]
    if not fragments:
        return None
    if len(fragments) == 1:
        return fragments[0]

    parts = [fragments[0].rstrip("。")]
    for fragment in fragments[1:]:
        normalized = fragment.rstrip("。")
        if normalized.startswith("先"):
            normalized = normalized[1:]
        parts.append(f"然后{normalized}")
    return "；".join(parts) + "。"
```

```python
# core/query/loop.py (inside QueryLoop.run)
            if model_resp.tool_calls:
                parsed_calls = _parse_tool_calls(model_resp.tool_calls)

                if renderer is not None:
                    assistant_text = model_resp.content.strip()
                    if assistant_text:
                        renderer.show_assistant(assistant_text)
                    else:
                        fallback = _build_tool_fallback_status(parsed_calls)
                        if fallback:
                            renderer.show_status(fallback)

                _note_assistant_turn(state, model_resp)
                batch = tool_runtime.execute_batch(parsed_calls)
```

```python
# tests/test_query_logging.py
def test_query_loop_renders_assistant_content_when_tool_calls_are_present() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()

    class ToolCallingGateway:
        def __init__(self) -> None:
            self.calls = 0

        def call_once(self, messages, *, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    content="先读取文件。",
                    tool_calls=[{"id": "toolu_read", "name": "read_file", "args": {"path": "README.md"}}],
                )
            return ModelResponse(content="final answer")

    class FakeToolRuntime:
        def execute_batch(self, tool_calls):
            return ToolBatchResult(
                tool_results=[],
                files_modified=[],
                tool_names=["read_file"],
                tool_successes=[True],
                injected_messages=[],
                context_patches=[],
                barrier=None,
            )

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=object(),
        model_gateway=ToolCallingGateway(),
        tool_runtime=FakeToolRuntime(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.assistant_calls == ["先读取文件。"]
```

- [ ] **Step 4: 回跑 QueryLoop display tests**

Run: `pytest tests/test_query_display.py tests/test_query_logging.py -q`
Expected: PASS

- [ ] **Step 5: 提交 QueryLoop display 主路径**

```bash
git add core/query/loop.py tests/test_query_display.py tests/test_query_logging.py
git commit -m "feat: show assistant updates before tool execution"
```

---

### Task 3: 把 renderer 的工具显示变成人类可读标签，并移除 skill tool 的直接 stdout 泄漏

**Files:**
- Modify: `core/ui/renderer.py`
- Modify: `core/tools/builtin/skill.py`
- Modify: `tests/test_runtime_logging.py`
- Modify: `tests/session/test_skill_tool.py`

- [ ] **Step 1: 写 failing tests，锁定 tool label、compact result summary 和 skill stdout 边界**

```python
# tests/test_runtime_logging.py
from rich.console import Console

from core.ui.renderer import RichRenderer


def test_renderer_formats_human_friendly_tool_labels() -> None:
    console = Console(record=True, width=120)
    renderer = RichRenderer(console)

    renderer.show_tool_call("skill", {"skill": "analysis-report"})
    renderer.show_tool_call("read_file", {"path": "/tmp/TEST_DATA.csv"})
    renderer.show_tool_call("bash", {"description": "分析 CSV 结构", "command": "python3 -c 'print(42)'"})
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
    renderer.show_tool_result(
        "read_file",
        "1\tcol_a,col_b\n2\t1,2\n3\t3,4\n\n(已截断，显示第 1-3 行，共 50 行)",
    )

    output = console.export_text()

    assert "已加载 skill，等待重新规划" in output
    assert "已读取文件内容，预览 3 行" in output
```

```python
# tests/session/test_skill_tool.py
def test_skill_tool_does_not_write_direct_stdout(tmp_path: Path, capsys) -> None:
    from core.tools.builtin.skill import handle

    _write_skill(
        tmp_path,
        "analysis-report",
        "---\\nname: Analysis Report\\ndescription: Generate reports\\n---\\n\\nFollow the workflow.\\n",
    )
    registry = SkillRegistry()
    catalog = registry.discover(tmp_path / ".harness" / "skills", working_dir=tmp_path)
    state = SessionState(conversation_messages=[], skill_catalog=catalog)
    ctx = _make_context(tmp_path, state, registry)

    result = handle({"skill": "analysis-report"}, ctx)
    captured = capsys.readouterr()

    assert result.success is True
    assert captured.out == ""
```

- [ ] **Step 2: 运行 renderer / skill tool 测试，确认当前仍输出原始参数与 `[Skill] ...`**

Run: `pytest tests/test_runtime_logging.py tests/session/test_skill_tool.py -q`
Expected:
- label 测试失败，因为当前 `show_tool_call()` 还是原始参数摘要
- result summary 测试失败，因为当前 `show_tool_result()` 还是大段 preview
- `skill` tool stdout 测试失败，因为 handler 仍会直接 `sys.stdout.write(...)`

- [ ] **Step 3: 在 renderer 中实现标签/摘要规则，并只删除 skill.py 的 stdout 输出**

```python
# core/ui/renderer.py
from pathlib import Path


def _tool_call_label(name: str, args: dict[str, Any]) -> str:
    if name == "skill" and args.get("skill"):
        return f"Skill({args['skill']})"
    if name == "read_file" and args.get("path"):
        return f"Read({Path(str(args['path'])).name})"
    if name == "bash" and args.get("description"):
        return f"Bash({args['description']})"
    if name == "bash" and args.get("command"):
        return f"Bash({args['command']})"
    if name == "find" and args.get("pattern"):
        return f"Find({args['pattern']})"
    if name == "write_file" and args.get("path"):
        return f"Write({Path(str(args['path'])).name})"
    if name == "edit_file" and args.get("path"):
        return f"Edit({Path(str(args['path'])).name})"

    preferred_keys = ("path", "pattern", "query", "task", "offset", "limit")
    parts = [f"{key}={args[key]!r}" for key in preferred_keys if key in args]
    return name if not parts else f"{name} " + " ".join(parts)


def _line_count_preview(output: str) -> int:
    return sum(1 for line in output.splitlines() if "\t" in line and line.split("\t", 1)[0].isdigit())


def _tool_result_summary(name: str, output: str) -> str:
    if name == "skill" and output.startswith("Skill loaded:"):
        return "已加载 skill，等待重新规划"
    if name == "read_file":
        line_count = _line_count_preview(output)
        if line_count:
            return f"已读取文件内容，预览 {line_count} 行"
    if name == "find" and output and not output.startswith("未找到匹配"):
        file_count = len([line for line in output.splitlines() if line.strip() and not line.startswith("(")])
        if file_count:
            return f"已找到 {file_count} 个匹配文件"
    return _preview_output(output)
```

```python
# core/ui/renderer.py
    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._console.print(f"[yellow]{_tool_call_label(name, args)}[/yellow]")

    def show_tool_result(self, name: str, output: str) -> None:
        self._console.print(_tool_result_summary(name, output))
```

```diff
# core/tools/builtin/skill.py
# 保留现有 SCHEMA / READONLY / ANNOTATIONS / handle() 主体不变，只移除 stdout 副作用。

-import sys
 from typing import Any

 ...

-    ref_count = len(content.reference_bodies)
-    ref_chars = sum(len(v) for v in content.reference_bodies.values())
-    sys.stdout.write(
-        f"\033[36m[Skill] 内联加载 {skill_id}"
-        f" ({ref_count} refs, {ref_chars:,} chars 内联)\033[0m\n"
-    )

     return ToolResult(
         output=f"Skill loaded: {skill_id}. Re-evaluate your next action using the injected skill guidance.",
         success=True,
         injected_messages=[message],
         barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
     )
```

- [ ] **Step 4: 回跑 renderer / skill tool 测试**

Run: `pytest tests/test_runtime_logging.py tests/session/test_skill_tool.py -q`
Expected: PASS

- [ ] **Step 5: 提交 renderer label / skill stdout 清理**

```bash
git add core/ui/renderer.py core/tools/builtin/skill.py tests/test_runtime_logging.py tests/session/test_skill_tool.py
git commit -m "feat: render human-friendly tool events"
```

---

### Task 4: 把 runtime trace 收束到 debug 模式，但先保持现有 todo 渲染路径不变

**Files:**
- Modify: `core/tools/runtime.py`
- Modify: `tests/test_runtime_logging.py`

- [ ] **Step 1: 写 failing tests，先只锁定 compact/debug 分层**

```python
# tests/test_runtime_logging.py
from core.shared.run_options import RunDisplayOptions
from core.tools.context import ToolUseContext
from core.tools.runtime import ToolCall, ToolExecutorRuntime


def test_runtime_compact_mode_hides_internal_trace(capsys) -> None:
    renderer = FakeRenderer()
    runtime = ToolExecutorRuntime(
        FakeRegistry(),
        ToolUseContext(working_dir=".", max_turns=5),
        display=RunDisplayOptions(),
        renderer=renderer,
    )

    runtime.execute_batch([
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

    runtime.execute_batch([
        ToolCall(idx=0, name="read_file", call_id="call_1", args={"path": "README.md"})
    ])

    captured = capsys.readouterr()
    assert "[Runtime] 收到 1 个 tool_call" in captured.out
```

- [ ] **Step 2: 运行 runtime logging tests，确认当前 compact 模式仍直接打印 trace**

Run: `pytest tests/test_runtime_logging.py -q`
Expected:
- compact 模式测试失败，因为当前仍打印 `[Runtime] ...`

- [ ] **Step 3: 在 runtime 中加入 trace gating 和 compact progress，并修正进度变量名**

```python
# core/tools/runtime.py
def _trace_enabled(self) -> bool:
    return (not self._display.quiet) and self._display.runtime_trace == "debug"
```

```python
# core/tools/runtime.py
        if self._trace_enabled():
            sys.stdout.write(f"\033[36m[Runtime] 收到 {len(tool_calls)} 个 tool_call，分为 {len(batches)} 批：\033[0m\n")
            for i, b in enumerate(batches):
                mode = "并行" if b.parallel else "串行"
                names = [c.name for c in b.calls]
                sys.stdout.write(f"\033[36m[Runtime]   Batch {i}: [{mode}] {names}\033[0m\n")
```

```python
# core/tools/runtime.py (_run_single)
        shown_trace_progress = False
        shown_compact_status = False
        while thread.is_alive():
            thread.join(timeout=1.0)
            if thread.is_alive():
                elapsed = int(time.time() - start)
                if elapsed >= 2:
                    if self._trace_enabled():
                        sys.stdout.write(f"\r\033[K\033[36m[Runtime]   ⏳ {call.name} 执行中... {elapsed}s\033[0m")
                        sys.stdout.flush()
                        shown_trace_progress = True
                    elif (
                        self._renderer is not None
                        and not self._display.quiet
                        and not shown_compact_status
                    ):
                        self._renderer.show_status(f"{call.name} 执行中... {elapsed}s")
                        shown_compact_status = True

        if shown_trace_progress and not self._display.quiet:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
```

```python
# core/tools/runtime.py
# 本任务保留现有 todo 专用渲染逻辑不变；
# todo 从 runtime 迁到 QueryLoop 的改动统一放到 Task 5，避免中间态出现可见性空窗。
```

- [ ] **Step 4: 回跑 runtime logging tests**

Run: `pytest tests/test_runtime_logging.py -q`
Expected: PASS

- [ ] **Step 5: 提交 runtime compact/debug 分层**

```bash
git add core/tools/runtime.py tests/test_runtime_logging.py
git commit -m "feat: split compact and debug runtime traces"
```

---

### Task 5: 原子迁移 todo 显示到 QueryLoop，并同步替换过时的 runtime 级测试

**Files:**
- Modify: `core/query/loop.py`
- Modify: `core/tools/runtime.py`
- Modify: `tests/test_query_display.py`
- Modify: `tests/test_runtime_logging.py`

- [ ] **Step 1: 先替换旧的 runtime todo 测试，再写 QueryLoop 级三态展示测试**

```python
# tests/test_runtime_logging.py
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

    runtime.execute_batch([ToolCall(idx=0, name="todo", call_id="toolu_todo", args={"items": []})])

    assert renderer.tool_calls == []
    assert renderer.tool_results == []
```

```python
# tests/test_runtime_logging.py
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

    runtime.execute_batch([ToolCall(idx=0, name="todo", call_id="toolu_todo", args={"items": []})])

    assert renderer.tool_calls == [("todo", {"items": []})]
    assert renderer.tool_results == [("todo", "Todo plan updated successfully.")]
```

```python
# tests/test_query_display.py
from core.session.state import TodoItem, TodoState


def test_query_loop_renders_full_todo_plan_once_then_current_focus() -> None:
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeRenderer()

    class TodoWritingRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def execute_batch(self, tool_calls):
            self.calls += 1
            session_state.todo_state = TodoState(
                items=[
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
            )
            return ToolBatchResult(
                tool_results=[],
                files_modified=[],
                tool_names=["todo"],
                tool_successes=[True],
                injected_messages=[],
                context_patches=[],
                barrier=None,
            )

    gateway = FakeModelGateway(
        [
            ModelResponse(content="先建立执行计划。", tool_calls=[{"id": "toolu_todo_1", "name": "todo", "args": {"items": []}}]),
            ModelResponse(content="继续按计划推进。", tool_calls=[{"id": "toolu_todo_2", "name": "todo", "args": {"items": []}}]),
            ModelResponse(content="done"),
        ]
    )

    QueryLoop().run(
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
        def execute_batch(self, tool_calls):
            session_state.todo_state = TodoState(
                items=[],
                last_completed_items=[
                    TodoItem(
                        content="验证报告完整性",
                        active_form="验证报告完整性",
                        status="completed",
                        workflow_ref="4",
                    )
                ],
            )
            return ToolBatchResult(
                tool_results=[],
                files_modified=[],
                tool_names=["todo"],
                tool_successes=[True],
                injected_messages=[],
                context_patches=[],
                barrier=None,
            )

    gateway = FakeModelGateway(
        [
            ModelResponse(content="收尾并完成计划。", tool_calls=[{"id": "toolu_todo_done", "name": "todo", "args": {"items": []}}]),
            ModelResponse(content="done"),
        ]
    )

    QueryLoop().run(
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

    assert renderer.completion_calls == [(1, 1, 0.0)]
```

- [ ] **Step 2: 运行 todo 迁移测试，确认当前 QueryLoop 还没有接管展示，且 runtime 规则也还没切换**

Run: `pytest tests/test_query_display.py tests/test_runtime_logging.py -q`
Expected:
- 新的 QueryLoop 三态测试失败，因为目前 QueryLoop 不会根据 `todo_state` 做去重与切换
- compact/runtime todo 规则测试失败，因为当前 runtime 仍会渲染通用 todo 事件
- 需要同时删除或改写以下过时测试，否则全量测试会在迁移后继续失败：
  - `tests/test_runtime_logging.py::test_runtime_renders_active_form_after_todo_write`
  - `tests/test_runtime_logging.py::test_renderer_shows_completion_summary_when_active_plan_clears`

- [ ] **Step 3: 在同一个提交里完成 runtime todo 移除 + QueryLoop 接管 + 旧测试迁移**

```python
# core/query/loop.py
from core.session.state import TodoItem


def _clone_todo_items(items: list[TodoItem]) -> list[TodoItem]:
    return [
        TodoItem(
            content=item.content,
            active_form=item.active_form,
            status=item.status,
            workflow_ref=item.workflow_ref,
        )
        for item in items
    ]


def _todo_write_succeeded(batch: ToolBatchResult) -> bool:
    successes = batch.tool_successes or []
    return any(
        name == "todo" and idx < len(successes) and successes[idx]
        for idx, name in enumerate(batch.tool_names)
    )


def _render_todo_state_update(renderer, session_state, state: RunState, batch: ToolBatchResult) -> None:
    if renderer is None or not _todo_write_succeeded(batch):
        return

    todo_state = session_state.todo_state
    if todo_state.items:
        if state.last_displayed_todo_items != todo_state.items:
            renderer.show_progress(todo_state.items)
            state.last_displayed_todo_items = _clone_todo_items(todo_state.items)
            return

        current = next((item for item in todo_state.items if item.status == "in_progress"), None)
        if current is not None:
            completed = sum(1 for item in todo_state.items if item.status == "completed")
            renderer.show_current_todo(current, completed, len(todo_state.items))
        return

    if todo_state.last_completed_items:
        renderer.show_completion_summary(
            completed=len(todo_state.last_completed_items),
            total=len(todo_state.last_completed_items),
            elapsed=0.0,
        )
        state.last_displayed_todo_items = []
```

```python
# core/query/loop.py (inside QueryLoop.run, after _apply_batch_control_plane)
                _apply_batch_control_plane(state, batch)
                _render_todo_state_update(renderer, session_state, state, batch)
```

```python
# core/tools/runtime.py
def _should_render_generic_tool_event(self, tool_name: str) -> bool:
    if tool_name != "todo":
        return True
    return self._display.runtime_trace == "debug"
```

```python
# core/tools/runtime.py
        if self._renderer is not None and not self._display.quiet:
            for call, result in zip(ordered_calls, ordered_results):
                if not self._should_render_generic_tool_event(call.name):
                    continue
                self._renderer.show_tool_call(call.name, call.args)
                self._renderer.show_tool_result(call.name, result.output)
```

```python
# core/tools/runtime.py (_execute_serial)
    def _execute_serial(self, batch: _Batch) -> dict[int, ToolResult]:
        results: dict[int, ToolResult] = {}
        for call in batch.calls:
            if self._trace_enabled():
                sys.stdout.write(f"\033[36m[Runtime] ▶ 串行执行写工具：{call.name}\033[0m\n")
            results[call.idx] = self._run_single(call)
        return results
```

```python
# tests/test_runtime_logging.py
# 删除以下两个已过时的 runtime 级 todo 渲染测试：
# - test_runtime_renders_active_form_after_todo_write
# - test_renderer_shows_completion_summary_when_active_plan_clears
#
# 它们的行为断言由 tests/test_query_display.py 中的
# test_query_loop_renders_full_todo_plan_once_then_current_focus
# 和 test_query_loop_renders_completion_summary_when_todo_plan_clears 接替。
```

- [ ] **Step 4: 回跑 todo display regression**

Run: `pytest tests/test_query_display.py tests/test_query_logging.py tests/test_runtime_logging.py -q`
Expected: PASS

- [ ] **Step 5: 提交 todo 计划视图主路径**

```bash
git add core/query/loop.py tests/test_query_display.py
git add core/tools/runtime.py tests/test_runtime_logging.py
git commit -m "feat: move todo display ownership to query loop"
```

---

## Self-Review

### Spec coverage

- `Assistant Update` 在 tool 回合可见：Task 2 覆盖
- 无 assistant content 时的静态 fallback：Task 2 覆盖
- fallback 不写 transcript：Task 2 覆盖
- `RunDisplayOptions.runtime_trace` / compact vs debug：Task 1 + Task 4 覆盖
- renderer 的紧凑 tool label / result summary：Task 3 覆盖
- `skill` tool 不再绕过 display mode 直接写 stdout：Task 3 覆盖
- `todo` 的完整计划 / 当前聚焦 / 完成总结三态：Task 5 覆盖
- runtime 默认不再显示 `[Runtime] 收到/Batch/完成`：Task 4 覆盖
- `ToolExecutorRuntime` 不新增 transcript 写入路径：所有任务都保持这一约束
- runtime todo 渲染迁移后的旧测试替换：Task 5 覆盖
- Task 4 / Task 5 之间无 todo 可见性空窗：通过 Task 4 保持旧路径、Task 5 原子迁移覆盖

没有发现规格缺口。

### Placeholder scan

- 没有 `TBD` / `TODO` / “后续再补” 之类占位语句
- 每个任务都给了明确文件、代码片段、测试命令和预期
- 所有新增 helper 名称在前后任务中保持一致

### Type consistency

- `RunDisplayOptions.runtime_trace` 全文统一为 `Literal["compact", "debug"]`
- `RunState.last_displayed_todo_items` 全文统一为 `list[TodoItem] | None`
- fallback helper 统一使用 `_tool_fallback_fragment()` / `_build_tool_fallback_status()`
- todo helper 统一使用 `_todo_write_succeeded()` / `_render_todo_state_update()`

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-17-runtime-display-separation-implementation.md`. Two execution options:

1. Subagent-Driven (recommended) - I dispatch a fresh subagent per task, review between tasks, fast iteration

2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
