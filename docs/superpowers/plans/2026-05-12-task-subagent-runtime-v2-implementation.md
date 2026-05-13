# Task / Subagent Runtime V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `docs/superpowers/specs/2026-05-11-task-subagent-runtime-v2-design.md` 落成真实可运行实现，重点修复 subagent 的三类用户问题：输入边界不可见、执行过程黑盒等待、结果回收不可消费。

**Architecture:** 采用“先修已知断点，再升级正式协议”的兼容迁移路径。短期保留 `task_plan` / `task_execute` 作为模型可见入口，但把它们降级为 `task_sync` / `agent_dispatch` 的兼容壳。正式协议分三层落地：`DispatchEnvelope` 负责输入边界，`SubagentEvent` 负责执行可见性，`SubagentResultEnvelope` 负责结果回收；`fork` 纳入同一协议，但在实现顺序上晚于 `fresh` 主路径。

**Tech Stack:** Python 3.10+, dataclasses, pytest, Rich renderer, 现有 `SessionEngine` / `QueryLoop` / `ToolExecutorRuntime` / `PromptAssembler` / `ToolUseContext`

---

## Context

- 设计文档：[2026-05-11-task-subagent-runtime-v2-design.md](/Users/kino/works/kino/harness/docs/superpowers/specs/2026-05-11-task-subagent-runtime-v2-design.md)
- 被替代旧计划：[2026-05-10-task-subagent-runtime-implementation.md](/Users/kino/works/kino/harness/docs/superpowers/plans/2026-05-10-task-subagent-runtime-implementation.md)
- 当前实现现状：
  - `task_state`、`task_plan`、`task_execute` 已经存在。
  - `TaskPacket`、`SubagentRunResult`、`render_subagent_summary()` 仍是 V1 语义。
  - `SubagentRuntime.run()` 只支持 `fresh`，且目前没有正式 dispatch preview / event protocol / result envelope。
  - `ToolExecutorRuntime` 仍然在 batch 结束后统一渲染工具事件，导致子代理执行不可见。

---

## Scope

本计划包含：

1. Phase 0 已确认 4 个断点的修复与回归测试。
2. `TaskRecordV2` 语义在现有 `TaskRecord` 上落地，不额外制造双份权威对象。
3. `DispatchRequest` / `DispatchEnvelope` / `SubagentEvent` / `SubagentResultEnvelope` 的正式协议。
4. `fresh` 主路径的 dispatch preview、事件流、结果合并。
5. `fork` 的快照继承、delta 规则、递归防护。
6. `task_plan` / `task_execute` 向 `task_sync` / `agent_dispatch` / `task_merge` 的兼容迁移。

本计划不包含：

- 后台异步 subagent。
- transcript toggle UI。
- EventBus、消息队列、pub-sub 中间件。
- 结果二次 LLM 后处理。

---

## Implementation Rules

1. 先补测试，再补实现。
2. 每个阶段结束都要能在 CLI 上看到清晰用户收益。
3. 不允许新增“只在 renderer 打印，但协议层不可追踪”的旁路逻辑。
4. `fresh` 与 `fork` 必须共享正式 envelope/result 协议；区别只体现在 compile rule 和 runtime context 来源。
5. V2 语义优先，但代码命名允许保守迁移：
   - `TaskRecordV2` 先落在现有 `TaskRecord` 上。
   - `SubagentSessionRuntime` 可先通过扩展现有 `SubagentRuntime` 达成，再决定是否重命名。
6. validation 默认 `warning`，只有 strict policy 才 hard fail。

---

## File Map

| 文件 | 操作 | 责任 |
| --- | --- | --- |
| `core/tasks/models.py` | Modify | 升级权威任务对象；引入 `DispatchRequest`、`DispatchEnvelope`、`SubagentEvent`、`SubagentResultEnvelope` |
| `core/tasks/dispatcher.py` | Modify | 编译 dispatch、校验 envelope、合并 subagent 结果 |
| `core/tasks/planner_runtime.py` | Modify | `task_plan` 的 normalization / validation / V2 字段写入 |
| `core/tasks/projection.py` | Modify | `TaskState -> TodoItem[]` 兼容投影继续保留 |
| `core/session/state.py` | Modify | 增加 dispatch timeline / snapshot / result ref 所需持久状态 |
| `core/query/state.py` | Modify | 当前 dispatch、事件可见性、warning、trace ref 等单轮状态 |
| `core/query/reducers.py` | Modify | 处理新的 session/run updates，维护 task 投影和 runtime state |
| `core/tools/context.py` | Modify | runtime 绑定 renderer、snapshot、dispatch 访问句柄 |
| `core/tools/runtime.py` | Modify | 修复实时事件时序，提供 subagent tool-event hook |
| `core/tools/builtin/task_plan.py` | Modify | 作为 `task_sync` 兼容壳写入正式任务对象 |
| `core/tools/builtin/task_execute.py` | Modify | 作为 `agent_dispatch(task_id=...)` 兼容壳 |
| `core/session/subagent.py` | Modify | dispatch preview、fresh/fork context、event callback、result envelope |
| `core/ui/renderer.py` | Modify | dispatch preview、subagent event stream、compact/verbose 渲染 |
| `core/prompt/assembler.py` | Modify | 保证 preview 与真实模型输入共用同一份 dispatch 数据 |
| `core/prompt/system_context.py` | Modify | 系统提示对 task/subagent 的正式约束 |
| `core/query/loop.py` | Modify | event display、task merge、status fallback 的统一接线 |
| `tests/session/test_subagent_runtime.py` | Modify | Phase 0、fresh、fork、event 顺序、result envelope |
| `tests/session/test_task_execute_tool.py` | Modify | 兼容入口、错误路径、merge 结果 |
| `tests/session/test_task_plan_tool.py` | Modify | 正式字段写入、validation warning / strict fail |
| `tests/test_task_runtime_models.py` | Modify | datamodel、compile rules、result envelope |
| `tests/test_runtime_control_plane.py` | Modify | context bind、run/session update、snapshot/runtime 句柄 |
| `tests/test_query_display.py` | Modify | dispatch preview、event 渲染、summary-only completion |
| `tests/test_tool_runtime.py` | Create | renderer 传递、工具实时事件、batch replay 回归 |
| `tests/test_tool_registry.py` | Modify | 若新增内部可见控制平面 schema，更新注册验证 |

---

## Task 1: 锁定 Phase 0 回归面

**Files:**
- Modify: `tests/session/test_subagent_runtime.py`
- Modify: `tests/session/test_task_execute_tool.py`
- Create: `tests/test_tool_runtime.py`
- Modify: `tests/test_runtime_control_plane.py`

- [ ] **Step 1: 先写 `SubagentRuntime` 的 Phase 0 回归测试**

在 `tests/session/test_subagent_runtime.py` 追加以下测试：

```python
from types import SimpleNamespace
from unittest.mock import patch

from core.session.subagent import _render_fresh_packet, SubagentRequest, SubagentRuntime, SubagentType
from core.tasks.models import TaskExecutionMode, TaskPacket
from core.tools.context import ToolUseContext


def test_render_fresh_packet_includes_task_context() -> None:
    packet = TaskPacket(
        task_id="task-1",
        mode=TaskExecutionMode.FRESH_SUBAGENT,
        agent_type="general",
        title="Travel research",
        directive="Research travel plan",
        task_context=["Date: 2026-05-12", "Start: Shenzhen University Town"],
    )

    rendered = _render_fresh_packet(packet)

    assert "Task context:" in rendered
    assert "Date: 2026-05-12" in rendered
    assert "Start: Shenzhen University Town" in rendered


def test_subagent_runtime_passes_tools_to_session_engine(tmp_path) -> None:
    parent = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    runtime = SubagentRuntime(parent_context=parent)
    request = SubagentRequest(
        task_packet=TaskPacket(
            task_id="task-1",
            mode=TaskExecutionMode.FRESH_SUBAGENT,
            agent_type="general",
            title="Inspect runtime",
            directive="Inspect runtime deeply",
        ),
        agent_type=SubagentType.GENERAL,
    )

    captured = {}

    class FakeEngine:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.state = SimpleNamespace(system_prompt_override=None, invoked_skills={})

        def submit_user_message(self, prompt):
            return SimpleNamespace(
                final_output="done",
                success=True,
                stop_reason="completed",
                turns_used=1,
                files_modified=[],
            )

    with patch("core.session.subagent.SessionEngine", FakeEngine):
        runtime.run(request)

    assert captured["tools"] is not None
    assert captured["tools"] != []
```

- [ ] **Step 2: 写 `task_execute` 的错误路径和 merge 入口测试**

在 `tests/session/test_task_execute_tool.py` 追加：

```python
def test_task_execute_fails_for_unknown_task_id(tmp_path) -> None:
    from core.tools.builtin.task_execute import handle

    state = SessionState(conversation_messages=[])
    result = handle({"task_id": "missing"}, _context(state, tmp_path))

    assert result.status.value == "failure"
    assert result.error == "not_found"


def test_task_execute_surfaces_subagent_failure(tmp_path) -> None:
    from core.tools.builtin.task_execute import handle

    state = SessionState(conversation_messages=[])
    state.task_state.tasks_by_id["task-1"] = TaskRecord(
        task_id="task-1",
        subject="Inspect runtime",
        goal="Inspect runtime deeply",
        status=TaskStatus.IN_PROGRESS,
        execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
    )
    state.task_state.ordered_task_ids = ["task-1"]

    fake_result = SimpleNamespace(
        success=False,
        output="tool failed",
        files_modified=[],
        stop_reason=SimpleNamespace(value="tool_error"),
        turns_used=2,
    )

    with patch("core.session.subagent.SubagentRuntime.run", return_value=fake_result):
        result = handle({"task_id": "task-1"}, _context(state, tmp_path))

    assert result.status.value == "success"
    assert result.session_updates[0].payload["task_state"].tasks_by_id["task-1"].status == TaskStatus.FAILED
```

- [ ] **Step 3: 新建 runtime 时序回归测试**

Create `tests/test_tool_runtime.py`:

```python
from core.query.state import RunState
from core.query.reducers import apply_run_update, apply_session_update
from core.session.state import SessionState
from core.tools import ToolRegistry
from core.tools.context import ToolInvocationOutcome, ToolOutcomeStatus, ToolUseContext, make_tool_message
from core.tools.runtime import ToolCall, ToolExecutorRuntime


class RecorderRenderer:
    def __init__(self) -> None:
        self.events = []

    def show_tool_call(self, name, args):
        self.events.append(("call", name))

    def show_tool_result(self, name, output):
        self.events.append(("result", name, output))


class _ReadTool:
    SCHEMA = {"name": "read_file", "description": "read_file", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = True
    ANNOTATIONS = {"readonly": True, "destructive": False, "idempotent": True, "concurrency_safe": True}

    @staticmethod
    def handle(args, context):
        return ToolInvocationOutcome(
            status=ToolOutcomeStatus.SUCCESS,
            messages=[make_tool_message(context, "ok")],
        )


def test_runtime_renders_tool_call_before_tool_result(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(_ReadTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    renderer = RecorderRenderer()
    runtime = ToolExecutorRuntime(reg, ctx, renderer=renderer)
    batch = runtime.execute_batch(
        [ToolCall(idx=0, name="read_file", call_id="toolu_1", args={"path": "README.md"})],
        run_state=RunState(),
        apply_session_update=lambda update: apply_session_update(SessionState(conversation_messages=[]), update),
        apply_run_update=apply_run_update,
    )

    assert batch.tool_statuses == [ToolOutcomeStatus.SUCCESS]
    assert renderer.events[0] == ("call", "read_file")
    assert renderer.events[1][0] == "result"
```

- [ ] **Step 4: 跑 Phase 0 回归测试，确认当前主干失败**

Run:

```bash
pytest tests/session/test_subagent_runtime.py tests/session/test_task_execute_tool.py tests/test_tool_runtime.py -q
```

Expected:

- `_render_fresh_packet` 缺 `task_context` 导致失败。
- `SubagentRuntime` 未传 `tools=` 导致失败。
- `tests/test_tool_runtime.py` 失败，因为当前渲染在 batch 结束后回放。

- [ ] **Step 5: 提交测试基线**

```bash
git add tests/session/test_subagent_runtime.py tests/session/test_task_execute_tool.py tests/test_tool_runtime.py tests/test_runtime_control_plane.py
git commit -m "test: lock subagent phase-0 regressions"
```

---

## Task 2: 修复 Phase 0 已知断点

**Files:**
- Modify: `core/tools/context.py`
- Modify: `core/tools/runtime.py`
- Modify: `core/session/subagent.py`

- [ ] **Step 1: 给 `ToolUseContext` 增加 renderer 句柄**

在 `core/tools/context.py` 的 `ToolUseContext.__init__` 和属性区补上：

```python
class ToolUseContext:
    def __init__(self, *, working_dir: str, max_turns: int):
        ...
        self._renderer: Any = None

    @property
    def renderer(self) -> Any:
        return self._renderer

    def bind_runtime(
        self,
        *,
        session_state: Any | None = None,
        skill_registry: Any | None = None,
        renderer: Any | None = None,
    ) -> None:
        if session_state is not None:
            self._session_state = session_state
        if skill_registry is not None:
            self._skill_registry = skill_registry
        if renderer is not None:
            self._renderer = renderer
```

- [ ] **Step 2: 在 runtime 内把 renderer 传入 call context，并改实时渲染时序**

在 `core/tools/runtime.py` 做两件事：

1. `__init__` 保存 renderer 后，同步写回根 context。
2. 在每个批次执行前发出 `show_tool_call()`，拿到结果后立刻发 `show_tool_result()`，而不是等全部 batch 完成再统一回放。

目标改动骨架：

```python
class ToolExecutorRuntime:
    def __init__(..., renderer=None):
        ...
        self._renderer = renderer
        if renderer is not None:
            context._renderer = renderer

    def _emit_tool_started(self, call: ToolCall) -> None:
        if self._renderer is not None and not self._display.quiet and self._should_render_generic_tool_event(call.name):
            self._renderer.show_tool_call(call.name, call.args)

    def _emit_tool_finished(self, call: ToolCall, result: ToolInvocationOutcome) -> None:
        if self._renderer is not None and not self._display.quiet and self._should_render_generic_tool_event(call.name):
            self._renderer.show_tool_result(call.name, self._first_content(result))
```

串行路径在 `_run_single` 前触发 started，拿到 outcome 后立即触发 finished；只读并行路径在提交 future 前触发 started，在 `future.result()` 后触发 finished。

- [ ] **Step 3: 修复 `SubagentRuntime` 的 `tools=` 和 preview 漏字段**

在 `core/session/subagent.py`：

```python
def _render_fresh_packet(packet: TaskPacket) -> str:
    sections = [
        f"Task: {packet.title}",
        "",
        "Directive:",
        packet.directive,
    ]
    if packet.task_context:
        sections.extend(["", "Task context:"] + [f"- {item}" for item in packet.task_context])
    ...

engine = SessionEngine(
    model_gateway=ModelGateway(self._llm_factory()),
    tool_runtime=ToolExecutorRuntime(sub_registry, tool_context, display=RunDisplayOptions(quiet=True)),
    tool_context=tool_context,
    policy_runner=PolicyRunner([MaxTurnsPolicy(max_turns)]),
    recovery=RecoveryManager(),
    view_builder=MessageViewBuilder(tools=sub_schemas),
    tools=sub_schemas,
)
```

如果 `SessionEngine` 当前构造签名不接受 `tools=`，先同步补它的构造器与存储字段，使 `MessageViewBuilder` 和 `model_gateway.call_once(..., tools=...)` 都用同一份 schema。

- [ ] **Step 4: 重新跑 Phase 0 测试**

Run:

```bash
pytest tests/session/test_subagent_runtime.py tests/session/test_task_execute_tool.py tests/test_tool_runtime.py -q
```

Expected:

- 全部 PASS。

- [ ] **Step 5: 提交 Phase 0 修复**

```bash
git add core/tools/context.py core/tools/runtime.py core/session/subagent.py tests/session/test_subagent_runtime.py tests/session/test_task_execute_tool.py tests/test_tool_runtime.py
git commit -m "fix: restore visible and wired subagent runtime baseline"
```

---

## Task 3: 引入 V2 正式协议模型

**Files:**
- Modify: `core/tasks/models.py`
- Modify: `tests/test_task_runtime_models.py`

- [ ] **Step 1: 先写协议模型测试**

在 `tests/test_task_runtime_models.py` 增加：

```python
from core.tasks.models import (
    DispatchEnvelope,
    DispatchRequest,
    SubagentEvent,
    SubagentResultEnvelope,
    TaskExecutionMode,
)


def test_dispatch_request_defaults_are_explicit() -> None:
    req = DispatchRequest(
        task_id="task-1",
        dispatch_id="dispatch-1",
        mode="fresh",
        agent_role="general",
        max_turns=None,
        parent_snapshot_ref=None,
    )
    assert req.mode == "fresh"
    assert req.parent_snapshot_ref is None


def test_dispatch_envelope_supports_fork_fields() -> None:
    env = DispatchEnvelope(
        task_id="task-1",
        dispatch_id="dispatch-1",
        mode="fork",
        agent_role="plan",
        title="Analyze failing spec",
        objective="Analyze the current spec",
        why=["Need isolated analysis"],
        task_context=["Review section 17"],
        known_facts=["Phase 0 bugs already fixed"],
        constraints=["Do not edit files"],
        expected_output=["Short analysis"],
        done_criteria=["List retained findings"],
        required_skills=[],
        allowed_tools=["find", "read_file"],
        write_scope=[],
        parent_snapshot_ref="snap-1",
        delta_context=["Focus on section 17"],
        excluded_parent_context=["Ignore unrelated pending tasks"],
        envelope_revision=1,
    )
    assert env.parent_snapshot_ref == "snap-1"
    assert env.delta_context == ["Focus on section 17"]


def test_subagent_result_envelope_preserves_raw_text_and_metadata() -> None:
    result = SubagentResultEnvelope(
        task_id="task-1",
        dispatch_id="dispatch-1",
        success=True,
        completion_kind="completed",
        summary="Collected weather and route summary",
        raw_text="Longer final answer from subagent",
        artifacts=[],
        files_modified=[],
        open_questions=["Which hotel area is best?"],
        recommended_next_steps=["Book transport"],
        failure_reason=None,
        trace_ref="trace-1",
        metadata={"turns_used": 3, "duration_ms": 1200},
    )
    assert result.raw_text.startswith("Longer")
    assert result.metadata["turns_used"] == 3
```

- [ ] **Step 2: 运行模型测试，确认当前缺少 V2 类型**

Run:

```bash
pytest tests/test_task_runtime_models.py -q
```

Expected:

- FAIL，缺少 `DispatchRequest` / `DispatchEnvelope` / `SubagentEvent` / `SubagentResultEnvelope`。

- [ ] **Step 3: 在 `core/tasks/models.py` 定义正式协议 dataclass**

在保留现有 `TaskRecord` / `TaskState` 的同时，补入以下定义：

```python
from typing import Any, Literal


@dataclass(slots=True)
class DispatchRequest:
    task_id: str
    dispatch_id: str
    mode: Literal["fresh", "fork"]
    agent_role: str
    max_turns: int | None
    parent_snapshot_ref: str | None


@dataclass(slots=True)
class DispatchEnvelope:
    task_id: str
    dispatch_id: str
    mode: Literal["fresh", "fork"]
    agent_role: str
    title: str
    objective: str
    why: list[str] = field(default_factory=list)
    task_context: list[str] = field(default_factory=list)
    known_facts: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    expected_output: list[str] = field(default_factory=list)
    done_criteria: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    parent_snapshot_ref: str | None = None
    delta_context: list[str] = field(default_factory=list)
    excluded_parent_context: list[str] = field(default_factory=list)
    envelope_revision: int = 0


@dataclass(slots=True)
class SubagentEvent:
    dispatch_id: str
    task_id: str
    seq: int
    kind: str
    status: str
    title: str
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


@dataclass(slots=True)
class SubagentResultEnvelope:
    task_id: str
    dispatch_id: str
    success: bool
    completion_kind: Literal["completed", "blocked", "failed", "cancelled"]
    summary: str
    raw_text: str
    artifacts: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    recommended_next_steps: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    trace_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

同时升级 `TaskRecord`，把 V2 语义字段落在现有类上：

```python
title: str | None = None
objective: str | None = None
agent_role: str | None = None
why: list[str] = field(default_factory=list)
constraints: list[str] = field(default_factory=list)
tool_policy: dict[str, Any] = field(default_factory=dict)
result_ref: str | None = None
timeline_ref: str | None = None
```

不要新建 `TaskRecordV2` 第二份类；在注释里写明 `TaskRecord` 已承载 V2 语义。

- [ ] **Step 4: 跑模型测试**

Run:

```bash
pytest tests/test_task_runtime_models.py -q
```

Expected:

- PASS。

- [ ] **Step 5: 提交协议模型**

```bash
git add core/tasks/models.py tests/test_task_runtime_models.py
git commit -m "feat: add v2 task dispatch and result protocol models"
```

---

## Task 4: 升级 `task_plan` 写入 V2 权威任务对象

**Files:**
- Modify: `core/tasks/planner_runtime.py`
- Modify: `core/tools/builtin/task_plan.py`
- Modify: `tests/session/test_task_plan_tool.py`

- [ ] **Step 1: 先写 `task_plan` V2 字段和 validation 测试**

在 `tests/session/test_task_plan_tool.py` 追加：

```python
def test_task_plan_preserves_v2_fields(tmp_path) -> None:
    from core.tools.builtin.task_plan import handle

    state = SessionState(conversation_messages=[])
    result = handle(
        {
            "tasks": [
                {
                    "task_id": "task-1",
                    "subject": "Travel planning",
                    "goal": "Plan a 3-day trip",
                    "status": "in_progress",
                    "execution_mode": "fresh_subagent",
                    "agent_role": "general",
                    "why": ["Network search is noisy in main context"],
                    "constraints": ["Public transit only"],
                    "required_skills": ["weather", "serper-search"],
                    "expected_output": ["Connected 3-day itinerary"],
                }
            ]
        },
        _context(state, tmp_path),
    )

    apply_session_update(state, result.session_updates[0])
    task = state.task_state.tasks_by_id["task-1"]
    assert task.agent_role == "general"
    assert task.why == ["Network search is noisy in main context"]
    assert task.constraints == ["Public transit only"]
    assert task.required_skills == ["weather", "serper-search"]


def test_task_plan_fresh_search_missing_time_or_place_is_warning_not_failure(tmp_path) -> None:
    from core.tools.builtin.task_plan import handle

    state = SessionState(conversation_messages=[])
    result = handle(
        {
            "tasks": [
                {
                    "subject": "Weather lookup",
                    "goal": "Check the weather",
                    "execution_mode": "fresh_subagent",
                    "constraints": [],
                }
            ]
        },
        _context(state, tmp_path),
    )

    assert result.status.value == "success"
    assert any("warning" in message["content"].lower() for message in result.messages)
```

- [ ] **Step 2: 跑 `task_plan` 测试，确认当前实现不支持**

Run:

```bash
pytest tests/session/test_task_plan_tool.py -q
```

Expected:

- FAIL，`TaskRecord` 当前未写入 `agent_role` / `why` / `constraints`。
- FAIL，当前没有 warning-only validation 机制。

- [ ] **Step 3: 扩展 `normalize_task_payload()` 与 `build_task_state()`**

在 `core/tasks/planner_runtime.py`：

```python
def normalize_task_payload(raw: dict, *, index: int, previous: TaskState) -> TaskRecord:
    subject = str(raw["subject"]).strip()
    goal = str(raw["goal"]).strip()
    return TaskRecord(
        task_id=raw.get("task_id") or f"task-{index + 1}",
        subject=subject,
        title=subject,
        goal=goal,
        objective=goal,
        status=TaskStatus(raw.get("status", "pending")),
        execution_mode=TaskExecutionMode(raw.get("execution_mode", "local")),
        agent_role=raw.get("agent_role"),
        why=list(raw.get("why") or []),
        constraints=list(raw.get("constraints") or raw.get("out_of_scope") or []),
        ...
    )
```

再新增一个 warning 收集 helper：

```python
def collect_task_warnings(task: TaskRecord) -> list[str]:
    warnings: list[str] = []
    if task.execution_mode == TaskExecutionMode.FRESH_SUBAGENT:
        text = " ".join([task.objective or "", *task.inputs, *task.constraints]).lower()
        if "weather" in text or "trip" in text or "travel" in text:
            if not any(token in text for token in ["2026-", "tomorrow", "明天", "from", "start", "出发"]):
                warnings.append(f"Task {task.task_id} looks under-specified for fresh search: missing time or route context")
    return warnings
```

`build_task_state()` 只负责构建 state；warning 留给 `task_plan.handle()` 作为成功消息回传。

- [ ] **Step 4: 在 `task_plan.handle()` 回传 warning 消息**

在 `core/tools/builtin/task_plan.py`：

```python
from core.tasks.planner_runtime import build_task_state, collect_task_warnings

...
next_state = build_task_state(...)
warnings = []
for task_id in next_state.ordered_task_ids:
    warnings.extend(collect_task_warnings(next_state.tasks_by_id[task_id]))

messages = [make_tool_message(context, f"TaskState rewritten with {len(next_state.ordered_task_ids)} tasks.")]
messages.extend(make_tool_message(context, f"Warning: {warning}") for warning in warnings)
```

不要因为 warning 改成 failure；strict policy 以后在 `compile_dispatch_envelope(..., strict=True)` 再处理。

- [ ] **Step 5: 跑 `task_plan` 测试**

Run:

```bash
pytest tests/session/test_task_plan_tool.py tests/test_task_runtime_models.py -q
```

Expected:

- PASS。

- [ ] **Step 6: 提交 V2 任务对象写入**

```bash
git add core/tasks/planner_runtime.py core/tools/builtin/task_plan.py tests/session/test_task_plan_tool.py
git commit -m "feat: write v2 task semantics through task_plan"
```

---

## Task 5: 实现 `DispatchRequest` / `DispatchEnvelope` 编译与校验

**Files:**
- Modify: `core/tasks/dispatcher.py`
- Modify: `tests/test_task_runtime_models.py`

- [ ] **Step 1: 先写 dispatch 编译与校验测试**

在 `tests/test_task_runtime_models.py` 继续追加：

```python
from core.tasks.dispatcher import build_dispatch_request, compile_dispatch_envelope, validate_dispatch_envelope
from core.tasks.models import TaskExecutionMode, TaskRecord, TaskStatus


def _task(**overrides):
    task = TaskRecord(
        task_id="task-1",
        subject="Travel planning",
        title="Travel planning",
        goal="Plan a 3-day trip",
        objective="Plan a 3-day trip",
        status=TaskStatus.IN_PROGRESS,
        execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
        agent_role="general",
        why=["Avoid main-context search noise"],
        inputs=["Date: 2026-05-12", "From: Shenzhen University Town"],
        constraints=["Public transit only"],
        expected_output=["3-day itinerary"],
    )
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


def test_compile_dispatch_envelope_maps_v2_task_fields() -> None:
    task = _task()
    req = build_dispatch_request(task, dispatch_id="dispatch-1")
    env = compile_dispatch_envelope(task, req, session_state=None)

    assert env.dispatch_id == "dispatch-1"
    assert env.objective == "Plan a 3-day trip"
    assert env.why == ["Avoid main-context search noise"]
    assert env.task_context == ["Date: 2026-05-12", "From: Shenzhen University Town"]
    assert env.constraints == ["Public transit only"]


def test_validate_dispatch_envelope_returns_warning_for_under_specified_fresh_search() -> None:
    task = _task(inputs=[], constraints=[], objective="Check weather")
    req = build_dispatch_request(task, dispatch_id="dispatch-1")
    env = compile_dispatch_envelope(task, req, session_state=None)

    warnings = validate_dispatch_envelope(env, strict=False)
    assert warnings
    assert any("under-specified" in item.lower() for item in warnings)


def test_validate_dispatch_envelope_strictly_rejects_invalid_fork() -> None:
    task = _task(execution_mode=TaskExecutionMode.FORK_SUBAGENT)
    req = build_dispatch_request(task, dispatch_id="dispatch-1")
    env = compile_dispatch_envelope(task, req, session_state=None)
    env.parent_snapshot_ref = None

    try:
        validate_dispatch_envelope(env, strict=True)
    except ValueError as exc:
        assert "parent_snapshot_ref" in str(exc)
    else:
        raise AssertionError("expected strict validation failure")
```

- [ ] **Step 2: 跑 dispatcher 测试，确认当前 V1 实现不足**

Run:

```bash
pytest tests/test_task_runtime_models.py -q
```

Expected:

- FAIL，当前 `dispatcher.py` 只有 `compile_task_packet()` 与 `normalize_subagent_result()`。

- [ ] **Step 3: 在 `core/tasks/dispatcher.py` 引入正式 compile/validate 入口**

建议替换为以下结构：

```python
from __future__ import annotations

from core.tasks.models import (
    DispatchEnvelope,
    DispatchRequest,
    SubagentResultEnvelope,
    TaskExecutionMode,
    TaskRecord,
    TaskRunResult,
    TaskStatus,
)


def build_dispatch_request(task: TaskRecord, *, dispatch_id: str, max_turns: int | None = None) -> DispatchRequest:
    mode = "fork" if task.execution_mode == TaskExecutionMode.FORK_SUBAGENT else "fresh"
    return DispatchRequest(
        task_id=task.task_id,
        dispatch_id=dispatch_id,
        mode=mode,
        agent_role=task.agent_role or "general",
        max_turns=max_turns,
        parent_snapshot_ref=getattr(task, "parent_snapshot_ref", None),
    )


def compile_dispatch_envelope(task: TaskRecord, request: DispatchRequest, session_state) -> DispatchEnvelope:
    return DispatchEnvelope(
        task_id=task.task_id,
        dispatch_id=request.dispatch_id,
        mode=request.mode,
        agent_role=request.agent_role,
        title=task.title or task.subject,
        objective=task.objective or task.goal,
        why=list(task.why),
        task_context=list(task.inputs),
        known_facts=list(task.known_context),
        constraints=list(task.constraints or task.out_of_scope),
        expected_output=list(task.expected_output or [task.objective or task.goal]),
        done_criteria=list(task.done_criteria),
        required_skills=list(task.required_skills),
        allowed_tools=list(task.allowed_tools),
        write_scope=list(task.write_scope),
        parent_snapshot_ref=request.parent_snapshot_ref,
        delta_context=list(getattr(task, "delta_context", []) or []),
        excluded_parent_context=list(getattr(task, "excluded_parent_context", []) or []),
        envelope_revision=task.packet_revision + 1,
    )
```

然后新增 `validate_dispatch_envelope(...)`：

```python
def validate_dispatch_envelope(envelope: DispatchEnvelope, *, strict: bool) -> list[str]:
    warnings: list[str] = []
    if envelope.mode == "fresh":
        searchable = " ".join([envelope.objective, *envelope.task_context, *envelope.constraints]).lower()
        if any(token in searchable for token in ["weather", "trip", "travel", "天气", "行程"]):
            has_time = any(token in searchable for token in ["2026-", "tomorrow", "明天", "3-day", "3天"])
            has_place = any(token in searchable for token in ["shenzhen", "深圳", "南山", "大鹏", "from", "to", "出发"])
            if not (has_time and has_place):
                msg = f"Dispatch {envelope.dispatch_id} is under-specified for fresh search"
                if strict:
                    raise ValueError(msg)
                warnings.append(msg)
    if envelope.mode == "fork":
        if not envelope.parent_snapshot_ref:
            raise ValueError("fork dispatch requires parent_snapshot_ref")
        if not envelope.delta_context and not envelope.constraints:
            raise ValueError("fork dispatch requires delta_context or constraints")
    return warnings
```

- [ ] **Step 4: 暂时保留旧兼容函数**

为了不一次性打断已有调用，先保留：

```python
def compile_task_packet(task: TaskRecord):
    request = build_dispatch_request(task, dispatch_id=f"{task.task_id}:compat")
    return compile_dispatch_envelope(task, request, session_state=None)
```

`normalize_subagent_result()` 也先保留，下一任务替换。

- [ ] **Step 5: 跑 dispatcher 测试**

Run:

```bash
pytest tests/test_task_runtime_models.py tests/session/test_task_plan_tool.py -q
```

Expected:

- PASS。

- [ ] **Step 6: 提交 dispatch compiler**

```bash
git add core/tasks/dispatcher.py tests/test_task_runtime_models.py
git commit -m "feat: compile and validate v2 dispatch envelopes"
```

---

## Task 6: 把 dispatch preview 接到真实 subagent 输入

**Files:**
- Modify: `core/session/subagent.py`
- Modify: `core/tools/builtin/task_execute.py`
- Modify: `core/ui/renderer.py`
- Modify: `tests/test_query_display.py`
- Modify: `tests/session/test_task_execute_tool.py`

- [ ] **Step 1: 先写 preview 与真实输入一致的测试**

在 `tests/session/test_task_execute_tool.py` 增加：

```python
def test_task_execute_uses_dispatch_envelope_and_shows_preview(tmp_path) -> None:
    from core.tools.builtin.task_execute import handle

    class Renderer:
        def __init__(self):
            self.packets = []

        def show_subagent_dispatch(self, packet, warnings=None):
            self.packets.append((packet, warnings or []))

    state = SessionState(conversation_messages=[])
    state.task_state.tasks_by_id["task-1"] = TaskRecord(
        task_id="task-1",
        subject="Travel planning",
        title="Travel planning",
        goal="Plan a 3-day trip",
        objective="Plan a 3-day trip",
        status=TaskStatus.IN_PROGRESS,
        execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
        agent_role="general",
        inputs=["Date: 2026-05-12", "From: Shenzhen University Town"],
        constraints=["Public transit only"],
    )
    state.task_state.ordered_task_ids = ["task-1"]

    ctx = _context(state, tmp_path)
    renderer = Renderer()
    ctx.bind_runtime(session_state=state, skill_registry=None, renderer=renderer)

    fake_result = SimpleNamespace(
        success=True,
        output="done",
        files_modified=[],
        stop_reason=SimpleNamespace(value="completed"),
        turns_used=1,
    )

    with patch("core.session.subagent.SubagentRuntime.run", return_value=fake_result):
        handle({"task_id": "task-1"}, ctx)

    assert len(renderer.packets) == 1
    packet, warnings = renderer.packets[0]
    assert packet.objective == "Plan a 3-day trip"
    assert packet.task_context == ["Date: 2026-05-12", "From: Shenzhen University Town"]
```

- [ ] **Step 2: 跑测试，确认当前还没有 preview 入口**

Run:

```bash
pytest tests/session/test_task_execute_tool.py -q
```

Expected:

- FAIL，`renderer.show_subagent_dispatch(...)` 未被调用。

- [ ] **Step 3: 在 renderer 里补 `show_subagent_dispatch()`**

在 `core/ui/renderer.py` 的 `RichRenderer` 与 `QuietRenderer` 加方法：

```python
def show_subagent_dispatch(self, packet, warnings=None) -> None:
    warnings = warnings or []
    lines = [
        f"[bold]模式:[/bold] {packet.mode}",
        f"[bold]目标:[/bold] {packet.title}",
        f"[bold]Objective:[/bold] {packet.objective}",
    ]
    if packet.task_context:
        lines.append("[bold]Task context:[/bold]")
        lines.extend(f"  - {item}" for item in packet.task_context[:5])
    if packet.constraints:
        lines.append("[bold]Constraints:[/bold]")
        lines.extend(f"  - {item}" for item in packet.constraints[:5])
    if warnings:
        lines.append("[bold yellow]Warnings:[/bold yellow]")
        lines.extend(f"  - {item}" for item in warnings)
    self._console.print(Panel("\n".join(lines), title=f"子代理派发: {packet.dispatch_id}", border_style="cyan"))
```

`QuietRenderer.show_subagent_dispatch()` 直接 `pass`。

- [ ] **Step 4: 在 `task_execute` 里编译 envelope、校验 warning、展示 preview**

在 `core/tools/builtin/task_execute.py`：

```python
from uuid import uuid4
from core.tasks.dispatcher import (
    build_dispatch_request,
    compile_dispatch_envelope,
    validate_dispatch_envelope,
)

dispatch_id = f"{task.task_id}:{uuid4().hex[:8]}"
request = build_dispatch_request(task, dispatch_id=dispatch_id)
envelope = compile_dispatch_envelope(task, request, state)
warnings = validate_dispatch_envelope(envelope, strict=False)

if context.renderer is not None:
    context.renderer.show_subagent_dispatch(envelope, warnings=warnings)

sub_result = runtime.run(
    SubagentRequest(
        task_packet=envelope,
        agent_type=SubagentType.GENERAL,
        preloaded_skill_ids=list(task.required_skills),
    )
)
```

这里先把 `SubagentRequest.task_packet` 改名兼容为 `dispatch_envelope` 更好；如果改动范围太大，短期可保留字段名，但类型必须切到 `DispatchEnvelope`。

- [ ] **Step 5: 让 `SubagentRuntime` 接受 `DispatchEnvelope` 输入**

在 `core/session/subagent.py`：

```python
from ..tasks.models import DispatchEnvelope

@dataclass
class SubagentRequest:
    task_packet: DispatchEnvelope
    ...
```

同时把 `_render_fresh_packet()` 改为读取 `objective`、`constraints`，不要再依赖 `directive`、`out_of_scope` 的 V1 命名。

- [ ] **Step 6: 跑 preview 测试**

Run:

```bash
pytest tests/session/test_task_execute_tool.py tests/test_query_display.py -q
```

Expected:

- PASS。

- [ ] **Step 7: 提交 dispatch preview 接线**

```bash
git add core/tools/builtin/task_execute.py core/session/subagent.py core/ui/renderer.py tests/session/test_task_execute_tool.py tests/test_query_display.py
git commit -m "feat: show dispatch preview from compiled v2 envelope"
```

---

## Task 7: 引入 `SubagentEvent` 和同步 callback 事件流

**Files:**
- Modify: `core/session/subagent.py`
- Modify: `core/tools/runtime.py`
- Modify: `core/ui/renderer.py`
- Modify: `tests/session/test_subagent_runtime.py`
- Modify: `tests/test_tool_runtime.py`
- Modify: `tests/test_query_display.py`

- [ ] **Step 1: 先写 event 顺序和 callback transport 测试**

在 `tests/session/test_subagent_runtime.py` 追加：

```python
def test_subagent_runtime_emits_dispatch_and_completion_events(tmp_path) -> None:
    events = []
    parent = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    runtime = SubagentRuntime(parent_context=parent)

    request = SubagentRequest(
        task_packet=DispatchEnvelope(
            task_id="task-1",
            dispatch_id="dispatch-1",
            mode="fresh",
            agent_role="general",
            title="Inspect runtime",
            objective="Inspect runtime deeply",
        ),
        agent_type=SubagentType.GENERAL,
    )

    class FakeEngine:
        def __init__(self, **kwargs):
            self.state = SimpleNamespace(system_prompt_override=None, invoked_skills={})

        def submit_user_message(self, prompt):
            return SimpleNamespace(
                final_output="done",
                success=True,
                stop_reason="completed",
                turns_used=1,
                files_modified=[],
            )

    with patch("core.session.subagent.SessionEngine", FakeEngine):
        runtime.run(request, on_event=events.append)

    assert events[0].kind == "dispatch_started"
    assert events[-1].kind == "completed"
```

在 `tests/test_query_display.py` 追加一个轻量 renderer 测试，验证 `show_status()` 或专门 event 渲染方法能收到按顺序的子代理状态行。

- [ ] **Step 2: 跑事件测试，确认当前没有 `on_event` 协议**

Run:

```bash
pytest tests/session/test_subagent_runtime.py tests/test_tool_runtime.py tests/test_query_display.py -q
```

Expected:

- FAIL，`SubagentRuntime.run()` 还没有 `on_event` 参数。

- [ ] **Step 3: 在 `SubagentRuntime` 中定义最小事件发送 helper**

在 `core/session/subagent.py` 增加：

```python
import time

def _emit_event(on_event, *, dispatch_id: str, task_id: str, seq: int, kind: str, status: str, title: str, detail=None):
    if on_event is None:
        return
    on_event(
        SubagentEvent(
            dispatch_id=dispatch_id,
            task_id=task_id,
            seq=seq,
            kind=kind,
            status=status,
            title=title,
            detail=detail or {},
            created_at=time.time(),
        )
    )
```

然后把 `run()` 改成：

```python
def run(self, request: SubagentRequest, *, on_event=None) -> SubagentRunResult:
    seq = 0
    _emit_event(on_event, dispatch_id=request.task_packet.dispatch_id, task_id=request.task_packet.task_id, seq=seq, kind="dispatch_started", status="running", title=request.task_packet.title)
    seq += 1
    ...
    _emit_event(... kind="completed" if result.success else "failed", ...)
```

- [ ] **Step 4: 用 callback 把 subagent 内工具调用转成事件**

在 `core/tools/runtime.py` 为子代理路径增加可选 hook：

```python
def __init__(..., renderer=None, event_sink=None):
    ...
    self._event_sink = event_sink

def _emit_subagent_tool_event(self, *, call: ToolCall, phase: str, result: ToolInvocationOutcome | None = None):
    if self._event_sink is None:
        return
    if phase == "started":
        self._event_sink(kind="tool_started", title=call.name, detail={"args": call.args})
    else:
        self._event_sink(kind="tool_finished", title=call.name, detail={"preview": self._first_content(result) if result else ""})
```

`SubagentRuntime` 在创建 `ToolExecutorRuntime` 时传入一个适配器，把 tool hook 转成 `SubagentEvent`。

不要引入 EventBus；这个 callback 就是第一版 transport。

- [ ] **Step 5: 在 renderer 中消费最小事件集**

在 `core/ui/renderer.py` 加一个轻量事件渲染器，或者直接给 `RichRenderer` 加方法：

```python
def show_subagent_event(self, event) -> None:
    if event.kind == "dispatch_started":
        self._console.print(f"[dim]{event.title} 已开始[/dim]")
    elif event.kind == "tool_started":
        self._console.print(f"  [dim cyan]$ {escape(event.title)}[/dim cyan]")
    elif event.kind == "tool_finished":
        preview = event.detail.get("preview", "")
        self._console.print(f"  [dim]{preview[:120]}[/dim]")
    elif event.kind in {"completed", "failed", "blocked"}:
        self._console.print(f"[dim]{event.kind}: {event.title}[/dim]")
```

compact 模式默认只显示一行摘要；verbose 以后再扩。

- [ ] **Step 6: 跑事件测试**

Run:

```bash
pytest tests/session/test_subagent_runtime.py tests/test_tool_runtime.py tests/test_query_display.py -q
```

Expected:

- PASS。

- [ ] **Step 7: 提交事件流**

```bash
git add core/session/subagent.py core/tools/runtime.py core/ui/renderer.py tests/session/test_subagent_runtime.py tests/test_tool_runtime.py tests/test_query_display.py
git commit -m "feat: add callback-driven subagent event visibility"
```

---

## Task 8: 引入最小 `SubagentResultEnvelope` 并统一主线程消费

**Files:**
- Modify: `core/session/subagent.py`
- Modify: `core/tasks/dispatcher.py`
- Modify: `core/tools/builtin/task_execute.py`
- Modify: `tests/session/test_task_execute_tool.py`
- Modify: `tests/test_task_runtime_models.py`
- Modify: `tests/test_query_display.py`

- [ ] **Step 1: 先写结果回收测试**

在 `tests/session/test_task_execute_tool.py` 增加：

```python
def test_task_execute_merges_result_envelope_summary_and_metadata(tmp_path) -> None:
    from core.tools.builtin.task_execute import handle

    state = SessionState(conversation_messages=[])
    state.task_state.tasks_by_id["task-1"] = TaskRecord(
        task_id="task-1",
        subject="Travel planning",
        title="Travel planning",
        goal="Plan a 3-day trip",
        objective="Plan a 3-day trip",
        status=TaskStatus.IN_PROGRESS,
        execution_mode=TaskExecutionMode.FRESH_SUBAGENT,
    )
    state.task_state.ordered_task_ids = ["task-1"]

    fake_result = SubagentResultEnvelope(
        task_id="task-1",
        dispatch_id="dispatch-1",
        success=True,
        completion_kind="completed",
        summary="Trip draft prepared",
        raw_text="Long weather + route + beach analysis",
        open_questions=["Need hotel choice"],
        recommended_next_steps=["Pick accommodation zone"],
        metadata={"turns_used": 3},
    )

    with patch("core.session.subagent.SubagentRuntime.run", return_value=fake_result):
        result = handle({"task_id": "task-1"}, _context(state, tmp_path))

    next_state = result.session_updates[0].payload["task_state"]
    task = next_state.tasks_by_id["task-1"]
    assert task.result_summary == "Trip draft prepared"
    assert task.status == TaskStatus.COMPLETED
    assert task.result_ref is not None
```

- [ ] **Step 2: 跑结果测试，确认当前还是字符串路径**

Run:

```bash
pytest tests/session/test_task_execute_tool.py tests/test_task_runtime_models.py -q
```

Expected:

- FAIL，当前 `SubagentRuntime.run()` 返回 `SubagentRunResult`，不是正式 envelope。

- [ ] **Step 3: 让 `SubagentRuntime.run()` 直接返回 `SubagentResultEnvelope`**

在 `core/session/subagent.py`：

1. 保留内部 `SubagentRunResult` 也可以，但对外 `run()` 返回正式 envelope。
2. 记录 metadata：
   - `turns_used`
   - `duration_ms`
   - `files_modified_count`

目标形状：

```python
def run(self, request: SubagentRequest, *, on_event=None) -> SubagentResultEnvelope:
    started_at = time.time()
    ...
    model_result = engine.submit_user_message(prompt_text)
    duration_ms = int((time.time() - started_at) * 1000)
    raw_text = model_result.final_output or ""
    success = bool(model_result.success)
    completion_kind = "completed" if success else "failed"

    return SubagentResultEnvelope(
        task_id=request.task_packet.task_id,
        dispatch_id=request.task_packet.dispatch_id,
        success=success,
        completion_kind=completion_kind,
        summary=(raw_text.splitlines()[0][:200] if raw_text else "(无有效输出)"),
        raw_text=raw_text,
        files_modified=list(model_result.files_modified),
        failure_reason=None if success else str(model_result.stop_reason),
        metadata={
            "turns_used": model_result.turns_used,
            "duration_ms": duration_ms,
        },
    )
```

- [ ] **Step 4: 把 `normalize_subagent_result()` 改为 envelope merge**

在 `core/tasks/dispatcher.py` 替换旧函数：

```python
def merge_subagent_result(task: TaskRecord, result: SubagentResultEnvelope) -> TaskRecord:
    status = TaskStatus.COMPLETED if result.success else TaskStatus.FAILED
    return replace(
        task,
        status=status,
        result_summary=result.summary,
        files_modified=list(result.files_modified),
        failure_reason=result.failure_reason,
        result_ref=result.trace_ref or f"result:{result.dispatch_id}",
    )
```

保留一个兼容 wrapper：

```python
def normalize_subagent_result(task_id: str, result) -> TaskRunResult:
    if isinstance(result, SubagentResultEnvelope):
        return TaskRunResult(
            task_id=task_id,
            success=result.success,
            status=TaskStatus.COMPLETED if result.success else TaskStatus.FAILED,
            summary=result.summary,
            files_modified=list(result.files_modified),
            open_questions=list(result.open_questions),
            recommended_next_steps=list(result.recommended_next_steps),
            failure_reason=result.failure_reason,
        )
    ...
```

- [ ] **Step 5: `task_execute` 只把 `summary` 暴露给用户**

在 `core/tools/builtin/task_execute.py`：

```python
sub_result = runtime.run(...)
next_task = merge_subagent_result(task, sub_result)
...
messages=[make_tool_message(context, sub_result.summary)]
```

不要把 `raw_text` 整段写回 tool message。

- [ ] **Step 6: 跑结果测试**

Run:

```bash
pytest tests/session/test_task_execute_tool.py tests/test_task_runtime_models.py tests/test_query_display.py -q
```

Expected:

- PASS。

- [ ] **Step 7: 提交结果回收协议**

```bash
git add core/session/subagent.py core/tasks/dispatcher.py core/tools/builtin/task_execute.py tests/session/test_task_execute_tool.py tests/test_task_runtime_models.py tests/test_query_display.py
git commit -m "feat: merge minimal subagent result envelopes into task state"
```

---

## Task 9: 实现 `fork` 快照与父上下文继承

**Files:**
- Modify: `core/session/state.py`
- Modify: `core/tasks/models.py`
- Modify: `core/tasks/dispatcher.py`
- Modify: `core/session/subagent.py`
- Modify: `tests/session/test_subagent_runtime.py`
- Modify: `tests/test_task_runtime_models.py`

- [ ] **Step 1: 先写 fork 协议测试**

在 `tests/test_task_runtime_models.py` 增加：

```python
def test_fork_dispatch_requires_parent_snapshot_ref_and_delta() -> None:
    task = _task(execution_mode=TaskExecutionMode.FORK_SUBAGENT)
    req = build_dispatch_request(task, dispatch_id="dispatch-1")
    env = compile_dispatch_envelope(task, req, session_state=None)
    env.parent_snapshot_ref = None
    env.delta_context = []
    env.constraints = []
    with pytest.raises(ValueError):
        validate_dispatch_envelope(env, strict=True)
```

在 `tests/session/test_subagent_runtime.py` 增加：

```python
def test_fork_runtime_uses_parent_snapshot(tmp_path) -> None:
    state = SessionState(conversation_messages=[{"role": "user", "content": "Parent context"}])
    state.session_metadata["subagent_snapshots"] = {
        "snap-1": {
            "conversation_messages": list(state.conversation_messages),
            "system_prompt_override": "PARENT",
            "invoked_skills": {},
            "read_file_state": {},
        }
    }
    parent = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent.bind_runtime(session_state=state, skill_registry=None)
    runtime = SubagentRuntime(parent_context=parent)

    request = SubagentRequest(
        task_packet=DispatchEnvelope(
            task_id="task-1",
            dispatch_id="dispatch-1",
            mode="fork",
            agent_role="general",
            title="Analyze parent task",
            objective="Analyze the current task",
            parent_snapshot_ref="snap-1",
            delta_context=["Focus on section 17"],
        ),
        agent_type=SubagentType.GENERAL,
    )
```

这里只需要断言 runtime 会读取 `snap-1` 并把它喂给子会话，不必一次性测试全部 prompt 内容。

- [ ] **Step 2: 跑 fork 测试，确认当前未实现**

Run:

```bash
pytest tests/session/test_subagent_runtime.py tests/test_task_runtime_models.py -q
```

Expected:

- FAIL，当前只有 `fresh` 主路径。

- [ ] **Step 3: 给 `SessionState` 增加快照存储**

在 `core/session/state.py` 用 `session_metadata` 先保守承载，不新建大块新系统：

```python
def _default_subagent_snapshot_store() -> dict[str, dict[str, Any]]:
    return {}

...
subagent_snapshots: dict[str, dict[str, Any]] = field(default_factory=_default_subagent_snapshot_store)
```

如果你不想增公开字段，也可统一落在 `session_metadata["subagent_snapshots"]`，但实现时必须有 helper：

```python
def store_subagent_snapshot(state: SessionState, snapshot_id: str) -> None: ...
def get_subagent_snapshot(state: SessionState, snapshot_id: str) -> dict[str, Any] | None: ...
```

- [ ] **Step 4: 扩展 `TaskRecord` / `DispatchEnvelope` 的 fork 字段来源**

在 `core/tasks/models.py` 的 `TaskRecord` 补齐：

```python
parent_snapshot_ref: str | None = None
delta_context: list[str] = field(default_factory=list)
excluded_parent_context: list[str] = field(default_factory=list)
```

然后 `compile_dispatch_envelope()` 直接映射这些字段。

- [ ] **Step 5: 在 `SubagentRuntime.run()` 实现 fork 上下文继承**

在 `core/session/subagent.py`：

```python
if request.task_packet.mode == "fork":
    snapshot = _load_snapshot(self._parent_context, request.task_packet.parent_snapshot_ref)
    if snapshot is None:
        raise ValueError(f"Missing parent snapshot: {request.task_packet.parent_snapshot_ref}")
    tool_context = ToolUseContext(working_dir=working_dir, max_turns=max_turns)
    engine = SessionEngine(...)
    engine.state.conversation_messages = list(snapshot["conversation_messages"])
    engine.state.system_prompt_override = snapshot.get("system_prompt_override")
    engine.state.invoked_skills = dict(snapshot.get("invoked_skills", {}))
    engine.state.read_file_state = dict(snapshot.get("read_file_state", {}))
```

同时做 2 个防护：

1. 如果父快照来自已经 fork 的子代理，拒绝再次 fork。
2. 如果父快照存在未闭合 tool_use/tool_result 状态，拒绝 fork。

保守做法：当前先要求 `conversation_messages` 内最后一条不是未完成工具轮次；不做复杂修复。

- [ ] **Step 6: 跑 fork 测试**

Run:

```bash
pytest tests/session/test_subagent_runtime.py tests/test_task_runtime_models.py -q
```

Expected:

- PASS。

- [ ] **Step 7: 提交 fork 支持**

```bash
git add core/session/state.py core/tasks/models.py core/tasks/dispatcher.py core/session/subagent.py tests/session/test_subagent_runtime.py tests/test_task_runtime_models.py
git commit -m "feat: add fork snapshot inheritance for subagent runtime"
```

---

## Task 10: 收口控制平面与系统提示

**Files:**
- Modify: `core/prompt/system_context.py`
- Modify: `core/query/loop.py`
- Modify: `core/query/reducers.py`
- Modify: `core/tools/builtin/task_plan.py`
- Modify: `core/tools/builtin/task_execute.py`
- Modify: `tests/test_query_display.py`
- Modify: `tests/test_runtime_control_plane.py`

- [ ] **Step 1: 先写兼容壳和主路径测试**

在 `tests/test_runtime_control_plane.py` 增加：

```python
def test_task_plan_and_task_execute_remain_public_shims() -> None:
    from core.tools.builtin.task_plan import SCHEMA as task_plan_schema
    from core.tools.builtin.task_execute import SCHEMA as task_execute_schema

    assert task_plan_schema["name"] == "task_plan"
    assert task_execute_schema["name"] == "task_execute"
    assert "compat" in task_plan_schema["description"].lower() or "rewrite" in task_plan_schema["description"].lower()
```

在 `tests/test_query_display.py` 增加一条完成态测试，确保 subagent 完成后只展示 summary，不会把 raw_text 直接写进 assistant output。

- [ ] **Step 2: 更新系统提示，明确 V2 运行规则**

在 `core/prompt/system_context.py` 把与 subagent 相关的文案收敛到：

```text
- 对非平凡多步骤任务，先用 task_plan 写入权威 TaskState。
- fresh_subagent 任务必须通过 task_execute 派发。
- 派发前要保证任务边界明确；搜索类任务缺日期/地点/约束时应先补齐或接受 warning。
- local 任务直接用普通工具执行，不要滥用 task_execute。
- 子代理结果只返回摘要给用户；详细原始结论走 task/result 状态。
```

- [ ] **Step 3: 在 `QueryLoop` 和 reducer 中统一 task merge / display 行为**

在 `core/query/loop.py`：

1. 保留主线程工具的 fallback status。
2. 对 subagent 事件优先走 renderer 的 `show_subagent_dispatch()` / `show_subagent_event()`。
3. 完成后只显示 `summary`。

在 `core/query/reducers.py`：

1. `SET_TASK_STATE` 仍然刷新 todo projection。
2. 如后续补了 timeline / result refs，在这里统一更新 run/session state，避免工具直接改对象。

- [ ] **Step 4: 明确兼容壳的职责，不新增第二套公开工具**

短期不要真的暴露新的 `task_sync` / `agent_dispatch` / `task_merge` 工具给模型。

而是在代码里引入 host helper：

```python
def task_sync(...): ...
def agent_dispatch(...): ...
def task_merge(...): ...
```

然后让：

- `task_plan.handle()` 调 `task_sync(...)`
- `task_execute.handle()` 调 `agent_dispatch(...)` 和 `task_merge(...)`

这样后续如果真要公开新接口，主逻辑已经脱离工具 handler。

- [ ] **Step 5: 跑控制平面与显示测试**

Run:

```bash
pytest tests/test_query_display.py tests/test_runtime_control_plane.py tests/session/test_task_execute_tool.py -q
```

Expected:

- PASS。

- [ ] **Step 6: 提交收口改动**

```bash
git add core/prompt/system_context.py core/query/loop.py core/query/reducers.py core/tools/builtin/task_plan.py core/tools/builtin/task_execute.py tests/test_query_display.py tests/test_runtime_control_plane.py
git commit -m "refactor: route task tools through v2 control-plane shims"
```

---

## Final Verification

- [ ] **Step 1: 运行全部 task/subagent 相关测试**

Run:

```bash
pytest \
  tests/session/test_subagent_runtime.py \
  tests/session/test_task_execute_tool.py \
  tests/session/test_task_plan_tool.py \
  tests/test_task_runtime_models.py \
  tests/test_runtime_control_plane.py \
  tests/test_query_display.py \
  tests/test_tool_runtime.py \
  -q
```

Expected:

- 全部 PASS。

- [ ] **Step 2: 做 CLI 手工回归**

Run:

```bash
python 01_agent_loop.py
```

Manual scenario:

```text
帮我看看明天的天气情况, 然后我想从深圳南山区大学城出发, 去大鹏新区桔钓沙玩3天, 但是我不会开车只能乘坐公共交通, 现在需要结合天气情况, 给我安排合适的行程规划(整体行程时间要连贯), 还需要有行程中景点的推荐和热门项目详细说明。
```

Expected:

1. 在真正执行 subagent 前先看到 dispatch preview。
2. preview 中至少出现：日期、起点、交通限制、3 天时长、输出要求。
3. 执行期间能看到天气查询、搜索、整理等关键事件，不再是长时间无输出等待。
4. 最终主线程只收到摘要，不直接喷出长搜索原文。

- [ ] **Step 3: 搜索 under-specified warning 场景**

Manual scenario:

```text
帮我看看天气，然后安排个旅行。
```

Expected:

- `task_plan` 或 dispatch preview 里出现 warning，指出 fresh 搜索任务缺日期/地点/约束。

- [ ] **Step 4: fork 场景手工检查**

Manual scenario:

```text
基于我们刚刚已经读过的 subagent 设计文档，单独开一个子代理只分析 fork 风险，不要重复总结前面已经确认的 phase 0 问题。
```

Expected:

- 走 `fork` 语义。
- preview 中能看到 `parent_snapshot_ref` / `delta_context` 的效果。
- 不会退化成“把所有父上下文全丢给子代理”。

---

## Spec Coverage Review

这份计划对应设计文档的关系：

- 设计文档第 7 节数据模型：Task 3、Task 5、Task 8、Task 9。
- 第 8 节 `fresh` / `fork` 协议：Task 5、Task 9。
- 第 9 节事件流与可见性：Task 2、Task 6、Task 7。
- 第 10 节结果回收：Task 8。
- 第 11 节控制平面：Task 10。
- 第 12 节分阶段方案：Task 1~10 按 Phase 顺序覆盖。
- 第 14 节测试策略：Final Verification + 各任务内测试。

缺口检查：

- 没有遗漏 `warning` 优先 validation。
- 没有遗漏 callback transport。
- 没有遗漏 `raw_text` 结果契约。
- 没有遗漏 `fork` 的父快照和递归防护。

---

## Notes

- 代码里可以保守保留 `TaskRecord` / `SubagentRuntime` 名称，但语义必须按 V2 升级；不要为了追求术语纯粹再制造第二套并存类型。
- `compile_task_packet()` / `normalize_subagent_result()` 可以保留为兼容 wrapper，但主路径必须迁移到 `DispatchEnvelope` / `SubagentResultEnvelope`。
- 如果在 Task 9 中发现当前 `SessionState` 不足以安全表达快照，不要放松 `fork` 边界；先把快照边界补齐。
- 任何“先把用户看见的 panel 打印出来，后面再补协议”的做法都不要接受；展示层必须消费正式协议对象。
