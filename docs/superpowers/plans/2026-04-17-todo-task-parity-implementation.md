# Todo / Task Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前简陋的 `todo` 升级为 session-scoped planning subsystem，使 todo 能在 skill expansion 之后稳定生成与工作流对齐的计划，并通过 targeted reminders 持续保持计划新鲜。

**Architecture:** Phase 2 建立在 Phase 1 的 inline skill runtime 之上，不额外引入 hooks 或完整 task graph。核心改动是三件事：把 todo state 迁入 `SessionState`；把 `todo` tool description 从一句话扩成行为规则；把 `TodoPlanningPolicy` 建立在 Phase 1 已经抽出的 `_apply_batch_control_plane(...)` 之上，增量增加 `todo_replan_required`、plan snapshot、stale turns 逻辑。Renderer 只负责展示，计划规范化与 reminder 判定都放在 tool / policy 层。

**Tech Stack:** Python 3.12、pytest、现有 `SessionState` / `RunState` / `PolicyRunner` / `ToolExecutorRuntime` / Rich renderer

---

## File Structure

### New Files

- `tests/session/test_todo_tool.py`
  Responsibility: 覆盖新的 todo schema、validation、session-state 写入、completed snapshot 规范化。
- `tests/test_todo_planning_policy.py`
  Responsibility: 覆盖 post-skill replan reminder、stale-plan reminder、plan snapshot 注入。
- `tests/test_todo_planning_integration.py`
  Responsibility: 用固定 model stub 回放 `analysis-report` 场景，确保 `workflow_ref="2.5"` 等结构能保留下来。

### Modified Files

- `core/tools/builtin/todo.py`
  Responsibility: 扩写 tool description；强制 `active_form`；允许可选 `workflow_ref`；移除 `failed`；写入 `SessionState.todo_state`。
- `core/session/state.py`
  Responsibility: 增加 `TodoItem`、`TodoState`、`SessionState.todo_state`。
- `core/query/state.py`
  Responsibility: 增加 `todo_replan_required`、`todo_replan_reason`、`assistant_turns_since_todo`。
- `core/query/loop.py`
  Responsibility: 在 `skill_expanded` barrier 后设置 replan flag；成功 todo 写入后清除 flag。
- `core/policy/todo_tracking.py`
  Responsibility: 用 richer planning policy 替换旧的“3 个 batch 提醒一次”的泛化逻辑。
- `core/prompt/system_context.py`
  Responsibility: 把 todo 全局 guidance 升级为短而强的义务说明，并删除任何旧的 delayed skill 叙述。
- `core/tools/runtime.py`
  Responsibility: todo 写入成功后从 `context.session_state.todo_state` 渲染当前计划；如果 plan 刚刚归零但存在 completed snapshot，触发 completion summary。
- `core/ui/renderer.py`
  Responsibility: 优先展示 `active_form`；对空 active plan + `last_completed_items` 渲染完成总结。Phase 2 不改 renderer protocol 签名，只改实现细节。
- `01_agent_loop.py`
  Responsibility: 把旧的 `TodoTrackingPolicy` 装配替换为新的 `TodoPlanningPolicy`。
- `tests/test_query_logging.py`
  Responsibility: policy 接口和 tool result 行为回归。
- `tests/session/test_prompt_assembler.py`
  Responsibility: 继续覆盖 stable prompt cache key，确保 todo guidance 文本变化也会触发新 key。

## Implementation Notes Locked In Before Coding

- `workflow_ref` 是可选字段，只用于结构保真与观察性，任何验证逻辑都不能把它当成必填。
- model-facing status 只保留 `pending` / `in_progress` / `completed`。
- “全部 completed” 与“提交空列表”都规范化为：`todo_state.items = []`，`todo_state.last_completed_items = 最后一版 completed 快照`。
- stale reminder 阈值固定为 `4` 个 assistant turns，不在实现中留 magic number。
- policy 只处理两类 reminder：`post_skill_replan` 与 `todo_stale`；不把它变成通用复杂度分类器。
- Phase 2 直接依赖 Phase 1 已经完成的两项基础能力：`RunState.allowed_tools_override / barrier_reason` 等字段，以及 `core.query.loop._apply_batch_control_plane(...)` helper。未先完成 Phase 1，不允许开始 Phase 2。
- `assistant_turns_since_todo` 的唯一权威维护者是 `QueryLoop`；policy 只读取，不负责 reset，避免计数逻辑分散。

---

### Task 1: 把 Todo 状态迁入 SessionState，并先修正 todo handler 的数据结构

**Files:**
- Create: `tests/session/test_todo_tool.py`
- Modify: `core/session/state.py`
- Modify: `core/tools/builtin/todo.py`

- [ ] **Step 1: 写 failing tests，先锁定新的输入 schema 与 session-state 写入行为**

```python
# tests/session/test_todo_tool.py
from core.session.state import SessionState
from core.tools.context import ToolUseContext


def _make_context(tmp_path, state: SessionState) -> ToolUseContext:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state)
    ctx._set_call_identity(name="todo", call_id="toolu_todo", turn=3)
    return ctx


def test_todo_writes_items_into_session_state(tmp_path) -> None:
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)
    result = handle(
        {
            "items": [
                {
                    "content": "Perform primary analysis",
                    "active_form": "Performing primary analysis",
                    "status": "in_progress",
                    "workflow_ref": "2",
                }
            ]
        },
        ctx,
    )

    assert result.success is True
    assert state.todo_state.items[0].content == "Perform primary analysis"
    assert state.todo_state.items[0].active_form == "Performing primary analysis"
    assert state.todo_state.items[0].workflow_ref == "2"


def test_todo_rejects_missing_active_form(tmp_path) -> None:
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)
    result = handle({"items": [{"content": "Analyze", "status": "pending"}]}, ctx)

    assert result.success is False
    assert result.error == "validation_failed"


def test_todo_normalizes_all_completed_to_completed_snapshot(tmp_path) -> None:
    from core.tools.builtin.todo import handle

    state = SessionState(conversation_messages=[])
    ctx = _make_context(tmp_path, state)
    result = handle(
        {
            "items": [
                {
                    "content": "Verify report completeness",
                    "active_form": "Verifying report completeness",
                    "status": "completed",
                    "workflow_ref": "4",
                }
            ]
        },
        ctx,
    )

    assert result.success is True
    assert state.todo_state.items == []
    assert len(state.todo_state.last_completed_items) == 1
    assert state.todo_state.last_completed_items[0].workflow_ref == "4"
```

- [ ] **Step 2: 运行 todo tool tests，确认当前模块级单例设计不满足这些断言**

Run: `pytest tests/session/test_todo_tool.py -q`

Expected:
- `SessionState` 没有 `todo_state`
- `todo` schema 不接受 `active_form`
- 全 completed 不会被规范化为 completed snapshot

- [ ] **Step 3: 在 session state 和 todo handler 中做最小重构**

```python
# core/session/state.py
@dataclass(slots=True)
class TodoItem:
    content: str
    active_form: str
    status: str
    workflow_ref: str | None = None


@dataclass(slots=True)
class TodoState:
    items: list[TodoItem] = field(default_factory=list)
    last_completed_items: list[TodoItem] = field(default_factory=list)
    last_write_turn: int | None = None
    last_reminder_turn: int | None = None


todo_state: TodoState = field(default_factory=TodoState)
```

```python
# core/tools/builtin/todo.py
VALID_STATUSES = {"pending", "in_progress", "completed"}
MAX_ITEMS = 20

SCHEMA["input_schema"]["properties"]["items"]["items"]["properties"] = {
    "content": {"type": "string", "description": "给用户看的祈使句任务描述"},
    "active_form": {"type": "string", "description": "进行时形式，用于显示当前聚焦工作"},
    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
    "workflow_ref": {"type": "string", "description": "可选的工作流标签，如 2.5", "nullable": True},
}
```

```python
def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    state = context.session_state
    if state is None:
        return ToolResult(output="No session state available", success=False, error="no_state")
    items = [
        TodoItem(
            content=item["content"].strip(),
            active_form=item["active_form"].strip(),
            status=item["status"],
            workflow_ref=(item.get("workflow_ref") or None),
        )
        for item in args.get("items", [])
    ]
    if items and all(item.status == "completed" for item in items):
        state.todo_state.items = []
        state.todo_state.last_completed_items = items
    else:
        state.todo_state.items = items
        state.todo_state.last_completed_items = []
    state.todo_state.last_write_turn = context.turn_count
```

- [ ] **Step 4: 回跑 todo tool tests**

Run: `pytest tests/session/test_todo_tool.py -q`

Expected: `3 passed`

- [ ] **Step 5: 提交 session-scoped todo 状态迁移**

```bash
git add core/session/state.py core/tools/builtin/todo.py tests/session/test_todo_tool.py
git commit -m "feat: move todo state into session state"
```

---

### Task 2: 把 `todo` description 提升为真正的行为约束载体

**Files:**
- Modify: `core/tools/builtin/todo.py`
- Modify: `core/prompt/system_context.py`
- Modify: `tests/session/test_todo_tool.py`
- Modify: `tests/session/test_prompt_assembler.py`

- [ ] **Step 1: 写 failing tests，先卡住 description 和全局 guidance 的关键文案**

```python
# tests/session/test_todo_tool.py
from core.tools.builtin.todo import SCHEMA


def test_todo_schema_description_mentions_workflow_and_verification() -> None:
    description = SCHEMA["description"]

    assert "post-skill" in description.lower()
    assert "workflow" in description.lower()
    assert "verification" in description.lower()
    assert "exactly one" in description.lower()
```

```python
# tests/session/test_prompt_assembler.py
def test_build_stable_includes_stronger_todo_guidance(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assembler = PromptAssembler()

    stable = assembler.build_stable(state, project_root=str(tmp_path))

    assert "多步骤任务必须使用 todo" in stable
    assert "如果 skill 刚展开" in stable
```

- [ ] **Step 2: 运行提示词相关测试，确认当前文案过弱**

Run: `pytest tests/session/test_todo_tool.py tests/session/test_prompt_assembler.py -q`

Expected:
- `SCHEMA["description"]` 仍是一句话
- stable prompt 里还没有 post-skill todo guidance

- [ ] **Step 3: 扩写 `todo` description，同时保持 system prompt 简短**

```python
# core/tools/builtin/todo.py
SCHEMA["description"] = (
    "Rewrite the current session plan for non-trivial multi-step work. "
    "Use this early for tasks that require multiple actions, especially after a skill was just expanded. "
    "Mirror the active workflow instead of collapsing it into 1-2 vague items. "
    "Keep exactly one in_progress item whenever active work exists. "
    "Update the plan as tasks complete or scope changes. "
    "If validation is required, include an explicit verification task. "
    "Preserve meaningful workflow labels such as 2.5 when they are real and relevant."
)
```

```python
# core/prompt/system_context.py
_FRAMEWORK_PROMPT = """\
你是一个 AI 助手，运行在 harness 代理框架中。
判断用户意图：日常对话直接回答，需要操作时使用工具。
多步骤任务必须使用 todo 跟踪计划，保持恰好一个 in_progress。
如果某个 skill 刚刚展开，而任务明显是多步骤，在继续深入执行之前先刷新 todo。
"""
```

- [ ] **Step 4: 回跑提示词测试，并顺带验证 stable prompt cache key 没有回退**

Run: `pytest tests/session/test_todo_tool.py tests/session/test_prompt_assembler.py -q`

Expected:
- description 覆盖 workflow / verification / post-skill
- stable prompt 反映新的 todo guidance
- cache key regression tests 仍通过

- [ ] **Step 5: 提交 prompt / description 升级**

```bash
git add core/tools/builtin/todo.py core/prompt/system_context.py tests/session/test_todo_tool.py tests/session/test_prompt_assembler.py
git commit -m "feat: strengthen todo planning guidance"
```

---

### Task 3: 引入 `todo_replan_required`，把 skill barrier 与第一版计划真正串起来

**Files:**
- Modify: `core/query/state.py`
- Modify: `core/query/loop.py`
- Modify: `tests/test_todo_planning_policy.py`

- [ ] **Step 1: 写 failing tests，先证明 skill barrier 之后会设置 replan flag**

```python
# tests/test_todo_planning_policy.py
from core.query.state import RunState
from core.tools.context import ExecutionBarrier
from core.tools.runtime import ToolBatchResult


def test_skill_expanded_barrier_sets_todo_replan_flag() -> None:
    from core.query.loop import _apply_batch_control_plane

    state = RunState()
    batch = ToolBatchResult(
        tool_results=[],
        files_modified=[],
        tool_names=["skill"],
        injected_messages=[],
        context_patches=[],
        barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
    )

    _apply_batch_control_plane(state, batch)

    assert state.todo_replan_required is True
    assert state.todo_replan_reason == "skill_expanded"
```

- [ ] **Step 2: 运行测试，确认 `RunState` 与 `QueryLoop` 还没有这条语义**

Run: `pytest tests/test_todo_planning_policy.py -q`

Expected:
- `RunState` 缺少 `todo_replan_required`
- `core.query.loop` 没有 `_apply_batch_control_plane`

- [ ] **Step 3: 增加 run flags，并把 batch 后处理抽成 helper**

```python
# core/query/state.py
todo_replan_required: bool = False
todo_replan_reason: str | None = None
assistant_turns_since_todo: int = 0
```

```python
# core/query/loop.py
def _apply_batch_control_plane(state: RunState, batch: ToolBatchResult) -> None:
    for patch in batch.context_patches:
        if patch.allowed_tools is not None:
            state.allowed_tools_override = (
                patch.allowed_tools
                if state.allowed_tools_override is None
                else state.allowed_tools_override & patch.allowed_tools
            )
        if patch.model_override is not None:
            state.model_override = patch.model_override
        if patch.effort_override is not None:
            state.effort_override = patch.effort_override
    if batch.barrier is not None:
        state.barrier_reason = batch.barrier.reason
        if batch.barrier.reason == "skill_expanded":
            state.todo_replan_required = True
            state.todo_replan_reason = "skill_expanded"
    if "todo" in batch.tool_names:
        state.todo_replan_required = False
        state.todo_replan_reason = None
        state.assistant_turns_since_todo = 0
```

- [ ] **Step 4: 回跑 replan flag 测试**

Run: `pytest tests/test_todo_planning_policy.py -q`

Expected:
- `skill_expanded` 会设置 replan flag
- 后续 todo 写入路径可以清除此标志

- [ ] **Step 5: 提交 skill-to-todo 串接逻辑**

```bash
git add core/query/state.py core/query/loop.py tests/test_todo_planning_policy.py
git commit -m "feat: track todo replanning after skill barriers"
```

---

### Task 4: 用 richer planning policy 取代旧的 batch-count nag

**Files:**
- Modify: `core/policy/todo_tracking.py`
- Modify: `01_agent_loop.py`
- Modify: `tests/test_todo_planning_policy.py`
- Modify: `tests/test_query_logging.py`

- [ ] **Step 1: 写 failing tests，锁定两类 reminder 和阈值**

```python
# tests/test_todo_planning_policy.py
from core.policy.todo_tracking import TodoPlanningPolicy
from core.query.state import RunState
from core.session.state import SessionState, TodoItem, TodoState


def test_before_model_call_emits_post_skill_replan_reminder() -> None:
    policy = TodoPlanningPolicy()
    session_state = SessionState(conversation_messages=[])
    run_state = RunState(todo_replan_required=True, todo_replan_reason="skill_expanded")

    messages = policy.before_model_call(session_state, run_state)

    assert len(messages) == 1
    assert "skill 刚刚展开" in messages[0]["content"]


def test_stale_reminder_requires_existing_plan_and_four_turns() -> None:
    policy = TodoPlanningPolicy()
    session_state = SessionState(
        conversation_messages=[],
        todo_state=TodoState(
            items=[
                TodoItem(
                    content="Cross-check findings",
                    active_form="Cross-checking findings",
                    status="in_progress",
                    workflow_ref="2.5",
                )
            ],
            last_write_turn=1,
        ),
    )
    run_state = RunState(assistant_turns_since_todo=4)

    messages = policy.before_model_call(session_state, run_state)

    assert len(messages) == 1
    assert "Cross-check findings" in messages[0]["content"]
    assert "2.5" in messages[0]["content"]
```

- [ ] **Step 2: 运行 policy tests，确认当前 policy 只按 batch 数粗暴提醒**

Run: `pytest tests/test_todo_planning_policy.py tests/test_query_logging.py -q`

Expected:
- `TodoPlanningPolicy` 不存在
- 当前 `TodoTrackingPolicy` 不知道 `todo_replan_required`
- stale reminder 不包含 plan snapshot

- [ ] **Step 3: 在原文件内替换为新的 planning policy，并更新 CLI 装配**

```python
# core/policy/todo_tracking.py
class TodoPlanningPolicy:
    STALE_ASSISTANT_TURNS = 4

    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        if run_state.todo_replan_required:
            return [{
                "role": "user",
                "content": "<system-reminder type=\"post_skill_replan\">某个 skill 刚刚展开。若任务是多步骤，请先刷新 todo，并让计划对齐当前 workflow。</system-reminder>",
            }]

        todo_state = session_state.todo_state
        if todo_state.items and run_state.assistant_turns_since_todo >= self.STALE_ASSISTANT_TURNS:
            snapshot = "\n".join(
                f"- [{item.status}] {item.content}" + (f" ({item.workflow_ref})" if item.workflow_ref else "")
                for item in todo_state.items
            )
            if todo_state.last_reminder_turn == run_state.turn_count:
                return []
            todo_state.last_reminder_turn = run_state.turn_count
            return [{
                "role": "user",
                "content": f"<system-reminder type=\"todo_stale\">\n当前计划可能已过时，请先刷新 todo。\n{snapshot}\n</system-reminder>",
            }]
        return []

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        return []
```

```python
# 01_agent_loop.py
from core.policy.todo_tracking import TodoPlanningPolicy

policy_runner=PolicyRunner([MaxTurnsPolicy(20), TodoPlanningPolicy()]),
```

- [ ] **Step 4: 回跑 policy 与 logging 相关测试**

Run: `pytest tests/test_todo_planning_policy.py tests/test_query_logging.py -q`

Expected:
- 两类 reminder 都按条件触发
- stale reminder 阈值固定为 4 turns
- `01_agent_loop.py` 已切换到新 policy 类名

- [ ] **Step 5: 提交 planning policy 改造**

```bash
git add core/policy/todo_tracking.py 01_agent_loop.py tests/test_todo_planning_policy.py tests/test_query_logging.py
git commit -m "feat: add targeted todo planning reminders"
```

---

### Task 5: 修正 runtime / renderer，让计划展示与完成总结符合新状态模型

**Files:**
- Modify: `core/tools/runtime.py`
- Modify: `core/ui/renderer.py`
- Modify: `tests/session/test_todo_tool.py`
- Modify: `tests/test_runtime_logging.py`

- [ ] **Step 1: 写 failing tests，先锁定 active_form 与 completion summary 的展示行为**

```python
# tests/test_runtime_logging.py
from core.session.state import SessionState, TodoItem, TodoState
from core.tools.context import ToolResult, ToolUseContext
from core.tools.runtime import ToolCall, ToolExecutorRuntime


class FakeRegistryReturningTodoSuccess:
    def is_readonly(self, name: str) -> bool:
        return False

    def execute(self, name: str, args: dict, context: ToolUseContext) -> ToolResult:
        return ToolResult(output="Todo plan updated successfully.", success=True)


class FakeRenderer:
    def __init__(self) -> None:
        self.progress_calls: list[list[TodoItem]] = []
        self.completion_calls: list[tuple[int, int]] = []

    def show_tool_call(self, name: str, args: dict) -> None:
        pass

    def show_tool_result(self, name: str, output: str) -> None:
        pass

    def show_progress(self, items: list[TodoItem]) -> None:
        self.progress_calls.append(items)

    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None:
        self.completion_calls.append((completed, total))


def test_runtime_renders_active_form_after_todo_write(tmp_path) -> None:
    renderer = FakeRenderer()
    context = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
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
```

- [ ] **Step 2: 运行 renderer/runtime tests，确认它们仍依赖模块级 `todo.get_state()`**

Run: `pytest tests/test_runtime_logging.py tests/session/test_todo_tool.py -q`

Expected:
- runtime 仍从 `todo.get_state()` 读取
- renderer 还不知道 `active_form`
- active plan 归零时不会显示 completion summary

- [ ] **Step 3: 让 runtime 从 `SessionState.todo_state` 读取，并增强 renderer**

```python
# core/tools/runtime.py
if call.name == "todo" and results[call.idx].success and self._renderer is not None and not self._display.quiet:
    todo_state = getattr(self._context.session_state, "todo_state", None)
    if todo_state is not None and todo_state.items:
        self._renderer.show_progress(todo_state.items)
    elif todo_state is not None and todo_state.last_completed_items:
        self._renderer.show_completion_summary(
            completed=len(todo_state.last_completed_items),
            total=len(todo_state.last_completed_items),
            elapsed=0.0,
        )
```

```python
# core/ui/renderer.py
def show_progress(self, items: list[Any]) -> None:
    completed = sum(1 for item in items if item.status in ("completed", "COMPLETED"))
    total = len(items)
    for i, item in enumerate(items, 1):
        status = item.status if isinstance(item.status, str) else item.status.value
        if status in ("completed", "COMPLETED"):
            icon = "[green]✅[/green]"
            style = "[dim]"
            label = item.content
        elif status in ("in_progress", "IN_PROGRESS"):
            icon = "[yellow]⚡[/yellow]"
            style = "[bold]"
            label = item.active_form or item.content
        else:
            icon = "[dim]⬜[/dim]"
            style = "[dim]"
            label = item.content
        self._console.print(f"  {icon} {style}{i}. {label}[/]")
```

- [ ] **Step 4: 回跑展示相关测试**

Run: `pytest tests/test_runtime_logging.py tests/session/test_todo_tool.py -q`

Expected:
- in-progress item 展示 `active_form`
- active plan 清空但有 completed snapshot 时出现完成总结

- [ ] **Step 5: 提交 renderer / runtime 展示修复**

```bash
git add core/tools/runtime.py core/ui/renderer.py tests/test_runtime_logging.py tests/session/test_todo_tool.py
git commit -m "feat: render session-scoped todo state and completion summary"
```

---

### Task 6: 用固定 transcript / model stub 做 Phase 2 行为回归

**Files:**
- Create: `tests/test_todo_planning_integration.py`
- Modify: `core/query/loop.py`

- [ ] **Step 1: 写 integration test，直接覆盖用户最关心的 `analysis-report` 场景**

```python
# tests/test_todo_planning_integration.py
from types import SimpleNamespace

from core.policy.base import PolicyRunner
from core.policy.max_turns import MaxTurnsPolicy
from core.policy.todo_tracking import TodoPlanningPolicy
from core.query.recovery import RecoveryManager
from core.session.engine import SessionEngine
from core.tools import registry
from core.tools.context import ToolUseContext
from core.tools.runtime import ToolExecutorRuntime


def write_skill(root, skill_id: str, body: str) -> None:
    skill_dir = root / ".harness" / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def write_analysis_report_fixture(tmp_path) -> None:
    write_skill(
        tmp_path,
        "analysis-report",
        "---\nname: Analysis Report\ndescription: Generate reports\n---\n\nFollow the report workflow.\n",
    )
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")


def response_with_tool(call_id: str, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        reasoning="",
        tool_calls=[{"id": call_id, "name": name, "args": args}],
        content="",
        has_final_text=False,
        to_message=lambda: {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "name": name, "args": args}]},
    )


def response_with_text(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        reasoning="",
        tool_calls=[],
        content=text,
        has_final_text=True,
        to_message=lambda: {"role": "assistant", "content": text},
    )


class StubModelGateway:
    def __init__(self, responses):
        self._responses = list(responses)

    def call_once(self, messages, *, tools):
        return self._responses.pop(0)


def make_engine_with_stubbed_model(tmp_path, responses):
    tool_context = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    return SessionEngine(
        model_gateway=StubModelGateway(responses),
        tool_runtime=ToolExecutorRuntime(registry, tool_context),
        tool_context=tool_context,
        policy_runner=PolicyRunner([MaxTurnsPolicy(20), TodoPlanningPolicy()]),
        recovery=RecoveryManager(),
        tools=registry.schemas(),
        renderer=None,
    )


def test_analysis_report_todo_preserves_skill_workflow_labels(tmp_path) -> None:
    """
    模型桩顺序：
    1. 先调用 skill("analysis-report")
    2. 第二轮调用 todo，items 包含 workflow_ref: 1 / 2 / 2.5 / 3 / 4
    3. 第三轮返回 final text
    """
    write_analysis_report_fixture(tmp_path)
    engine = make_engine_with_stubbed_model(
        tmp_path,
        responses=[
            response_with_tool("toolu_skill", "skill", {"skill": "analysis-report"}),
            response_with_tool(
                "toolu_todo",
                "todo",
                {
                    "items": [
                        {"content": "Collect target inputs", "active_form": "Collecting target inputs", "status": "completed", "workflow_ref": "1"},
                        {"content": "Perform primary analysis", "active_form": "Performing primary analysis", "status": "completed", "workflow_ref": "2"},
                        {"content": "Cross-check findings", "active_form": "Cross-checking findings", "status": "in_progress", "workflow_ref": "2.5"},
                        {"content": "Draft the final report", "active_form": "Drafting the final report", "status": "pending", "workflow_ref": "3"},
                        {"content": "Verify report completeness", "active_form": "Verifying report completeness", "status": "pending", "workflow_ref": "4"},
                    ]
                },
            ),
            response_with_text("final"),
        ],
    )
    engine.submit_user_message("Generate the analysis report")
    assert any(item.workflow_ref == "2.5" for item in engine.state.todo_state.items)
    assert len(engine.state.todo_state.items) >= 5


def test_stale_reminder_does_not_fire_during_normal_two_step_scoping(tmp_path) -> None:
    """
    模型桩顺序：
    1. skill
    2. read_file
    3. read_file
    4. todo
    """
    write_analysis_report_fixture(tmp_path)
    engine = make_engine_with_stubbed_model(
        tmp_path,
        responses=[
            response_with_tool("toolu_skill", "skill", {"skill": "analysis-report"}),
            response_with_tool("toolu_read_1", "read_file", {"path": "a.txt"}),
            response_with_tool("toolu_read_2", "read_file", {"path": "b.txt"}),
            response_with_tool(
                "toolu_todo",
                "todo",
                {
                    "items": [
                        {"content": "Collect scope", "active_form": "Collecting scope", "status": "completed", "workflow_ref": "1"},
                        {"content": "Analyze inputs", "active_form": "Analyzing inputs", "status": "in_progress", "workflow_ref": "2"},
                    ]
                },
            ),
            response_with_text("final"),
        ],
    )
    engine.submit_user_message("Generate the analysis report")
    assert not any("todo_stale" in msg["content"] for msg in engine.state.conversation_messages if msg["role"] == "user")
```

- [ ] **Step 2: 运行 integration tests，找出 assistant-turn 计数或 flag 生命周期问题**

Run: `pytest tests/test_todo_planning_integration.py -q`

Expected:
- 初次运行大概率暴露：
  - `assistant_turns_since_todo` 统计位置不对
  - replan reminder 重复注入
  - `workflow_ref` 渲染未落入最终 session state

- [ ] **Step 3: 只修暴露出的真实缺口，不追加新的抽象层**

```python
# core/query/loop.py
def _note_assistant_turn(state: RunState, model_resp) -> None:
    if not any(tc.get("name") == "todo" for tc in getattr(model_resp, "tool_calls", [])):
        state.assistant_turns_since_todo += 1


_note_assistant_turn(state, model_resp)
if model_resp.tool_calls:
    state.turn_count += 1
    parsed_calls = _parse_tool_calls(model_resp.tool_calls)
    batch = tool_runtime.execute_batch(parsed_calls)
    _apply_batch_control_plane(state, batch)
```

- [ ] **Step 4: 运行 Phase 2 回归测试集**

Run:

```bash
pytest \
  tests/session/test_todo_tool.py \
  tests/test_todo_planning_policy.py \
  tests/test_todo_planning_integration.py \
  tests/test_runtime_logging.py \
  tests/test_query_logging.py \
  tests/session/test_prompt_assembler.py \
  -q
```

Expected:
- todo 写入、policy reminder、renderer、integration 场景全部通过
- `workflow_ref="2.5"` 能进入最终 plan
- stale reminder 不会在正常 1-3 步收集阶段过早触发

- [ ] **Step 5: 提交 Phase 2 行为回归收尾**

```bash
git add core/tools/builtin/todo.py core/session/state.py core/query/state.py core/query/loop.py core/policy/todo_tracking.py core/tools/runtime.py core/ui/renderer.py core/prompt/system_context.py 01_agent_loop.py tests/session/test_todo_tool.py tests/test_todo_planning_policy.py tests/test_todo_planning_integration.py tests/test_runtime_logging.py tests/test_query_logging.py tests/session/test_prompt_assembler.py
git commit -m "feat: align todo planning with inline skill workflow"
```

---

## Self-Review

### Spec Coverage

- `todo` schema 增加 `active_form` / 可选 `workflow_ref`：Task 1。
- 去掉 `failed`，只保留三种状态：Task 1。
- `TodoState` 迁入 `SessionState`：Task 1。
- 更强的 todo description 与 system guidance：Task 2。
- `skill_expanded -> todo_replan_required`：Task 3。
- targeted post-skill reminder：Task 4。
- stale reminder 含 plan snapshot，阈值为 4 turns：Task 4。
- renderer 对空 active plan + completed snapshot 显示总结：Task 5。
- `analysis-report` / `2.5` 场景回归：Task 6。

### Placeholder Scan

- 没有保留 “可选再决定” 的实现点；`workflow_ref` 明确是 optional 但行为已定义。
- 没有把 policy 类名、阈值、renderer 行为留到实现时拍板。
- 每个任务都带了测试入口和预期输出。

### Type / Naming Consistency

- `TodoItem` / `TodoState` 统一放在 `core/session/state.py`。
- `RunState` 统一使用 `todo_replan_required` / `todo_replan_reason` / `assistant_turns_since_todo`。
- `TodoPlanningPolicy` 作为新的主类名；CLI 装配同步更新。
