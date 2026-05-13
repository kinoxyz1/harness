# Subagent Runtime 重新设计

## 文档信息

- **状态**: 草案（Draft）
- **创建日期**: 2026-05-12
- **作者**: 基于对 harness 源码的深入分析、与 emperor-agent 的对比研究、以及多次讨论后整理
- **前置文档**: `2026-05-10-task-subagent-runtime-design.md`, `2026-05-11-task-subagent-runtime-v2-design.md`

---

## 1. 为什么需要这份文档

harness 的 subagent 系统经过多轮迭代，目前处于一个"设计意图清晰但执行链路断裂"的状态。核心痛点是：

1. **黑盒执行**：用户调用 `task_execute` 后，子代理的运行过程完全不可见，只能等待最终结果。
2. **阻塞串行**：`SubagentRuntime.run()` 是同步阻塞调用，无法并发派遣多个子代理。
3. **数据丢失**：planner 填写的结构化字段在传输过程中大量丢失，子代理实际看到的输入远少于设计意图。
4. **字段死亡**：`TaskRecord` 定义了 20+ 个字段，但超过一半从未被写入或读取。

本文档旨在：**梳理当前问题、吸收外部优秀实践（emperor-agent、Claude Code 等）、提出可落地的修复方案**。

---

## 2. 当前实现分析（代码级）

### 2.1 现有链路总览

```
用户请求 → task_plan (Schema: 5字段) → normalize_task_payload (20+字段, 大多为空)
         → TaskState (会话级存储)
         → task_execute (agent_type 被覆盖为 GENERAL)
         → compile_task_packet (编译 TaskPacket: 13字段)
         → SubagentRuntime.run()
         → _render_fresh_packet (只渲染 6 个字段)
         → engine.submit_user_message() (同步阻塞)
         → normalize_subagent_result (open_questions 永远为空)
         → 更新 TaskState → 返回摘要
```

### 2.2 关键代码缺陷（已确认）

#### Bug 1: `agent_type` 被强制覆盖为 GENERAL

**位置**: `core/tools/builtin/task_execute.py:70`

```python
packet = compile_task_packet(task)  # 这里正确编译了 task.agent_type
runtime = SubagentRuntime(parent_context=context)
sub_result = runtime.run(
    SubagentRequest(
        task_packet=packet,
        agent_type=SubagentType.GENERAL,  # ← 永远覆盖为 GENERAL
        preloaded_skill_ids=list(task.required_skills),
    )
)
```

**影响**: planner 指定 `agent_type: "explore"` 或 `"plan"` 永远不会生效。`EXPLORE_AGENT` 和 `PLAN_AGENT` 虽然在 `DEFAULT_SUBAGENTS` 中注册，但实际执行路径下是**死代码**。

**严重度**: P1 — 导致子代理类型系统形同虚设。

---

#### Bug 2: `task_context` 编译后丢失

**位置**: `core/tasks/dispatcher.py:15`（编译正确） / `core/session/subagent.py:169-184`（渲染遗漏）

`compile_task_packet()` 正确地把 `task.inputs` 编译到 `TaskPacket.task_context`：

```python
def compile_task_packet(task: TaskRecord) -> TaskPacket:
    return TaskPacket(
        # ...
        task_context=list(task.inputs),  # ← 正确编译
        # ...
    )
```

但 `_render_fresh_packet()` 完全遗漏了 `task_context`：

```python
def _render_fresh_packet(packet: TaskPacket) -> str:
    sections = [
        f"Task: {packet.title}",
        "",
        "Directive:",
        packet.directive,
    ]
    if packet.known_facts:  # ← 渲染了 known_facts
        ...
    if packet.out_of_scope:  # ← 渲染了 out_of_scope
        ...
    # ← 这里缺少：if packet.task_context:
    return "\n".join(sections)
```

**影响**: planner 通过 `inputs` 传递给子代理的上下文信息（如文件路径、关键参数）永远不会出现在子代理的 prompt 中。

**严重度**: P1 — 数据丢失 bug。

---

#### Bug 3: 事件批处理后回放，不是实时流

**位置**: `core/tools/runtime.py:137-142`

```python
if self._renderer is not None and not self._display.quiet:
    for call, result in zip(ordered_calls, ordered_results):
        if not self._should_render_generic_tool_event(call.name):
            continue
        self._renderer.show_tool_call(call.name, call.args)
        self._renderer.show_tool_result(call.name, self._first_content(result))
```

**影响**: 整批工具全部执行完成后，才统一回放 `show_tool_call/show_tool_result`。对只读工具，它们是并行执行的——用户看到的是"所有工具都完成后一次性展示结果"，而不是"工具 A 已开始、工具 B 已开始"的实时反馈。

子代理内部设置了 `display=RunDisplayOptions(quiet=True)`，所以连这个事后回放都被静默了。子代理对用户来说是**完全的黑盒**。

**严重度**: P1 — 用户体验差，无法感知长任务的进展。

---

#### Bug 4: `tools=` 参数未传给 SessionEngine

**位置**: `core/session/subagent.py:241-248`

```python
engine = SessionEngine(
    model_gateway=ModelGateway(self._llm_factory()),
    tool_runtime=ToolExecutorRuntime(sub_registry, tool_context, display=RunDisplayOptions(quiet=True)),
    tool_context=tool_context,
    policy_runner=PolicyRunner([MaxTurnsPolicy(max_turns)]),
    recovery=RecoveryManager(),
    view_builder=MessageViewBuilder(tools=sub_schemas),  # ← 传给了 view_builder
    # ← 但没有传 tools=sub_schemas 给 SessionEngine 本身
)
```

`SessionEngine.__init__` 接受 `tools=None`（默认值），存储为 `self._tools = None`。这个 `None` 会传给 `QueryLoop.run(tools=None)`。

**分析**: 工具 schema 实际上通过 `view_builder=MessageViewBuilder(tools=sub_schemas)` 传给了 `MessageViewBuilder`。`QueryLoop.run()` 中 `active_tools = view.tools` 来自 view_builder，不是来自 `tools` 参数。所以工具**可能**通过 view_builder 路径到达了 API 调用，但存在两条不一致的路径。

**严重度**: P2 — 不是"完全无法调用工具"，而是"路径不一致，有隐患"。

---

#### Bug 5: `task_plan` Schema 太窄，导致上游数据无法流入

**位置**: `core/tools/builtin/task_plan.py:21-47`

```python
"input_schema": {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "subject": {"type": "string"},
                    "goal": {"type": "string"},
                    "status": {"type": "string", "enum": [...]},
                    "execution_mode": {"type": "string", "enum": [...]},
                },
                "required": ["subject", "goal"],
            },
        }
    },
}
```

**影响**: LLM planner 只能设置 5 个字段（task_id, subject, goal, status, execution_mode）。`TaskRecord` 的其余 15+ 个字段（inputs, known_context, expected_output, done_criteria, agent_type, allowed_tools 等）**永远为空**。

这是比 Bug 2 更根因的问题：`task_context` 渲染遗漏只是最后一环，上游根本就没数据可传。

**严重度**: P0 — 结构性缺陷，导致整个结构化输入系统失效。

---

#### Bug 6: 大量字段死亡

通过代码审查确认，以下字段存在于数据模型但从未在代码中被写入或读取：

| 字段 | 定义位置 | 写入位置 | 读取位置 | 状态 |
|------|----------|----------|----------|------|
| `TaskRecord.artifacts` | models.py | 无 | 无 | 死 |
| `TaskRecord.owner` | models.py | 无 | 无 | 死 |
| `TaskRecord.blocked_reason` | models.py | 无 | 无 | 死 |
| `TaskRecord.updated_at_turn` | models.py | 无 | 无 | 死 |
| `TaskRecord.last_projection_turn` | models.py | 无 | 无 | 死 |
| `TaskRunResult.open_questions` | models.py | 无 | 无 | 死 |
| `TaskRunResult.recommended_next_steps` | models.py | 无 | 无 | 死 |
| `TaskRecord.active_form` | models.py | task_plan 可传入 | 无读取 | 半死 |
| `TaskRecord.depends_on` | models.py | task_plan 可传入 | task_execute 不检查 | 装饰性 |
| `TaskRecord.allowed_tools` | models.py | task_plan 可传入 | SubagentRuntime 忽略 | 死 |
| `TaskRecord.write_scope` | models.py | task_plan 可传入 | SubagentRuntime 忽略 | 死 |
| `SubagentRequest.description` | subagent.py | 可设置 | run() 从不读取 | 死 |
| `planner_runtime.previous` | planner_runtime.py | 参数传入 | normalize_task_payload 从不使用 | 死 |

**严重度**: P3 — 维护负担，认知噪音。

---

#### Bug 7: `FORK` 模式完全未实现

**位置**: `core/session/subagent.py:23-32`

```python
class SubagentType(str, Enum):
    EXPLORE = "explore"
    PLAN = "plan"
    GENERAL = "general"
    FORK = "fork"  # ← 枚举存在

class SubagentContextMode(str, Enum):
    FRESH = "fresh"
    FORK = "fork"  # ← 枚举存在
```

但 `DEFAULT_SUBAGENTS` 没有注册 `FORK` 的定义，且 `SubagentRuntime.run()` 明确拒绝非 FRESH 模式：

```python
if definition.context_mode is not SubagentContextMode.FRESH:
    raise ValueError(f"Unsupported context mode in V1 runtime: ...")
```

**严重度**: P2 — 协议层支持但运行时拒绝。

---

### 2.3 结构性问题（非 bug，但阻碍演进）

1. **task_plan 是整表替换，不是增量更新**
   - `build_task_state()` 每次创建全新的 `TaskState`，丢弃旧状态。
   - `previous` 参数存在但从未使用。
   - 已完成的任务如果不包含在新 plan 中，会丢失。

2. **ToolExecutorRuntime 存在特殊耦合**
   - `_should_render_generic_tool_event()` 硬编码排除 `"todo"`。
   - `_make_rejected_outcome()` 特殊处理 `"task_plan"`。
   - 这些耦合违反了通用运行时的抽象边界。

3. **依赖关系未执行**
   - `TaskRecord.depends_on` 可以声明依赖，但 `task_execute` 不检查依赖是否完成。

---

## 3. 对比研究

### 3.1 emperor-agent (TheSyart/emperor-agent)

#### 架构特点

emperor-agent 的 subagent 是一个轻量并发执行单元：

- **触发方式**: `dispatch_subagent(agent_type, task)` 直接触发，无中间 TaskState。
- **输入边界**: 一个 `task` 字符串 + 模板化的 system prompt。无结构化 envelope。
- **工具过滤**: 纯白名单，从模板加载 prompt，从代码加载白名单。
- **执行方式**: `runner.step_stream(history, sub_emit)`，复用 AgentRunner，只注入过滤后的 ToolRegistry。
- **事件流**: `sub_emit` 把子代理内部事件实时桥接到主事件流：
  - `message_delta` → `subagent_delta`
  - `tool_call` → `subagent_tool_call`
  - `tool_result` → `subagent_tool_result`
  - `assistant_done` → `subagent_done`
- **并发**: `concurrency_safe=True`，同一帧可并发派遣多个子代理。
- **结果**: 只返回字符串 `final`。

#### 我们可以学习的点

1. **bridge_emit 机制**: 不引入新的事件总线，复用现有 emit callback，通过命名空间前缀（`subagent_*`）区分事件来源。这是实现成本最低的事件流方案。
2. **并发安全设计**: 每次 dispatch 创建独立 history/registry/runner，并发安全是架构属性，不是锁属性。
3. **模板化 system prompt**: 从 `templates/subagents/{name}.md` 读取角色定义，修改角色口吻不需要改代码。

#### emperor-agent 的局限

1. 输入只有字符串，无法表达结构化边界。
2. 结果只有字符串，主代理无法可靠消费。
3. 没有任务状态管理，无法编排多任务依赖。
4. 没有 fork 模式。

---

### 3.2 Claude Code

Claude Code 的 subagent（Agent tool）采用**适度结构化**方案：

- **输入端**: 以 prompt 字符串为主（不靠结构化表单），但 system prompt 是精心设计的角色模板。
- **工具控制**: 普通子代理不是简单"继承父工具集"，而是由 runtime 基于 agent definition、运行模式（sync/async/fork）和禁用规则重新解析工具池；只有 fork 路径才更接近继承父代理的 exact tool pool。
- **输出端**: 返回的是**中度结构化结果**，包含文本内容、agentId、usage、tool use 统计等元数据；但并没有统一的 `files_modified` / `open_questions` / `stop_reason` 契约。
- **事件**: 子代理的执行过程对用户可见，但主要体现为任务状态、工具调用进度、完成通知和可展开 transcript；并不是把子代理的内部思考过程原样展示出来。

**关键洞察**: Claude Code 的实践表明，**输入端可以薄（自然语言 prompt），运行时约束可以强（system prompt + tool filtering + execution mode），输出端至少要带一层可消费的元数据**。这和 harness 当前"输入字段很多，但实际传输和消费链路经常断裂"的问题形成了鲜明对比。

---

## 4. 设计原则

基于以上分析，我们达成以下共识：

### 原则 1: 输入端适度结构化（不要 13 个字段）

LLM 不擅长填结构化表单。让模型填写 `known_facts`、`expected_output`、`done_criteria` 等字段的失败率很高。更好的方式是：

- **让模型写一段自然语言描述**（prompt 方式，像 Claude Code）
- **让运行时（runtime）从描述中提取关键信息**（编译器方式）
- **保留少量必须的结构化字段**（如 `agent_type`、`execution_mode`、`depends_on`）

### 原则 2: 输出端必须结构化（不只是字符串）

主代理需要可靠地知道：
- 子代理是否成功完成（`success: bool`）
- 为什么停止（`stop_reason: SubagentStopReason`）
- 消耗了多少轮次（`turns_used: int`）
- 修改了哪些文件（`files_modified: list[str]`）

emperor-agent 的裸字符串做不到这一点。Claude Code 也只做到了"部分结构化"；如果 harness 希望主代理做可靠编排和结果消费，就需要在这里比 Claude Code 走得更远。

### 原则 3: 事件流是刚需，不是可选功能

子代理执行的黑盒体验不可接受。必须实现：
- 子代理开始执行时立即可见
- 子代理调用工具时立即可见
- 子代理的输出增量实时可见

emperor-agent 的 `bridge_emit` 已经证明了轻量实现的可行性，不需要独立事件总线。Claude Code 也说明了另一点：对用户真正重要的是**任务和工具进度可见**，不一定是完整暴露内部思维链。

### 原则 4: 每个字段必须端到端贯通

定义一个字段意味着：
1. 上游（planner/schema）能写入
2. 中间（编译/传输）不丢失
3. 下游（渲染/执行）能读取
4. 结果（归一化/消费）能反馈

如果做不到，宁可删除该字段也不要留着死代码。

### 原则 5: 并发是安全属性，不是锁属性

如果架构上每个子代理有独立的 history、registry、runner，那并发派遣就是安全的。不要通过全局锁来实现并发安全。

---

## 5. 目标架构

### 5.1 简化后的数据模型

**原则**: 删除死字段，保留活字段，新增必要字段。

```python
# 当前 TaskRecord 有 20+ 字段 → 精简为少量核心字段
@dataclass(slots=True)
class TaskRecord:
    task_id: str
    subject: str
    goal: str
    status: TaskStatus
    execution_mode: TaskExecutionMode
    agent_type: str | None = None        # 子代理类型：explore / plan / general
    description: str | None = None       # 自然语言任务描述（代替 inputs + known_context 的碎片化）
    done_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    result_summary: str | None = None
    files_modified: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    created_at_turn: int = 0
    turns_used: int = 0
    # --- 删除的字段 ---
    # active_form（从未读取）
    # allowed_tools（安全应由 SubagentDefinition 控制，不是任务级别）
    # write_scope（同上）
    # inputs（合并到 description 中）
    # known_context（合并到 description 中）
    # out_of_scope（合并到 description 中）
    # expected_output（合并到 done_criteria 中）
    # required_skills（从任务字段移除；如需预加载 skill，应由 SubagentDefinition 或独立 runtime 配置负责）
    # parent_todo_id（task 和 todo 的关系应在外层维护）
    # owner（未使用）
    # blocked_reason（未使用）
    # artifacts（未使用）
    # packet_revision（过度设计，删除）
    # updated_at_turn（未使用）

# 精简后的 TaskPacket（输入信封）
@dataclass(slots=True)
class TaskPacket:
    task_id: str
    title: str
    directive: str        # 自然语言描述（包含已知上下文、范围边界）
    done_criteria: list[str] = field(default_factory=list)
    agent_type: str | None = None
    # --- 删除的字段 ---
    # mode（由 execution_mode 推导，不需要在 packet 中重复）
    # task_context（合并到 directive）
    # known_facts（合并到 directive）
    # out_of_scope（合并到 directive）
    # expected_output（合并到 done_criteria）
    # required_skills（任务层移除）
    # allowed_tools（由 SubagentDefinition 控制）
    # write_scope（由 SubagentDefinition 控制）
    # packet_revision（删除）

# 精简后的 TaskRunResult（输出信封）
@dataclass(slots=True)
class TaskRunResult:
    task_id: str
    success: bool
    status: TaskStatus
    summary: str
    files_modified: list[str] = field(default_factory=list)
    stop_reason: str | None = None          # 新增：completed / max_turns / api_error / empty_response / cancelled
    turns_used: int = 0                     # 新增
```

**设计理由**:

1. `description`（自然语言）取代 `inputs` + `known_context` + `out_of_scope`：LLM 更擅长写一段完整描述，而不是填三个独立列表。
2. `done_criteria` 取代 `expected_output`：完成标准和期望输出在语义上接近，合并减少字段数。
3. `allowed_tools` / `write_scope` 从任务级别删除：安全边界应由 `SubagentDefinition` 控制，而不是每次任务都重新声明。
4. `required_skills` 从任务级别删除：任务 planner 不再动态决定预加载 skill；如确有需要，应由代码级子代理定义或独立 runtime 配置负责。
5. `stop_reason` 取代 `failure_reason`：无论成功或失败，都应报告停止原因（`completed` / `max_turns` / `api_error` / `empty_response` / `cancelled`）。
6. `turns_used` 新增：成本追踪和调试必需。

---

### 5.2 task_plan Schema 扩展

当前 `task_plan` 只接受 5 个字段。扩展后：

```json
{
  "type": "object",
  "properties": {
    "tasks": {
      "type": "array",
      "maxItems": 20,
      "items": {
        "type": "object",
        "properties": {
          "task_id": {"type": "string"},
          "subject": {"type": "string"},
          "goal": {"type": "string"},
          "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "completed", "failed", "cancelled"]},
          "execution_mode": {"type": "string", "enum": ["local", "fresh_subagent"]},
          "agent_type": {"type": "string", "enum": ["explore", "plan", "general"]},
          "description": {"type": "string", "description": "自然语言任务描述，包含已知上下文、范围边界、输入数据"},
          "done_criteria": {"type": "array", "items": {"type": "string"}},
          "depends_on": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["subject", "goal"]
      }
    }
  },
  "required": ["tasks"]
}
```

新增字段：`agent_type`、`description`、`done_criteria`、`depends_on`。

**保留 9 个字段（原来 5 个 + 新增 4 个），删除冗余字段。**

---

### 5.3 SubagentRuntime 改造

#### 5.3.1 保持同步执行，新增 renderer-bridge 事件流

当前问题不是"必须把 `SessionEngine` 改成流式 API"，而是**子代理内部已有的 renderer 事件没有桥接到父代理**。

```python
def run(self, request: SubagentRequest) -> SubagentRunResult:
    # ... 创建 engine ...
    result = engine.submit_user_message(prompt_text)
    return SubagentRunResult(...)
```

`QueryLoop.run()` 当前已经会经由 renderer 发出这些事件：

- `show_status(...)`
- `show_thinking(...)`
- `show_assistant(...)`
- `show_tool_call(...)`
- `show_tool_result(...)`

因此这里的修复方案是：**保留同步 `submit_user_message()`，增加一个 `SubagentBridgeRenderer`，把子代理内部 renderer 调用实时转成 `subagent_*` 事件发给父代理**。

目标接口：

```python
def run(
    self,
    request: SubagentRequest,
    emit: Callable[[dict], None] | None = None,
) -> SubagentRunResult:
    if emit:
        emit({"event": "subagent_start", ...})
    result = engine.submit_user_message(prompt_text)  # 仍然同步
    if emit:
        emit({"event": "subagent_done", ...})
    return SubagentRunResult(...)
```

**关键改动**:

1. `run()` 接受可选的 `emit` callback。
2. 如果提供了 `emit`，子代理 engine 使用 `SubagentBridgeRenderer`。
3. 如果没有提供 `emit`，保持 `quiet=True` 的现状。
4. 不新增 `SessionEngine.submit_user_message_stream()`；避免为了 subagent 进度可见性引入额外主循环接口。

#### 5.3.2 事件转换规范

```python
class SubagentBridgeRenderer(Renderer):
    def __init__(self, *, task_id: str, agent_type: str, emit: Callable[[dict], None]):
        self._task_id = task_id
        self._agent_type = agent_type
        self._emit = emit

    def _base(self, event: str) -> dict:
        return {
            "event": event,
            "task_id": self._task_id,
            "agent_type": self._agent_type,
        }

    def show_status(self, message: str) -> None:
        self._emit({**self._base("subagent_status"), "content": message})

    def show_thinking(self, title: str, reasoning: str) -> None:
        self._emit({**self._base("subagent_thinking"), "title": title, "content": reasoning})

    def show_assistant(self, content: str | None) -> None:
        if content:
            self._emit({**self._base("subagent_message"), "content": content})

    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._emit({**self._base("subagent_tool_call"), "tool_name": name, "tool_args": args})

    def show_tool_result(self, name: str, output: str) -> None:
        self._emit({**self._base("subagent_tool_result"), "tool_name": name, "content": output})
```

#### 5.3.3 修复 agent_type 硬编码

```python
# task_execute.py 修复
sub_result = runtime.run(
    SubagentRequest(
        task_packet=packet,
        agent_type=SubagentType(packet.agent_type) if packet.agent_type else SubagentType.GENERAL,
    )
)
```

#### 5.3.4 结果归一化规则（定稿）

`SubagentRunResult.stop_reason` 与 `TaskRunResult.stop_reason` 的允许值固定为：

- `completed`
- `max_turns`
- `api_error`
- `empty_response`
- `cancelled`

其中底层 `QueryResult.stop_reason == "aborted"` 在子代理层统一归一化为 `cancelled`。

归一化规则：

```python
def normalize_subagent_result(task_id: str, result: SubagentRunResult) -> TaskRunResult:
    status_map = {
        "completed": TaskStatus.COMPLETED,
        "cancelled": TaskStatus.CANCELLED,
        "max_turns": TaskStatus.FAILED,
        "api_error": TaskStatus.FAILED,
        "empty_response": TaskStatus.FAILED,
    }
    return TaskRunResult(
        task_id=task_id,
        success=result.success,
        status=status_map[result.stop_reason.value],
        summary=result.output,
        files_modified=list(result.files_modified),
        stop_reason=result.stop_reason.value,
        turns_used=result.turns_used,
    )
```

`files_modified` 的契约也在此定死：

- 它**只**来自 `RunUpdateKind.MARK_FILE_MODIFIED`
- 它**不**从自然语言 summary 中解析
- 如果某个写工具修改了文件但没有发出 `MARK_FILE_MODIFIED`，那是该工具的 bug，不由 `SubagentRuntime` 猜测补齐
- 第一版验收只覆盖 `edit_file` / `write_file` 等结构化写工具，不把 `bash` 写文件纳入 `files_modified` 的可靠性契约

---

### 5.4 ToolExecutorRuntime 实时渲染改造

当前 `execute_batch()` 在整批完成后统一渲染。改造为：工具开始执行时立即渲染，结果返回时立即渲染。

**方案**:

```python
def execute_batch(self, calls: list[ToolCall], *, turn: int) -> ToolBatchResult:
    # ... 分区 ...
    
    for batch in batches:
        if batch.parallel:
            results = self._execute_parallel(batch.calls, turn=turn)
        else:
            results = self._execute_serial(batch.calls, turn=turn)
        
    return ToolBatchResult(...)

def _execute_parallel(self, calls: list[ToolCall], *, turn: int) -> list[ToolInvocationOutcome]:
    for call in calls:
        self._render_tool_call(call)  # ← 提交前立即显示开始
    with ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(self._run_single, call, turn=turn): call
            for call in calls
        }
        results = []
        for future in as_completed(futures):
            call = futures[future]
            result = future.result()
            self._render_tool_result(call, result)  # ← 每个工具完成后立即渲染
            results.append(result)
    return results

def _render_tool_call(self, call: ToolCall):
    ...

def _render_tool_result(self, call: ToolCall, result: ToolInvocationOutcome):
    ...
```

串行路径同样适用：开始执行前立即 `show_tool_call`，返回后立即 `show_tool_result`，不再等整批结束统一回放。

**注意**: 子代理内部运行时默认使用 `display=RunDisplayOptions(quiet=True)`，所以子代理自己的工具调用不会通过这个机制渲染。只有在 `SubagentRuntime.run(..., emit=...)` 启用 bridge renderer 时，才把子代理内部 runtime 的 `quiet` 设为 `False`，让工具事件被 `SubagentBridgeRenderer` 接到。

---

### 5.5 并发派遣支持

当前 `task_execute` 标记了 `concurrency_safe=False`。改为 `concurrency_safe=True`，前提是子代理执行时上下文隔离。

需要明确两件事：

1. **子代理 `SessionEngine` 必须始终拥有自己的 `SessionState`**
2. **父上下文只允许以只读快照形式被子代理读取**

当前代码里会把 `parent_context` 保存到 `SubagentRuntime`：

```python
runtime = SubagentRuntime(parent_context=context)
```

这本身不是问题；真正要定死的是：

- `working_dir`、`skill_registry` 可以从父上下文读取
- 父 `session_state` 不能透传给子代理工具上下文
- 父 renderer 不能被多个子代理直接共享写入，必须走 task-scoped emit/bridge

**修复方案**: 在 `SubagentRuntime` 中只提取**不可变快照**（如 `working_dir`、`skill_registry`），然后在子代理 engine 初始化时绑定新的 `SessionState`。

```python
def run(self, request: SubagentRequest, emit=None) -> SubagentRunResult:
    # 从 parent_context 提取静态信息
    working_dir = self._parent_context.working_dir if self._parent_context else os.getcwd()
    skill_registry = self._parent_context.skill_registry if self._parent_context else None
    
    # 创建完全独立的 tool_context；随后由 child SessionEngine 绑定 child SessionState
    tool_context = ToolUseContext(working_dir=working_dir, max_turns=max_turns)
```

验收标准不是"理论上不会共享"，而是：

- 两个并发 `task_execute` 调用各自写入自己的 child `SessionState`
- 子代理 A 的 todo/task/file-state 变化不会出现在子代理 B 的结果中
- 两个子代理的进度事件都带 `task_id`，父 UI 能稳定区分来源

---

### 5.6 task_plan 增量更新

当前 `build_task_state()` 是整表替换。最终定稿为：**upsert + history preservation**，规则如下：

1. 新 payload 中出现的 `task_id`：更新/覆盖该任务
2. 旧状态中未出现于新 payload 的终态任务（`completed` / `failed` / `cancelled`）：保留
3. 旧状态中未出现于新 payload 的非终态任务（`pending` / `in_progress` / `blocked`）：自动转为 `cancelled`，并保留历史结果
4. 新 payload 中若复用一个旧的终态 `task_id`，视为非法；要重新开任务，必须换新 `task_id`
5. `ordered_task_ids` 顺序为：本次 payload 中的任务顺序 + 被保留/自动取消的旧任务，保持其原有相对顺序
6. `current_task_id` 只能从本次 payload 中 `in_progress` 的任务推导；自动取消和保留历史任务不会成为 current

```python
def build_task_state(
    raw_tasks: list[dict],
    *,
    previous: TaskState | None,
    turn_count: int,
) -> TaskState:
    validate_tasks(raw_tasks, previous=previous)

    incoming_ids = set()
    tasks: list[TaskRecord] = []

    for i, raw in enumerate(raw_tasks):
        task = normalize_task_payload(raw, index=i, previous=previous)
        if previous and task.task_id in previous.tasks_by_id:
            prev = previous.tasks_by_id[task.task_id]
            if prev.status in TERMINAL_STATUSES and task.status != prev.status:
                raise ValueError(f"cannot reopen terminal task_id: {task.task_id}")
        incoming_ids.add(task.task_id)
        tasks.append(task)

    preserved_tail: list[TaskRecord] = []
    if previous:
        for task_id in previous.ordered_task_ids:
            prev = previous.tasks_by_id[task_id]
            if task_id in incoming_ids:
                continue
            if prev.status in TERMINAL_STATUSES:
                preserved_tail.append(prev)
            else:
                preserved_tail.append(replace(prev, status=TaskStatus.CANCELLED))

    all_tasks = tasks + preserved_tail
    return TaskState(...)
```

---

### 5.7 行为契约与验收标准

为了让后续 impl spec 可以直接落地，本节把剩余未决点全部定稿。

#### 5.7.1 fork 的处理方式（定稿）

当前 harness 的 `fork` 不是"未完成能力"，而是**错误暴露的公共表面**。本次修复的定稿方案是：

- 从 `TaskExecutionMode` 中删除 `fork_subagent`
- 从 `SubagentType` / `SubagentContextMode` 中删除 `FORK`
- 从 `task_plan` schema 中删除 `fork_subagent`
- 如果历史状态里存在 `fork_subagent`，在加载或执行时直接报 `unsupported_legacy_mode`

理由：Claude Code 的 fork 之所以成立，是因为它有完整的 fork runtime、exact tool pool 继承和专门的 child prompt 约束；harness 当前没有这些基础设施，不应继续暴露半实现协议。

#### 5.7.2 `depends_on` 的执行规则（定稿）

`task_execute(task_id)` 在真正 dispatch 前必须检查依赖：

- 依赖任务不存在：失败，`error="missing_dependency"`
- 任一依赖状态不是 `completed`：失败，`error="dependency_not_ready"`
- 只有全部依赖完成，才允许执行

#### 5.7.3 最小测试矩阵（impl spec 必须覆盖）

1. `agent_type` 传播：`task_plan(agent_type=plan)` 后，`task_execute` 实际使用 `PLAN_AGENT`
2. prompt 传递：`description` 和 `done_criteria` 最终进入子代理 prompt
3. fork 清理：`task_plan` 不再接受 `fork_subagent`；旧 `FORK` 枚举不会留在运行时死代码中
4. 结果归一化：`completed/max_turns/api_error/empty_response/aborted` 分别映射到正确的 `TaskRunResult.stop_reason/status`
5. 事件可见：子代理开始、工具调用、工具结果、完成事件都会带 `task_id`
6. 并发安全：两个 `task_execute` 并发运行时，互不污染 `SessionState`、`todo_state`、`files_modified`
7. `task_plan` merge 语义：旧终态任务保留，旧非终态但被遗漏的任务自动转 `cancelled`
8. `depends_on`：依赖未完成时 `task_execute` 明确失败，不 silently execute

---

## 6. 实现计划

### Phase 0: Bug 修复（1-2 天）

1. **修复 agent_type 硬编码** (`task_execute.py:70`)
2. **修复 task_context 渲染遗漏** (`_render_fresh_packet` 添加 `task_context` 渲染)
3. **修复 task_plan schema** (添加 `agent_type`、`description`、`done_criteria`、`depends_on`；移除 `fork_subagent`)
4. **修复实时渲染** (`ToolExecutorRuntime` 改为每个工具完成后立即渲染)
5. **移除假 fork 表面** (`TaskExecutionMode` / `SubagentType` / schema 同步收口)

**预期效果**: subagent 基本可用，planner 能正确指定 agent_type，子代理能看到完整的任务描述，主代理能看到工具执行进度，公共协议不再暴露假 fork 能力。

### Phase 1: 数据模型精简（2-3 天）

1. **删除死字段**: `artifacts`、`owner`、`blocked_reason`、`updated_at_turn`、`packet_revision`、`failure_reason`、`open_questions`、`recommended_next_steps` 等。
2. **精简 TaskPacket**: 合并 `inputs`/`known_context`/`out_of_scope` 为 `directive`。
3. **精简 TaskRunResult**: 添加 `stop_reason`、`turns_used`，删除 `failure_reason` / `open_questions`。
4. **移除任务级 `required_skills`**，把 skill 预加载责任收回到 `SubagentDefinition` / runtime。
5. **清理 planner_runtime**: 将 `previous` 真正用于 merge 语义，不再保留死参数。

**预期效果**: 代码更清晰，认知负担降低，每个字段都有完整的读写路径。

### Phase 2: 事件流（3-5 天）

1. **实现 `SubagentBridgeRenderer`**，桥接 child renderer 到 `emit` callback。
2. **改造 `SubagentRuntime.run()`** 接受 `emit` callback，并在 `emit` 模式下关闭 child `quiet=True`。
3. **定稿 `subagent_*` 事件 payload**。
4. **在 `task_execute` 中传递 task-scoped emit 回调**。
5. **终端展示层消费 subagent 事件**（任务卡片 + 关键事件流）。

**预期效果**: 用户能看到子代理的实时执行过程，不再黑盒等待；不需要为此给 `SessionEngine` 引入额外的流式 public API。

### Phase 3: 并发派遣（2-3 天）

1. **验证并固化 child `SessionState` 绑定契约**，确保父 `session_state` 不会透传到子代理。
2. **将 `task_execute` 标记为 `concurrency_safe=True`**。
3. **验证多子代理并发执行的正确性**。

**预期效果**: 同一帧可以并发派遣多个子代理，提高吞吐量。

### Phase 4: 依赖管理和 planner 语义闭环（必做）

1. **实现 `depends_on` 依赖检查**。
2. **实现 task_plan merge 语义**。
3. **补齐 acceptance tests**。

**预期效果**: 任务计划不再丢历史状态，依赖关系真正生效，文档中列出的现有问题全部有代码路径修复。

---

## 7. 设计决策记录

### 决策 1: 输入端变薄，输出端变厚

**背景**: harness 当前在输入端和输出端都想做厚协议，但输入端的 13 字段系统从未跑通。

**决策**: 输入端减薄（用自然语言 `description` 代替碎片化的 `inputs`/`known_context`/`out_of_scope`），输出端适度加厚（`stop_reason`、`turns_used`、`files_modified`）。

**理由**: LLM 擅长写自然语言描述，不擅长填结构化表单。但主代理消费结果时必须依赖结构化数据，不能靠自然语言解析。

### 决策 2: 删除死字段，不保留未来扩展字段

**背景**: `TaskRecord` 有 20+ 字段，一半以上是死代码。

**决策**: 删除所有当前未读写的字段。未来如果需要，再添加。

**理由**: 死字段是维护负担，每改一次模型就要考虑"这个字段有人用吗？"。YAGNI 原则。

### 决策 3: 事件流用 callback 而非独立总线

**背景**: 需要实时事件流，但引入独立事件总线会增加架构复杂度。

**决策**: 复用现有的 renderer/emit callback 机制，通过命名空间前缀区分事件来源。

**理由**: emperor-agent 已经证明了这种方案的可行性，实现成本最低，不需要改动核心事件模型。

### 决策 4: 安全边界放在 SubagentDefinition 而非 TaskRecord

**背景**: `allowed_tools` 和 `write_scope` 当前在 TaskRecord 级别定义，但从未生效。

**决策**: 删除这两个字段，安全边界由 `SubagentDefinition`（代码级别）控制。

**理由**: 安全设置不应由 LLM planner 动态决定。代码级别的白名单更可靠。

### 决策 5: 删除 fork 的公共表面，而不是继续保留半实现协议

**背景**: `FORK` 枚举存在但完全未实现。

**决策**: 本次修复中直接删除 fork 的公共表面（枚举、schema、execution_mode），而不是继续保留半实现协议。

**理由**: "保留但不实现" 不是修复，只是延后。Claude Code 的 fork 之所以成立，是因为它有完整 runtime 语义；harness 当前没有，不应继续暴露假能力。

### 决策 6: 事件流通过 renderer bridge 实现，而不是新增 engine stream API

**背景**: 文档初稿假设需要 `submit_user_message_stream()` 才能让子代理进度可见。

**决策**: 不新增 `SessionEngine` 的流式 public API；直接复用 `QueryLoop` 已有的 renderer 回调，通过 `SubagentBridgeRenderer` 转成 `subagent_*` 事件。

**理由**: 当前代码已经在 renderer 层暴露了 reasoning、assistant、tool call、tool result、status。桥接现有接口即可满足需求，变更面更小，impl spec 也更确定。

---

## 8. 附录：代码清单

本设计文档涉及以下文件，按修改优先级排序：

| 优先级 | 文件 | 修改内容 |
|--------|------|----------|
| P0 | `core/tools/builtin/task_plan.py` | 扩展 Schema |
| P0 | `core/tools/builtin/task_execute.py` | 修复 agent_type 硬编码 |
| P0 | `core/session/subagent.py` | 修复 task_context 渲染、移除假 fork、支持 emit callback |
| P0 | `core/tools/runtime.py` | 修复实时渲染 |
| P1 | `core/tasks/models.py` | 精简数据模型 |
| P1 | `core/tasks/dispatcher.py` | 适配精简后的模型 |
| P1 | `core/tasks/planner_runtime.py` | 实现 merge 语义、删除 dead code |
| P2 | `core/ui/renderer.py` | 消费 subagent 事件 |

---

## 9. 附录： emperor-agent 相关代码引用

以下代码来自 https://github.com/TheSyart/emperor-agent（commit 基准），作为 bridge_emit 机制的参考：

```python
# agent/tools/dispatch.py

class DispatchSubagentTool(Tool):
    name = "dispatch_subagent"
    
    @property
    def concurrency_safe(self) -> bool:
        return True
    
    def execute(self, *, agent_type: str, task: str, **kwargs) -> dict:
        # ... 省略 spec 解析和 registry 构建 ...
        
        def sub_emit(evt):
            evt_type = evt.get("event", "")
            if evt_type == "message_delta":
                evt_type = "subagent_delta"
            elif evt_type == "tool_call":
                evt_type = "subagent_tool_call"
            elif evt_type == "tool_result":
                evt_type = "subagent_tool_result"
            elif evt_type == "assistant_done":
                evt_type = "subagent_done"
            
            base = {
                "parent_id": parent_call_id,
                "subagent_id": subagent_id,
                "event": evt_type,
            }
            base.update({k: v for k, v in evt.items() if k != "event"})
            bridge_emit(base)
        
        def bridge_emit(evt):
            loop = asyncio.get_running_loop()
            asyncio.run_coroutine_threadsafe(emit(evt), loop)
        
        # 流式执行
        run_sync(runner.step_stream(history, sub_emit))
        
        return {"content": final}
```

---

## 10. 结论

harness 的 subagent 设计方向是正确的（结构化边界、隔离执行、类型化结果），但执行链路存在大量断裂。本方案的核心思路是：

1. **先做减法**：删除死字段、修复已知 bug，让现有的 fresh 模式真正跑通。
2. **再做加法**：添加事件流和并发支持，但不引入过度复杂的协议。
3. **学习但不照搬**：吸收 emperor-agent 的 bridge_emit 和并发设计，但保留 harness 的结构化输出优势。

目标不是设计一个完美的 subagent 系统，而是先让一个**简单、完整、端到端贯通**的系统稳定运行。
