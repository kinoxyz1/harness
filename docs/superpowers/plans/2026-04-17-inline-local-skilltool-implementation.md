# Inline Local Skill / Runtime Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 inline local `skill` 替换延迟生效的 `activate_skill` 路径，让 skill 展开在当前 query run 内立刻生效，并通过最小 runtime control plane 支持 injected messages、context patch、barrier、显式 skipped tool results。

**Architecture:** Phase 1 不做完整通用控制平面，而是在现有 `ToolResult -> ToolExecutorRuntime -> QueryLoop -> MessageViewBuilder` 主链路上，加入 `ContextPatch`、`ExecutionBarrier`、`InvokedSkillRecord` 三个最小协议对象。`skill` 与 `/skills use` 复用同一套本地展开 helper，但 helper 只负责构造 message 和写 `invoked_skills` 这类结构化状态，不直接写 `conversation_messages`；真正的消息写入由 `QueryLoop` 或命令层在明确时机执行，避免双写。run-scoped patch 则只保留在 `RunState` 中直到本次 query run 结束。

**Tech Stack:** Python 3.12、pytest、dataclasses、现有 `ToolRegistry` / `ToolExecutorRuntime` / `SessionEngine` / `PromptAssembler` / Anthropic messages 协议适配层

---

## File Structure

### New Files

- `core/tools/builtin/skill.py`
  Responsibility: 新的模型可调用 `skill` 工具，校验 skill id，展开本地 skill，返回 injected messages / barrier / context patch。
- `core/skills/runtime.py`
  Responsibility: skill runtime helper，供 `skill` 工具和 `/skills use` 共享；负责构造 `<skill-runtime>` message、记录 invoked skill。`skill_events` 在消息真正写入会话历史的地方记录，而不是在 helper 中提前记录。
- `tests/session/test_skill_tool.py`
  Responsibility: 覆盖 `skill` handler、shared expansion helper、未知 skill / 重复调用 / injected message 结构。
- `tests/test_runtime_control_plane.py`
  Responsibility: 覆盖 runtime barrier、显式 skipped tool result、query loop 注入 message 与 patch 生效。

### Deleted Files

- `core/tools/builtin/activate_skill.py`
  Responsibility: 旧 delayed activation 路径，Phase 1 删除，避免被 `auto_discover()` 继续注册。
- `tests/session/test_activate_skill_tool.py`
  Responsibility: 旧工具测试，随 `activate_skill` 删除。

### Modified Files

- `core/tools/context.py`
  Responsibility: 扩展 `ToolResult`，新增 `ContextPatch`、`ExecutionBarrier`，并提供公共 `bind_runtime(...)` API，替代测试和 engine 对私有属性的直接赋值。
- `core/skills/models.py`
  Responsibility: 增加 `InvokedSkillRecord`。
- `core/session/state.py`
  Responsibility: 增加 `invoked_skills`，保留 `active_skills` 但标记为 deprecated compatibility field。
- `core/query/state.py`
  Responsibility: 增加 run-scoped patch 状态，例如 `allowed_tools_override`、`model_override`、`effort_override`、`barrier_reason`。
- `core/tools/runtime.py`
  Responsibility: 聚合 richer `ToolResult`，识别 barrier，生成 skipped tool results，返回 injected messages / context patches。
- `core/query/loop.py`
  Responsibility: 在 tool batch 后先追加 tool results，再追加 injected messages，再应用 context patches，然后基于 barrier 重新进入下一轮模型调用。
- `core/session/view_builder.py`
  Responsibility: 停止从 `active_skills` 合成消息，改为只读取 `conversation_messages`，并根据 `RunState` 过滤 tools；移除不再使用的 `PromptAssembler` 注入参数。
- `core/prompt/assembler.py`
  Responsibility: 移除主路径上的 active skill injection；修复 stable prompt cache key 把 prompt 文本摘要纳入 key。
- `core/prompt/system_context.py`
  Responsibility: 删除所有 `activate_skill` / “下一轮才加载” 的旧文案，改为要求调用 `skill`。
- `core/session/commands.py`
  Responsibility: `/skills use` 改为 inline expand；`/skills off` 改为明确报错，说明 inline skill 是持久历史事实，不能像旧 state 那样移除。
- `core/session/engine.py`
  Responsibility: 保持 bootstrap 与 shared helper 协同工作，确保 skill registry / state 注入完整。
- `core/tools/builtin/__init__.py`
  Responsibility: 导出 `skill`，移除 `activate_skill`。
- `core/tools/__init__.py`
  Responsibility: 注册表测试联动，无需改逻辑；只需让 `auto_discover()` 发现新的 `skill.py`。
- `tests/test_tool_registry.py`
  Responsibility: 工具集合断言改为 `skill` 替换 `activate_skill`。
- `tests/session/test_engine_commands.py`
  Responsibility: `/skills use` 改验 injected message 与 invoked skill；`/skills off` 改验新语义。
- `tests/session/test_prompt_assembler.py`
  Responsibility: 删除 `build_active_skill_messages()` 主路径断言，增加 stable prompt cache key regression test。
- `tests/session/test_view_builder.py`
  Responsibility: 验证 view builder 不再从 `active_skills` 注入内容；验证 `allowed_tools_override` 过滤。

## Implementation Notes Locked In Before Coding

- `ContextPatch` 在 Phase 1 是最小协议对象，但只保证 `allowed_tools` 真正进入行为路径；`model_override` 与 `effort_override` 先打通 `RunState` 存储，不要求本阶段改变 `AnthropicClient.call()` 参数。
- injected `<skill-runtime>` message 会作为 `system` 消息永久写入 `conversation_messages`；这是 session-visible 事实，不是 run-scoped patch。
- `core/skills/runtime.py` 中的 shared helper 不允许直接写 `conversation_messages`；真正写入会话历史的唯一地方，是 `QueryLoop` 中 `store.extend(batch.injected_messages)` 和 `/skills use` 命令的显式 `state.conversation_messages.append(...)`。
- `/skills use` 走 shared helper，但不返回 barrier，因为它不是 model tool batch 中的一部分。
- Phase 1 在 `core/query/loop.py` 中必须先抽出 `_apply_batch_control_plane(state, batch)` helper；Phase 2 只在这个 helper 上增量添加 `todo_replan_required` 逻辑，不再重新内联一套 patch/barrier 处理代码。
- `/skills off` 不再尝试“撤销 skill”，直接返回明确错误文本，例如：`Inline-loaded skills cannot be deactivated from history; start a new session if you need a clean context.`
- 保留字符预算检查，但移除 `MAX_ACTIVE_SKILLS` 这类旧 activation 概念。新的约束是：inline skill message 的累计内容不能无限增长，`skill` 和 `/skills use` 都必须复用同一个 budget helper。
- `active_skills` 字段暂时保留在 `SessionState` 中，只为兼容旧构造与潜在外部调用；Phase 1 结束后主路径不能再读写它。

---

### Task 1: 建立最小 Runtime 协议对象与状态承载

**Files:**
- Create: `tests/test_runtime_control_plane.py`
- Modify: `core/tools/context.py`
- Modify: `core/skills/models.py`
- Modify: `core/session/state.py`
- Modify: `core/query/state.py`

- [ ] **Step 1: 先写 failing tests，锁定新的数据结构和默认值**

```python
# tests/test_runtime_control_plane.py
from core.query.state import RunState
from core.session.state import SessionState
from core.skills.models import InvokedSkillRecord
from core.tools.context import ContextPatch, ExecutionBarrier, ToolResult, ToolUseContext


def test_tool_result_exposes_runtime_protocol_fields() -> None:
    result = ToolResult(output="ok", success=True)

    assert result.injected_messages == []
    assert result.context_patch is None
    assert result.barrier is None


def test_run_state_starts_without_runtime_overrides() -> None:
    state = RunState()

    assert state.allowed_tools_override is None
    assert state.model_override is None
    assert state.effort_override is None
    assert state.barrier_reason is None


def test_session_state_tracks_invoked_skills() -> None:
    state = SessionState(conversation_messages=[])
    record = InvokedSkillRecord(
        skill_id="analysis-report",
        skill_path=".harness/skills/analysis-report/SKILL.md",
        content_digest="abc123",
        content="Skill body",
        invoked_at_turn=2,
    )

    state.invoked_skills["analysis-report"] = record

    assert state.invoked_skills["analysis-report"].invoked_at_turn == 2


def test_tool_use_context_bind_runtime_sets_public_runtime_handles(tmp_path) -> None:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    state = SessionState(conversation_messages=[])

    ctx.bind_runtime(session_state=state, skill_registry="registry")

    assert ctx.session_state is state
    assert ctx.skill_registry == "registry"
```

- [ ] **Step 2: 运行测试，确认当前代码确实没有这些字段**

Run: `pytest tests/test_runtime_control_plane.py -q`

Expected:
- `ImportError` or `AttributeError` for `InvokedSkillRecord`
- `ToolResult` 缺少 `injected_messages`
- `RunState` 缺少 runtime override 字段
- `ToolUseContext` 缺少 `bind_runtime`

- [ ] **Step 3: 最小实现协议 dataclass 与状态字段**

```python
# core/tools/context.py
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ContextPatch:
    allowed_tools: set[str] | None = None
    model_override: str | None = None
    effort_override: str | None = None


@dataclass(slots=True)
class ExecutionBarrier:
    stop_after_tool: bool = True
    reason: str | None = None


@dataclass
class ToolResult:
    output: str
    success: bool
    error: str | None = None
    truncated: bool = False
    injected_messages: list[dict[str, Any]] = field(default_factory=list)
    context_patch: ContextPatch | None = None
    barrier: ExecutionBarrier | None = None
```

```python
# core/skills/models.py
@dataclass(slots=True)
class InvokedSkillRecord:
    skill_id: str
    skill_path: str
    content_digest: str
    content: str
    invoked_at_turn: int
```

```python
# core/session/state.py
invoked_skills: dict[str, InvokedSkillRecord] = field(default_factory=dict)
# active_skills 保留，但注释写明 deprecated compatibility field
```

```python
# core/query/state.py
allowed_tools_override: set[str] | None = None
model_override: str | None = None
effort_override: str | None = None
barrier_reason: str | None = None
```

```python
# core/tools/context.py
def bind_runtime(self, *, session_state: Any | None = None, skill_registry: Any | None = None) -> None:
    if session_state is not None:
        self._session_state = session_state
    if skill_registry is not None:
        self._skill_registry = skill_registry
```

- [ ] **Step 4: 回跑新增测试**

Run: `pytest tests/test_runtime_control_plane.py -q`

Expected: `4 passed`

- [ ] **Step 5: 提交这组纯数据结构改动**

```bash
git add core/tools/context.py core/skills/models.py core/session/state.py core/query/state.py tests/test_runtime_control_plane.py
git commit -m "feat: add runtime protocol dataclasses for skill expansion"
```

---

### Task 2: 落地 shared skill expansion helper 和新的 `skill` 工具

**Files:**
- Create: `core/skills/runtime.py`
- Create: `core/tools/builtin/skill.py`
- Create: `tests/session/test_skill_tool.py`
- Modify: `core/tools/builtin/__init__.py`
- Modify: `core/session/engine.py`

- [ ] **Step 1: 写 failing tests，先把 shared helper / skill handler 的契约固定下来**

```python
# tests/session/test_skill_tool.py
from pathlib import Path

from core.session.state import SessionState
from core.skills.registry import SkillRegistry
from core.tools.context import ExecutionBarrier, ToolUseContext


def _write_skill(root: Path, skill_id: str, body: str) -> None:
    skill_dir = root / ".harness" / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _make_context(tmp_path: Path, state: SessionState, registry: SkillRegistry) -> ToolUseContext:
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    ctx.bind_runtime(session_state=state, skill_registry=registry)
    ctx._set_call_identity(name="skill", call_id="toolu_skill", turn=1)
    return ctx


def test_skill_tool_returns_injected_runtime_message_and_barrier(tmp_path: Path) -> None:
    from core.tools.builtin.skill import handle

    _write_skill(
        tmp_path,
        "analysis-report",
        "---\nname: Analysis Report\ndescription: Generate reports\n---\n\nFollow the workflow.\n",
    )
    registry = SkillRegistry()
    catalog = registry.discover(tmp_path / ".harness" / "skills", working_dir=tmp_path)
    state = SessionState(conversation_messages=[], skill_catalog=catalog)
    ctx = _make_context(tmp_path, state, registry)

    result = handle({"skill": "analysis-report"}, ctx)

    assert result.success is True
    assert result.barrier == ExecutionBarrier(stop_after_tool=True, reason="skill_expanded")
    assert len(result.injected_messages) == 1
    assert "<skill-runtime>" in result.injected_messages[0]["content"]
    assert "Follow the workflow." in result.injected_messages[0]["content"]
    assert "analysis-report" in state.invoked_skills


def test_skill_tool_rejects_unknown_skill(tmp_path: Path) -> None:
    from core.tools.builtin.skill import handle

    registry = SkillRegistry()
    state = SessionState(conversation_messages=[], skill_catalog={})
    ctx = _make_context(tmp_path, state, registry)

    result = handle({"skill": "missing-skill"}, ctx)

    assert result.success is False
    assert result.error == "not_found"
```

- [ ] **Step 2: 运行 skill handler 测试，确认现在确实没有实现**

Run: `pytest tests/session/test_skill_tool.py -q`

Expected:
- `ModuleNotFoundError` for `core.tools.builtin.skill`
- `ModuleNotFoundError` for `core.skills.runtime`

- [ ] **Step 3: 实现 shared helper 与 `skill` handler**

```python
# core/skills/runtime.py
from core.skills.models import InvokedSkillRecord, SkillContent


def build_skill_runtime_message(skill_id: str, content: SkillContent) -> dict[str, str]:
    lines = [
        "<skill-runtime>",
        f'  <skill id="{skill_id}" source="local-inline">',
        "    <instruction>",
        content.body,
        "    </instruction>",
    ]
    if content.reference_bodies:
        lines.append("    <reference-files>")
        for path, body in content.reference_bodies.items():
            lines.append(f'      <file path="{path}">')
            lines.append(body)
            lines.append("      </file>")
        lines.append("    </reference-files>")
    lines.extend(["  </skill>", "</skill-runtime>"])
    return {"role": "system", "content": "\n".join(lines)}


def ensure_inline_skill_budget(*, state, new_content: str, max_chars: int = 24_000) -> None:
    used_chars = sum(len(record.content) for record in state.invoked_skills.values())
    if used_chars + len(new_content) > max_chars:
        raise ValueError(f"Inline skill budget exceeded: {used_chars + len(new_content)} > {max_chars}")


def apply_skill_invocation(*, state, skill_id: str, content: SkillContent, turn: int) -> dict[str, str]:
    message = build_skill_runtime_message(skill_id, content)
    ensure_inline_skill_budget(state=state, new_content=message["content"])
    state.invoked_skills[skill_id] = InvokedSkillRecord(
        skill_id=skill_id,
        skill_path=str(content.meta.skill_file),
        content_digest=content.content_digest,
        content=message["content"],
        invoked_at_turn=turn,
    )
    return message
```

```python
# core/tools/builtin/skill.py
SCHEMA = {
    "name": "skill",
    "description": "Load a local skill immediately. The skill instructions are injected into context now, and the current tool batch stops so you can re-evaluate the next action with the skill visible.",
    "input_schema": {
        "type": "object",
        "properties": {
            "skill": {"type": "string"},
            "args": {"type": "string"},
        },
        "required": ["skill"],
    },
}


def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    skill_id = args.get("skill", "").strip()
    state = context.session_state
    registry = context.skill_registry
    if not skill_id:
        return ToolResult(output="Missing skill", success=False, error="missing_params")
    if state is None or registry is None:
        return ToolResult(output="Skill runtime unavailable", success=False, error="runtime_unavailable")
    if skill_id not in state.skill_catalog:
        return ToolResult(output=f"Skill not found: {skill_id}", success=False, error="not_found")

    content = registry.load(skill_id)
    message = apply_skill_invocation(
        state=state,
        skill_id=skill_id,
        content=content,
        turn=context.turn_count,
    )
    return ToolResult(
        output=f"Skill loaded: {skill_id}. Re-evaluate your next action using the injected skill guidance.",
        success=True,
        injected_messages=[message],
        barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
    )
```

- [ ] **Step 4: 注册新工具并让 engine 继续注入 registry/state**

```python
# core/tools/builtin/__init__.py
__all__ = [
    "bash",
    "edit_file",
    "find",
    "read_file",
    "skill",
    "todo",
    "write_file",
]
```

Run: `pytest tests/session/test_skill_tool.py tests/test_tool_registry.py -q`

Expected:
- `skill` 注册成功
- `tests/session/test_skill_tool.py` 通过
- `tests/test_tool_registry.py` 中工具集合仍然失败，因为旧工具名还没换掉

- [ ] **Step 5: 更新注册表断言并提交 skill 工具**

```python
# tests/test_tool_registry.py
expected = {"bash", "edit_file", "find", "read_file", "skill", "todo", "write_file"}
```

```bash
git add core/skills/runtime.py core/tools/builtin/skill.py core/tools/builtin/__init__.py tests/session/test_skill_tool.py tests/test_tool_registry.py
git commit -m "feat: add inline local skill tool"
```

---

### Task 3: 改造 ToolExecutorRuntime，使 barrier 真正打断当前 batch

**Files:**
- Modify: `core/tools/runtime.py`
- Modify: `tests/test_runtime_control_plane.py`
- Modify: `tests/test_runtime_logging.py`

- [ ] **Step 1: 写 failing tests，先证明当前 runtime 会错误执行 barrier 后的后续调用**

```python
# tests/test_runtime_control_plane.py
from core.tools import ToolRegistry
from core.tools.context import ExecutionBarrier, ToolResult, ToolUseContext
from core.tools.runtime import ToolCall, ToolExecutorRuntime


class _BarrierTool:
    SCHEMA = {"name": "skill", "description": "skill", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = False
    ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

    @staticmethod
    def handle(args, context):
        return ToolResult(
            output="skill expanded",
            success=True,
            injected_messages=[{"role": "system", "content": "<skill-runtime>expanded</skill-runtime>"}],
            barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
        )


class _TodoTool:
    SCHEMA = {"name": "todo", "description": "todo", "input_schema": {"type": "object", "properties": {}, "required": []}}
    READONLY = False
    ANNOTATIONS = {"readonly": False, "destructive": False, "idempotent": True, "concurrency_safe": False}

    @staticmethod
    def handle(args, context):
        raise AssertionError("todo should have been skipped after skill barrier")


def test_runtime_returns_explicit_skipped_result_after_skill_barrier(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(_BarrierTool)
    reg.register(_TodoTool)
    ctx = ToolUseContext(working_dir=str(tmp_path), max_turns=20)
    runtime = ToolExecutorRuntime(reg, ctx)

    batch = runtime.execute_batch([
        ToolCall(idx=0, name="skill", call_id="toolu_skill", args={}),
        ToolCall(idx=1, name="todo", call_id="toolu_todo", args={}),
    ])

    assert batch.barrier == ExecutionBarrier(stop_after_tool=True, reason="skill_expanded")
    assert batch.tool_results[1]["content"].startswith("(skipped: superseded by skill_expanded barrier")
    assert batch.injected_messages == [{"role": "system", "content": "<skill-runtime>expanded</skill-runtime>"}]
```

- [ ] **Step 2: 运行 runtime tests，确认它们在当前实现下失败**

Run: `pytest tests/test_runtime_control_plane.py tests/test_runtime_logging.py -q`

Expected:
- `ToolBatchResult` 没有 `barrier` / `injected_messages`
- `todo` 实际被执行，触发 `AssertionError`

- [ ] **Step 3: 修改 runtime 的返回结构和批处理逻辑**

```python
# core/tools/runtime.py
@dataclass(slots=True)
class ToolBatchResult:
    tool_results: list[dict[str, Any]]
    files_modified: list[str]
    tool_names: list[str]
    injected_messages: list[dict[str, Any]]
    context_patches: list[ContextPatch]
    barrier: ExecutionBarrier | None
```

```python
def execute_batch(self, tool_calls: list[ToolCall]) -> ToolBatchResult:
    ordered_results: dict[int, ToolResult] = {}
    injected_messages: list[dict[str, Any]] = []
    context_patches: list[ContextPatch] = []
    barrier: ExecutionBarrier | None = None

    if any(call.name == "skill" for call in tool_calls):
        for pos, call in enumerate(tool_calls):
            result = self._run_single(call)
            ordered_results[call.idx] = result
            injected_messages.extend(result.injected_messages)
            if result.context_patch is not None:
                context_patches.append(result.context_patch)
            if result.barrier is not None and result.barrier.stop_after_tool:
                barrier = result.barrier
                for skipped in tool_calls[pos + 1:]:
                    ordered_results[skipped.idx] = ToolResult(
                        output="(skipped: superseded by skill_expanded barrier; re-issue after re-evaluation if still needed)",
                        success=False,
                        error="skipped",
                    )
                break
    else:
        # 保留原有 readonly/write 分批逻辑，并在聚合时填充 injected_messages / context_patches
```

- [ ] **Step 4: 回跑 runtime tests，确认 barrier 语义成立**

Run: `pytest tests/test_runtime_control_plane.py tests/test_runtime_logging.py -q`

Expected:
- barrier case 通过
- 旧 logging 测试仍通过
- runtime 对 skipped tool call 产出显式 `tool` 消息

- [ ] **Step 5: 提交 runtime control plane 最小实现**

```bash
git add core/tools/runtime.py tests/test_runtime_control_plane.py tests/test_runtime_logging.py
git commit -m "feat: stop tool batches on skill barrier"
```

---

### Task 4: 把 QueryLoop / ViewBuilder / PromptAssembler 接到新的运行时语义上

**Files:**
- Modify: `core/query/loop.py`
- Modify: `core/session/view_builder.py`
- Modify: `core/prompt/assembler.py`
- Modify: `core/prompt/system_context.py`
- Modify: `tests/session/test_view_builder.py`
- Modify: `tests/session/test_prompt_assembler.py`
- Modify: `tests/test_runtime_control_plane.py`

- [ ] **Step 1: 写 failing tests，锁定“下一轮模型调用必须看到 injected skill message，同时 tools 已按 run patch 过滤”的行为**

```python
# tests/test_runtime_control_plane.py
from types import SimpleNamespace

from core.query.loop import QueryLoop
from core.query.recovery import RecoveryDecision
from core.session.state import SessionState
from core.session.store import SessionStore
from core.session.view_builder import MessageViewBuilder
from core.tools.context import ExecutionBarrier
from core.tools.runtime import ToolBatchResult


class _CapturingModelGateway:
    def __init__(self):
        self.calls = []
        self._responses = [
            SimpleNamespace(
                reasoning="",
                tool_calls=[{"id": "toolu_skill", "name": "skill", "args": {"skill": "analysis-report"}}],
                content="",
                has_final_text=False,
                to_message=lambda: {"role": "assistant", "content": "", "tool_calls": [{"id": "toolu_skill", "name": "skill", "args": {"skill": "analysis-report"}}]},
            ),
            SimpleNamespace(
                reasoning="",
                tool_calls=[],
                content="final",
                has_final_text=True,
                to_message=lambda: {"role": "assistant", "content": "final"},
            ),
        ]

    def call_once(self, messages, *, tools):
        self.calls.append({"messages": messages, "tools": tools})
        return self._responses.pop(0)


class _BatchRuntime:
    def execute_batch(self, tool_calls):
        return ToolBatchResult(
            tool_results=[{"role": "tool", "tool_call_id": "toolu_skill", "content": "Skill loaded: analysis-report"}],
            files_modified=[],
            tool_names=["skill"],
            injected_messages=[{"role": "system", "content": "<skill-runtime><skill id=\"analysis-report\">expanded</skill></skill-runtime>"}],
            context_patches=[],
            barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),
        )


class _NoopPolicyRunner:
    def before_model_call(self, context, state):
        return []

    def after_tool_batch(self, context, state, batch):
        return []

    def should_stop(self, context, state):
        return None


class _NoopRecovery:
    def handle(self, model_resp, state):
        return RecoveryDecision(should_continue=False, follow_up_messages=[])


def test_query_loop_reenters_model_after_skill_barrier(tmp_path) -> None:
    session = SessionState(conversation_messages=[{"role": "system", "content": "stable"}])
    store = SessionStore(session)
    gateway = _CapturingModelGateway()
    view_builder = MessageViewBuilder(tools=[{"name": "skill"}, {"name": "todo"}])
    result = QueryLoop().run(
        session_state=session,
        store=store,
        view_builder=view_builder,
        prompt_assembler=None,
        model_gateway=gateway,
        tool_runtime=_BatchRuntime(),
        tool_context=None,
        policy_runner=_NoopPolicyRunner(),
        recovery=_NoopRecovery(),
        renderer=None,
    )

    assert result.final_output == "final"
    assert len(gateway.calls) == 2
    assert any("<skill-runtime>" in msg.get("content", "") for msg in gateway.calls[1]["messages"])
```

```python
# tests/session/test_view_builder.py
from core.query.state import RunState


def test_view_builder_filters_tools_from_run_state(tmp_path: Path) -> None:
    state = SessionState(conversation_messages=[{"role": "system", "content": "stable"}])
    run_state = RunState(allowed_tools_override={"todo"})
    builder = MessageViewBuilder(
        tools=[
            {"name": "skill", "description": "skill", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "todo", "description": "todo", "input_schema": {"type": "object", "properties": {}, "required": []}},
        ]
    )

    view = builder.build(state, run_state=run_state)

    assert [tool["name"] for tool in view.tools] == ["todo"]
```

- [ ] **Step 2: 运行这些测试，确认当前 query/view 代码还没有接住这些语义**

Run: `pytest tests/test_runtime_control_plane.py tests/session/test_view_builder.py tests/session/test_prompt_assembler.py -q`

Expected:
- `MessageViewBuilder.build()` 不接受 `run_state`
- `PromptAssembler.build_active_skill_messages()` 相关旧测试仍是主路径
- `QueryLoop` 不会消费 `batch.injected_messages`
- `QueryLoop` 还没有 `_apply_batch_control_plane`

- [ ] **Step 3: 最小改造 QueryLoop、ViewBuilder、PromptAssembler**

```python
# core/session/view_builder.py
class MessageViewBuilder:
    def __init__(self, tools: list[dict[str, Any]] | None = None):
        self._tools = tools

    def build(self, state: SessionState, run_state=None) -> MessageView:
        messages = list(state.conversation_messages)
        tools = self._tools
        if run_state is not None and run_state.allowed_tools_override is not None and tools is not None:
            tools = [tool for tool in tools if tool.get("name") in run_state.allowed_tools_override]
        return MessageView(messages=messages, tools=tools)
```

```python
# core/query/loop.py
from core.skills.models import SkillEvent


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


view = view_builder.build(session_state, run_state=state)
# QueryLoop 直接信任 view.tools 的过滤结果，不再自己二次过滤 active_tools
model_resp = model_gateway.call_once(view.messages, tools=view.tools)
state.last_model_response = model_resp
store.append(model_resp.to_message())
parsed_calls = _parse_tool_calls(model_resp.tool_calls)
batch = tool_runtime.execute_batch(parsed_calls)
store.extend(batch.tool_results)
if batch.injected_messages:
    store.extend(batch.injected_messages)
    skill_calls = [call for call in parsed_calls if call.name == "skill"]
    for call in skill_calls:
        session_state.skill_events.append(
            SkillEvent(
                skill_id=call.args["skill"],
                action="activated",
                source="model_tool_call",
                conversation_index=len(session_state.conversation_messages) - 1,
            )
        )
_apply_batch_control_plane(state, batch)
after_messages = policy_runner.after_tool_batch(session_state, state, batch)
if after_messages:
    store.extend(after_messages)
stop_reason = policy_runner.should_stop(session_state, state)
if stop_reason == "max_turns" and state.stop_reason != "max_turns":
    state.stop_reason = "max_turns"
    store.append({"role": "user", "content": "你已达到迭代安全上限。请基于当前已收集的信息给出最终回复。"})
    continue
if batch.barrier is not None:
    continue
```

```python
# core/prompt/assembler.py
def _stable_cache_key(state: SessionState, *, project_root: str | None) -> str:
    system_prompt = get_system_context(project_root=project_root)
    digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    revision = state.skills_revision or "no-skills"
    return f"stable_system_prompt:{revision}:{digest}"


def build_active_skill_messages(self, state: SessionState) -> list[dict[str, str]]:
    return []
```

```python
# core/prompt/system_context.py
_FRAMEWORK_PROMPT = """\
你是一个 AI 助手，运行在 harness 代理框架中。
判断用户意图：日常对话直接回答，需要操作时使用工具。
多步骤任务必须使用 todo 跟踪计划，保持恰好一个 in_progress。

## Skills

系统提示词中包含 <available-skills> 目录。
如果任务匹配某个 skill，应先调用 skill 工具立即加载它，再基于已展开的 skill 重新评估下一步。
"""
```

- [ ] **Step 4: 用 targeted tests 验证 prompt cache key、view filtering、post-barrier re-entry**

Run: `pytest tests/test_runtime_control_plane.py tests/session/test_view_builder.py tests/session/test_prompt_assembler.py -q`

Expected:
- 第二次模型调用看得到 `<skill-runtime>`
- `MessageViewBuilder` 不再依赖 `active_skills`
- stable prompt cache key 在 prompt 文本变更时不同

- [ ] **Step 5: 提交 query/view/prompt 主链路改造**

```bash
git add core/query/loop.py core/session/view_builder.py core/session/engine.py core/prompt/assembler.py core/prompt/system_context.py tests/test_runtime_control_plane.py tests/session/test_view_builder.py tests/session/test_prompt_assembler.py
git commit -m "feat: wire query loop to inline skill runtime messages"
```

---

### Task 5: 迁移 `/skills use`，并明确 `/skills off` 新语义

**Files:**
- Modify: `core/session/commands.py`
- Modify: `tests/session/test_engine_commands.py`

- [ ] **Step 1: 写 failing tests，锁定用户命令的新行为**

```python
# tests/session/test_engine_commands.py
def test_handle_command_use_injects_skill_runtime_message(tmp_path: Path) -> None:
    write_skill(tmp_path, "analysis-report", "Analysis Report", "Generate reports", "Use the workflow.")

    engine = make_engine(tmp_path)
    engine.bootstrap()

    result = engine.handle_command("/skills use analysis-report")

    assert "loaded" in result.lower() or "activated" in result.lower()
    assert "analysis-report" in engine.state.invoked_skills
    assert any("<skill-runtime>" in m.get("content", "") for m in engine.state.conversation_messages if m["role"] == "system")
    assert engine.state.active_skills == {}


def test_handle_command_off_reports_inline_skills_cannot_be_removed(tmp_path: Path) -> None:
    write_skill(tmp_path, "analysis-report", "Analysis Report", "Generate reports", "Use the workflow.")
    engine = make_engine(tmp_path)
    engine.bootstrap()
    engine.handle_command("/skills use analysis-report")

    result = engine.handle_command("/skills off analysis-report")

    assert "cannot be deactivated" in result.lower()


def test_handle_command_use_respects_inline_skill_budget(tmp_path: Path) -> None:
    big_body = "x" * 20_000
    write_skill(tmp_path, "skill-a", "Skill A", "A", big_body)
    write_skill(tmp_path, "skill-b", "Skill B", "B", big_body)
    engine = make_engine(tmp_path)
    engine.bootstrap()

    first = engine.handle_command("/skills use skill-a")
    second = engine.handle_command("/skills use skill-b")

    assert "loaded" in first.lower()
    assert "budget" in second.lower() or "exceeded" in second.lower()
```

- [ ] **Step 2: 运行命令测试，确认当前实现仍然写 `active_skills`**

Run: `pytest tests/session/test_engine_commands.py -q`

Expected:
- `/skills use` 仍写入 `active_skills`
- `/skills off` 仍尝试移除旧状态

- [ ] **Step 3: 让命令层复用 shared helper，而不是复制旧逻辑**

```python
# core/session/commands.py
from core.skills.runtime import apply_skill_invocation
from core.skills.models import SkillEvent


if subcmd == "use" and len(parts) == 3:
    skill_id = parts[2]
    if skill_id not in state.skill_catalog:
        return CommandResult(True, f"Skill not found: {skill_id}")
    content = registry.load(skill_id)
    message = apply_skill_invocation(
        state=state,
        skill_id=skill_id,
        content=content,
        turn=0,
    )
    state.conversation_messages.append(message)
    state.skill_events.append(
        SkillEvent(
            skill_id=skill_id,
            action="activated",
            source="user_command",
            conversation_index=len(state.conversation_messages) - 1,
        )
    )
    return CommandResult(True, f"Loaded skill inline: {skill_id}")


if subcmd == "off" and len(parts) == 3:
    return CommandResult(
        True,
        "Inline-loaded skills cannot be deactivated from history; start a new session if you need a clean context.",
    )
```

- [ ] **Step 4: 回跑 engine command tests**

Run: `pytest tests/session/test_engine_commands.py -q`

Expected:
- `/skills use` 写入 injected message + invoked skill record
- `/skills off` 输出明确的不可撤销说明
- `/skills use` 若累计注入过大，会收到预算超限错误，而不是无限扩张上下文

- [ ] **Step 5: 提交用户命令迁移**

```bash
git add core/session/commands.py tests/session/test_engine_commands.py
git commit -m "feat: migrate skills command to inline expansion"
```

---

### Task 6: 删除旧 `activate_skill` 路径并做全量回归

**Files:**
- Delete: `core/tools/builtin/activate_skill.py`
- Delete: `tests/session/test_activate_skill_tool.py`
- Modify: `tests/test_tool_registry.py`
- Modify: `01_agent_loop.py`

- [ ] **Step 1: 删除旧工具文件和旧测试文件**

```diff
- core/tools/builtin/activate_skill.py
- tests/session/test_activate_skill_tool.py
```

- [ ] **Step 2: 确认没有任何剩余引用**

Run: `rg -n "activate_skill|build_active_skill_messages|active_skills" core tests 01_agent_loop.py`

Expected:
- 不再出现 `activate_skill` 工具引用
- `build_active_skill_messages` 最多只剩 deprecated 空实现
- `active_skills` 只剩 compatibility field / 注释

- [ ] **Step 3: 运行 Phase 1 回归测试集**

Run:

```bash
pytest \
  tests/test_tool_registry.py \
  tests/test_runtime_control_plane.py \
  tests/test_runtime_logging.py \
  tests/session/test_skill_tool.py \
  tests/session/test_engine_commands.py \
  tests/session/test_prompt_assembler.py \
  tests/session/test_view_builder.py \
  tests/test_protocol.py \
  tests/test_loop.py \
  -q
```

Expected:
- 所有 Phase 1 相关测试通过
- `skill` 已成为唯一 skill tool
- tool results 对 skipped calls 使用显式 runtime 语义，而不是依赖 protocol 自动 `(cancelled)`

- [ ] **Step 4: 手工 smoke 一次 CLI 主路径**

Run: `python 01_agent_loop.py`

Manual script:
1. 输入一个会触发 skill 的任务。
2. 确认模型调用的是 `skill`，不是 `activate_skill`。
3. 确认下一轮前出现 `<skill-runtime>` 相关效果，而不是“下一轮再加载”的旧行为。

Expected:
- 终端日志能看到 skill expansion
- 后续 tool calls 在 skill 之后重新规划

- [ ] **Step 5: 提交删除旧路径的收尾变更**

```bash
git add core/tools/builtin/__init__.py core/session/commands.py core/session/engine.py core/prompt/system_context.py core/tools/builtin/skill.py core/skills/runtime.py core/query/loop.py core/tools/runtime.py core/session/view_builder.py core/prompt/assembler.py core/tools/context.py core/skills/models.py core/session/state.py core/query/state.py tests/test_tool_registry.py tests/test_runtime_control_plane.py tests/test_runtime_logging.py tests/session/test_skill_tool.py tests/session/test_engine_commands.py tests/session/test_prompt_assembler.py tests/session/test_view_builder.py 01_agent_loop.py
git rm core/tools/builtin/activate_skill.py tests/session/test_activate_skill_tool.py
git commit -m "refactor: replace delayed skill activation with inline skill runtime"
```

---

## Self-Review

### Spec Coverage

- `skill` 替换 `activate_skill`：Task 2、Task 6。
- injected `<skill-runtime>` message：Task 2、Task 4。
- `InvokedSkillRecord`：Task 1、Task 2、Task 5。
- `ToolResult` / `ToolBatchResult` 扩展：Task 1、Task 3。
- barrier 停止当前 batch：Task 3。
- explicit skipped tool results：Task 3、Task 6。
- `QueryLoop` 在 barrier 后重入模型：Task 4。
- `MessageViewBuilder` 使用 run-scoped patch：Task 4。
- stable prompt cache key 修复：Task 4。
- `/skills use` 迁移与 `/skills off` 新语义：Task 5。
- 删除旧 `activate_skill` 主路径：Task 6。

### Placeholder Scan

- 没有使用 `TBD`、`TODO`、`implement later`。
- 每个任务都给了明确文件、测试命令、预期结果。
- 对 `/skills off` 的新语义已做明确选择，没有把决定留到实现时。

### Type / Naming Consistency

- 统一使用 `skill` 作为工具名，不再混用 `SkillTool` / `activate_skill`。
- 统一使用 `InvokedSkillRecord`、`ContextPatch`、`ExecutionBarrier`。
- `RunState` 中统一使用 `allowed_tools_override` / `model_override` / `effort_override` / `barrier_reason`。
