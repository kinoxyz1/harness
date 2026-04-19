# Harness 架构演进深度解析

> 本文基于 9 个 commit 的完整代码分析，面向想学习 Agent 智能体设计的开发者，详细讲解架构从单体脚本到多层 Agent 框架的演进过程，重点剖析阶段 4 中 Control Plane、Policy System、Skills System 等核心设计的原理与联动机制。

---

## 目录

1. [四个阶段概览](#1-四个阶段概览)
2. [阶段 4 前因：为什么需要架构重构](#2-阶段-4-前因为什么需要架构重构)
3. [领域分解：从单类到多层包](#3-领域分解从单类到多层包)
4. [Control Plane：工具反向控制循环行为](#4-control-plane工具反向控制循环行为)
5. [Policy System：横切关注点的钩子机制](#5-policy-system横切关注点的钩子机制)
6. [Skills System：本地文件即能力的动态扩展](#6-skills-system本地文件即能力的动态扩展)
7. [三大系统的联动：一个完整的请求生命周期](#7-三大系统的联动一个完整的请求生命周期)
8. [设计原则总结](#8-设计原则总结)

---

## 1. 四个阶段概览

### 阶段一：单体脚本（commits 1-2）

一个 `agent_loop()` 函数 + 一个 bash 工具 + OpenAI 兼容 API。commit 2 迅速引入插件式工具系统——每个工具独立模块，暴露 `SCHEMA` + `handle()`，通过 `ToolRegistry` 自动发现注册。

```
01_agent_loop.py
core/
  agent.py          ← 176 行，包含所有逻辑
  config.py
  llm.py
  tools.py          ← 单文件，只有 bash 工具
```

### 阶段二：结构化 Agent（commits 3-4）

引入 `LLMResponse` 抽象层、消息归一化协议（`protocol.py`）、上下文注入管道、`AgentLoop` 类（依赖注入）。commit 4 提取 `RichRenderer`、`ContextPipeline`、接口定义。

```
core/
  agent.py          ← AgentLoop 类，~500 行
  context.py        ← 上下文注入
  protocol.py       ← 消息归一化
  runtime.py        ← ToolExecutorRuntime
  interfaces.py     ← Protocol 定义
  llm_client.py     ← OpenAIClient + LLMResponse
  renderer.py       ← RichRenderer
  todo.py           ← Todo 状态管理
  tools/
    bash.py / edit_file.py / find.py / read_file.py / write_file.py / todo.py
```

### 阶段三：子 Agent 支持（commit 6）

`AgentRunResult` 让 `run()` 返回结构化结果。`SubagentRuntime` 创建隔离 agent 实例。`RunDisplayOptions` 支持 quiet 模式。

```
新增:
  core/subagent_runtime.py
  core/tools/subagent.py
  core/run_options.py
```

### 阶段四：领域分解 + Anthropic 迁移（commits 7-9）

**最大规模重构：53 个文件，+4218/-728 行。** 将 `AgentLoop` 完全分解为领域包，引入 Control Plane、Policy System、Skills System，迁移到原生 Anthropic SDK。

```
core/
  llm/              ← 模型层：Gateway、Client、Protocol、Response
  query/            ← 循环层：Loop、State、Recovery、Result
  session/          ← 编排层：Engine、State、Store、ViewBuilder、Commands
  policy/           ← 策略层：Base Protocol、MaxTurns、TodoTracking
  prompt/           ← 提示层：Assembler、Cache、Context
  skills/           ← 技能层：Models、Registry、Runtime
  tools/            ← 工具层：Registry、Runtime、Builtin/*
  shared/           ← 共享：Config、Interfaces、Types
  ui/               ← 渲染层：Renderer
```

---

## 2. 阶段 4 前因：为什么需要架构重构

### 2.1 单类膨胀问题

到阶段 3 结束时，`AgentLoop` 已经承载了过多职责：

- LLM 调用与重试
- 工具执行编排
- 消息管理
- 上下文注入
- 显示渲染
- 策略判断（轮次限制、todo 追踪）
- 子 Agent 生命周期
- 恢复机制

一个类做了 8 件事。每次要加新功能（比如 Skills 系统），就要修改这个巨大的类。**修改的风险与类的体积成正比。**

### 2.2 模型迁移需求

从 DashScope（OpenAI 兼容 API）迁移到原生 Anthropic SDK，消息格式完全不同：

- Anthropic 的 `system` 是顶级参数，不是消息列表中的角色
- 工具调用从 `tool_calls` 变成 `tool_use` content block
- 工具结果从 `tool` 角色消息变成 `user` 角色中的 `tool_result` block
- 新增 `thinking` content block 支持

这要求彻底重写协议层，旧的 `LLMResponse` 抽象不够用。

### 2.3 能力扩展需求

需要一个让 Agent 能力可以**不修改代码就动态扩展**的机制。Skills 系统应运而生，但它带来的不仅仅是"加载文件"——它改变了循环的执行流程（需要让模型看到技能内容后再做决策），这需要一个比"工具返回字符串"更强大的机制。

---

## 3. 领域分解：从单类到多层包

### 3.1 分解原则

每个包有且只有一个变更理由（单一职责）。包与包之间通过数据结构（dataclass）和 Protocol 接口通信，不通过函数调用或继承。

### 3.2 各包职责

| 包 | 职责 | 核心类 | 行数 |
|---|---|---|---|
| `session/` | 会话编排：管理整个对话的生命周期 | `SessionEngine`, `SessionState`, `SessionStore` | ~300 |
| `query/` | 查询循环：管理一次用户查询的思考-行动循环 | `QueryLoop`, `RunState`, `RecoveryManager` | ~350 |
| `llm/` | 模型通信：封装 API 调用、消息格式转换 | `ModelGateway`, `AnthropicClient` | ~400 |
| `tools/` | 工具执行：注册、调度、控制平面信号收集 | `ToolExecutorRuntime`, `ToolRegistry` | ~400 |
| `policy/` | 策略钩子：横切关注点的插件式管理 | `PolicyRunner`, `RunPolicy` | ~80 |
| `skills/` | 技能管理：发现、加载、注入 | `SkillRegistry`, `SkillRuntime` | ~250 |
| `prompt/` | 提示组装：系统提示词构建 | `PromptAssembler`, `PromptCache` | ~200 |
| `ui/` | 终端渲染：所有用户可见的输出 | `RichRenderer` | ~200 |

### 3.3 调用关系

```
SessionEngine（编排层）
    │
    ├── 持有 SessionState（会话级可变状态）
    ├── 持有 SessionStore（消息列表操作）
    ├── 持有 SkillRegistry（技能发现与加载）
    ├── 持有 PromptAssembler（提示词构建）
    │
    └── 每次用户输入 → 调用 QueryLoop.run()（循环层）
                        │
                        ├── 持有 RunState（查询级临时状态）
                        ├── 调用 PolicyRunner（策略层）
                        ├── 调用 MessageViewBuilder（视图构建）
                        ├── 调用 ModelGateway（模型层）
                        ├── 调用 ToolExecutorRuntime（工具层）
                        └── 调用 RecoveryManager（恢复层）
```

### 3.4 两种状态的分离

一个关键设计决策是将状态分为两层：

**SessionState（会话级，跨查询持久）：**

```python
# core/session/state.py
@dataclass(slots=True)
class SessionState:
    conversation_messages: list[dict[str, Any]]   # 完整对话历史
    skill_catalog: dict[str, SkillMeta]            # 已发现的技能目录
    invoked_skills: dict[str, InvokedSkillRecord]  # 已激活的技能记录
    skill_events: list[SkillEvent]                 # 技能事件审计日志
    todo_state: TodoState                          # Todo 任务状态
    skills_revision: str | None                    # 技能目录版本哈希
    # ...
```

**RunState（查询级，单次查询临时）：**

```python
# core/query/state.py
@dataclass(slots=True)
class RunState:
    turn_count: int = 0                           # 已执行的轮次数
    stop_reason: str | None = None                # 当前停止原因
    allowed_tools_override: set[str] | None = None  # 控制平面：工具限制
    model_override: str | None = None             # 控制平面：模型切换
    effort_override: str | None = None            # 控制平面：努力程度
    barrier_reason: str | None = None             # 控制平面：屏障原因
    todo_replan_required: bool = False            # 控制平面：需要重新规划
    assistant_turns_since_todo: int = 0           # 策略用：距上次 todo 的轮次
    # ...
```

为什么分两层？因为一次会话可能有多轮用户输入，每轮输入触发一个新的 `QueryLoop.run()`，产生一个全新的 `RunState`。但 `SessionState` 中的对话历史、技能目录、Todo 状态是跨轮持久存在的。

---

## 4. Control Plane：工具反向控制循环行为

### 4.1 传统 Agent Loop 的问题

传统的 Agent 工具调用是单向的：

```
模型 → 调用工具 → 拿到字符串结果 → 继续
```

工具是被动的。它不知道自己在什么上下文中被调用，也不知道调用后循环应该怎么变化。

但在实际场景中，有些工具的执行结果应该**改变循环本身的行为**：

| 场景 | 需求 |
|---|---|
| 激活了一个受限的 Skill | 后续只能使用特定工具子集 |
| Skill 需要更强的推理能力 | 切换到更强大的模型 |
| Skill 展开了新指令 | 强制模型重新规划任务 |
| 某个工具检测到安全风险 | 立即终止当前执行路径 |

### 4.2 解决方案：数据驱动的控制信号

Harness 的做法是：**工具不只返回字符串，还返回控制信号。** 这些信号通过数据结构（dataclass）传递，不需要函数回调或直接引用。

核心数据结构定义在 `core/tools/context.py`：

```python
@dataclass(slots=True)
class ContextPatch:
    """运行时参数修改信号。"""
    allowed_tools: set[str] | None = None    # 限制可用工具集
    model_override: str | None = None        # 切换模型
    effort_override: str | None = None       # 调整努力程度

@dataclass(slots=True)
class ExecutionBarrier:
    """执行屏障：中断当前批次，强制回模型。"""
    stop_after_tool: bool = True
    reason: str | None = None
```

工具的返回类型 `ToolResult`：

```python
@dataclass
class ToolResult:
    output: str                                    # 给模型的文本结果
    success: bool                                  # 是否成功
    error: str | None = None                       # 错误码
    truncated: bool = False                        # 是否截断
    injected_messages: list[dict[str, Any]] = field(default_factory=list)  # 要注入对话的消息
    context_patch: ContextPatch | None = None      # 运行时参数修改
    barrier: ExecutionBarrier | None = None        # 执行屏障
```

### 4.3 信号的聚合：ToolBatchResult

模型可能在一次响应中调用多个工具。`ToolExecutorRuntime` 将所有工具的结果聚合为一个 `ToolBatchResult`：

```python
# core/tools/runtime.py
@dataclass(slots=True)
class ToolBatchResult:
    tool_results: list[dict[str, Any]]         # 标准工具结果消息（给模型看的）
    files_modified: list[str]                  # 被修改的文件列表
    tool_names: list[str]                      # 工具名称列表
    injected_messages: list[dict[str, Any]]    # 收集所有 injected_messages
    context_patches: list[ContextPatch]         # 收集所有 ContextPatch
    barrier: ExecutionBarrier | None           # 第一个 barrier 生效
    tool_successes: list[bool] | None = None   # 每个工具是否成功
```

聚合逻辑在 `execute_batch()` 中：

```python
# 核心聚合代码（简化版）
injected_messages = []
context_patches = []
barrier = None

for result in all_results.values():
    injected_messages.extend(result.injected_messages)
    if result.context_patch is not None:
        context_patches.append(result.context_patch)
    if result.barrier is not None and result.barrier.stop_after_tool:
        barrier = result.barrier  # 第一个 barrier 生效
```

### 4.4 信号的消费：_apply_batch_control_plane

`QueryLoop` 中的 `_apply_batch_control_plane()` 是控制平面的核心——它读取 `ToolBatchResult` 中的信号，写入 `RunState`：

```python
# core/query/loop.py:110-142
def _apply_batch_control_plane(state: RunState, batch: ToolBatchResult) -> None:
    skill_expanded_barrier = False

    # 1. 应用所有 ContextPatch
    for patch in batch.context_patches:
        if patch.allowed_tools is not None:
            # 交集取窄：只能用更少的工具，不能变多
            state.allowed_tools_override = (
                patch.allowed_tools
                if state.allowed_tools_override is None
                else state.allowed_tools_override & patch.allowed_tools
            )
        if patch.model_override is not None:
            state.model_override = patch.model_override
        if patch.effort_override is not None:
            state.effort_override = patch.effort_override

    # 2. 处理 Barrier
    if batch.barrier is not None:
        state.barrier_reason = batch.barrier.reason
        if batch.barrier.reason == "skill_expanded":
            skill_expanded_barrier = True
            state.todo_replan_required = True
            state.todo_replan_reason = "skill_expanded"

    # 3. 如果 todo 工具成功（且没有 skill barrier），重置规划标记
    todo_succeeded = False
    tool_successes = getattr(batch, "tool_successes", None) or []
    for idx, tool_name in enumerate(getattr(batch, "tool_names", [])):
        if tool_name == "todo" and idx < len(tool_successes) and tool_successes[idx]:
            todo_succeeded = True
            break

    if todo_succeeded and not skill_expanded_barrier:
        state.todo_replan_required = False
        state.todo_replan_reason = None
        state.assistant_turns_since_todo = 0
```

### 4.5 信号的生效：MessageViewBuilder

控制平面的效果不是在 `_apply_batch_control_plane` 中直接执行的。它是**延迟生效**的——在下一轮循环迭代中，通过 `MessageViewBuilder` 读取 `RunState`，构造出模型实际看到的视图：

```python
# core/session/view_builder.py
class MessageViewBuilder:
    def build(self, state: SessionState, run_state=None) -> MessageView:
        messages = list(state.conversation_messages)
        tools = self._tools
        # 如果控制平面设置了工具限制，过滤工具列表
        if run_state is not None and run_state.allowed_tools_override is not None and tools is not None:
            tools = [tool for tool in tools if tool.get("name") in run_state.allowed_tools_override]
        return MessageView(messages=messages, tools=tools)
```

### 4.6 完整的数据流

```
工具返回 ToolResult(context_patch=..., barrier=...)
    ↓
ToolExecutorRuntime 聚合为 ToolBatchResult
    ↓
QueryLoop._apply_batch_control_plane() 写入 RunState
    ↓
下一轮循环:
  MessageViewBuilder.build() 读取 RunState.allowed_tools_override
    → 过滤工具列表
  ModelGateway.call_once(tools=过滤后的工具列表)
    → 模型只能看到被允许的工具
```

**关键洞察：** 写入者（控制平面）不需要知道读取者（ViewBuilder）的存在。读取者不需要知道谁写入了什么。它们通过 `RunState` 这个共享状态对象间接通信。这就是**数据驱动的控制流**——不通过函数调用或回调，而通过数据传递。

---

## 5. Policy System：横切关注点的钩子机制

### 5.1 问题：主循环中的非核心逻辑

Agent Loop 有很多"不是核心逻辑但又必须穿插在循环中"的需求。如果把它们都写进 `QueryLoop.run()` 的 `while True` 里：

```python
# 假设不用 Policy System 的写法
while True:
    # 轮次检查
    if state.turn_count >= max_turns:
        ...
    # Todo 过期检查
    if state.assistant_turns_since_todo >= 4:
        store.append(reminder_message)
    # Skill 展开后的重新规划检查
    if state.todo_replan_required:
        store.append(replan_message)
    # 将来还要加：费用控制、安全审计、性能监控...
    # 每加一个，这里就多一段 if-else
```

循环体会无限膨胀。更糟的是，这些逻辑之间可能存在交互（比如 skill 展开触发的重新规划应该优先于 todo 过期提醒），交互逻辑会嵌套在 if-else 中，越来越难以理清。

### 5.2 解决方案：Protocol 定义的钩子接口

定义三个钩子点，精确对应 Agent Loop 的三个决策时刻：

```python
# core/policy/base.py
class RunPolicy(Protocol):
    def before_model_call(self, context, state) -> list[dict[str, str]]:
        """模型调用前——可以注入提醒消息。"""
        raise NotImplementedError

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        """工具执行后——可以基于结果注入消息。"""
        raise NotImplementedError

    def should_stop(self, context, state) -> str | None:
        """是否应该停止——返回停止原因或 None。"""
        raise NotImplementedError
```

`PolicyRunner` 是一个简单的组合器，按顺序调用所有策略：

```python
# core/policy/base.py
class PolicyRunner:
    def __init__(self, policies: list[RunPolicy]):
        self._policies = policies

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        messages = []
        for policy in self._policies:
            messages.extend(policy.before_model_call(context, state))
        return messages

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        messages = []
        for policy in self._policies:
            messages.extend(policy.after_tool_batch(context, state, batch_result))
        return messages

    def should_stop(self, context, state) -> str | None:
        for policy in self._policies:
            decision = policy.should_stop(context, state)
            if decision is not None:
                return decision  # 第一个要停止的策略优先
        return None
```

### 5.3 钩子点在 QueryLoop 中的位置

```python
# core/query/loop.py（简化版）
class QueryLoop:
    def run(self, ...) -> QueryResult:
        state = RunState()
        while True:
            # ── 钩子点 1: before_model_call ──
            before_messages = policy_runner.before_model_call(session_state, state)
            if before_messages:
                store.extend(before_messages)

            # 调用模型
            view = view_builder.build(session_state, run_state=state)
            model_resp = model_gateway.call_once(view.messages, tools=active_tools)
            store.append(model_resp.to_message())

            if model_resp.tool_calls:
                # 执行工具
                batch = tool_runtime.execute_batch(parsed_calls)
                store.extend(batch.tool_results)
                _apply_batch_control_plane(state, batch)

                # ── 钩子点 2: after_tool_batch ──
                after_messages = policy_runner.after_tool_batch(session_state, state, batch)
                if after_messages:
                    store.extend(after_messages)

                # ── 钩子点 3: should_stop ──
                stop_reason = policy_runner.should_stop(session_state, state)
                if stop_reason == "max_turns":
                    state.stop_reason = "max_turns"
                    store.append({"role": "user", "content": "你已达到迭代安全上限..."})
                    continue  # 再调一次模型（不带工具），获取最终回答

                if batch.barrier is not None:
                    continue  # barrier 强制回模型
                continue

            if model_resp.has_final_text:
                return QueryResult(...)

            # 恢复逻辑
            decision = recovery.handle(model_resp, state)
            ...
```

### 5.4 两个具体策略的实现

#### MaxTurnsPolicy（终止型策略）

最简单的策略——只关心"是否应该停止"，不注入任何消息：

```python
# core/policy/max_turns.py
class MaxTurnsPolicy:
    def __init__(self, max_turns: int):
        self._max_turns = max_turns

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        return []

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, context, state) -> str | None:
        if state.turn_count >= self._max_turns:
            return "max_turns"
        return None
```

注意 `should_stop` 返回 `"max_turns"` 后，QueryLoop 不是立即退出——而是：
1. 设置 `state.stop_reason = "max_turns"`
2. 注入一条"请给出最终回复"的用户消息
3. 继续循环（但下一轮 `active_tools = None`，即禁用所有工具）
4. 模型被迫只能输出文本回答
5. 然后 `QueryResult(stop_reason=StopReason.MAX_TURNS)` 返回

这种"优雅降级"而不是"硬停止"的设计，确保用户总能得到一个有用的回答。

#### TodoPlanningPolicy（影响型策略）

不终止循环，但通过注入消息来影响模型行为：

```python
# core/policy/todo_tracking.py
class TodoPlanningPolicy:
    STALE_ASSISTANT_TURNS = 4

    def before_model_call(self, session_state, run_state) -> list[dict[str, str]]:
        # 触发条件 1：skill 刚展开，需要重新规划
        if run_state.todo_replan_required:
            return [{
                "role": "user",
                "content": (
                    "<system-reminder type=\"post_skill_replan\">"
                    "某个 skill 刚刚展开。若任务是多步骤，请先刷新 todo，并让计划对齐当前 workflow。"
                    "</system-reminder>"
                ),
            }]

        # 触发条件 2：已经 4 轮没碰 todo 了，可能忘了
        todo_state = session_state.todo_state
        if todo_state.items and run_state.assistant_turns_since_todo >= self.STALE_ASSISTANT_TURNS:
            if todo_state.last_reminder_turn == run_state.turn_count:
                return []  # 防止同一轮重复提醒
            todo_state.last_reminder_turn = run_state.turn_count
            snapshot = "\n".join(
                f"- [{item.status}] {item.content}"
                for item in todo_state.items
            )
            return [{
                "role": "user",
                "content": (
                    "<system-reminder type=\"todo_stale\">\n"
                    "当前计划可能已过时，请先刷新 todo。\n"
                    f"{snapshot}\n"
                    "</system-reminder>"
                ),
            }]
        return []

    def after_tool_batch(self, session_state, run_state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, session_state, run_state) -> str | None:
        return None  # 永不停止
```

### 5.5 策略的两种角色

| 角色 | 行为 | 典型策略 |
|---|---|---|
| **终止型** | 不注入消息，在 `should_stop` 中返回停止原因 | `MaxTurnsPolicy` |
| **影响型** | 在 `before_model_call` 中注入消息，`should_stop` 永远返回 None | `TodoPlanningPolicy` |

一个策略也可以同时兼具两种角色（比如"超过预算就停止，否则注入费用提醒"）。

### 5.6 扩展性

新增一个策略只需要三步：

1. 创建一个类，实现 `RunPolicy` 的三个方法
2. 在 `SessionEngine` 初始化时注册到 `PolicyRunner`
3. `QueryLoop` 一行都不用改

```python
# 假设要加一个 TokenBudgetPolicy
class TokenBudgetPolicy:
    def __init__(self, max_tokens: int):
        self._max_tokens = max_tokens

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        total = sum(state.usage_delta.values())
        if total > self._max_tokens * 0.8:
            return [{"role": "user", "content": "<system-reminder>Token 使用已超过 80% 预算</system-reminder>"}]
        return []

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        return []

    def should_stop(self, context, state) -> str | None:
        total = sum(state.usage_delta.values())
        if total > self._max_tokens:
            return "budget_exceeded"
        return None
```

---

## 6. Skills System：本地文件即能力的动态扩展

### 6.1 问题：硬编码能力的局限

模型的能力受限于它的工具和系统提示。如果要给它新能力（比如"TDD 开发"的完整工作流），传统做法是：
1. 改系统提示 → 硬编码，不够灵活
2. 加新工具 → 需要写代码
3. 用 few-shot 示例 → 浪费 token，无法承载复杂工作流

### 6.2 解决方案：文件系统即能力注册表

把"能力"定义为本地文件系统中的 markdown 文件：

```
.harness/skills/
  tdd/
    SKILL.md              ← frontmatter(元数据) + 正文(指令)
    references/
      test_patterns.md    ← 可选参考文件
  debugging/
    SKILL.md
    references/
      stack_trace_guide.md
```

### 6.3 三个阶段

#### 阶段 A — 发现（Discovery）

在会话启动时，`SkillRegistry` 扫描 `.harness/skills/` 目录：

```python
# core/skills/registry.py
class SkillRegistry:
    def discover(self, skills_dir: Path, *, working_dir=None) -> dict[str, SkillMeta]:
        self._catalog = {}
        self._cache = {}

        if not skills_dir.is_dir():
            return {}

        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                self.errors[skill_dir.name] = "SKILL.md missing"
                continue

            # 解析 YAML frontmatter
            meta_dict, _ = _parse_skill_markdown(skill_file)
            name = str(meta_dict["name"])
            description = str(meta_dict["description"])
            when_to_use = meta_dict.get("when-to-use")
            references = _parse_references(meta_dict, skill_dir=skill_dir, working_dir=...)

            self._catalog[skill_dir.name] = SkillMeta(
                skill_id=skill_dir.name,
                name=name,
                description=description,
                when_to_use=when_to_use,
                skill_dir=skill_dir,
                skill_file=skill_file,
                references=references,
            )
        return dict(self._catalog)
```

这发生在 `SessionEngine.bootstrap()` 中：

```python
# core/session/engine.py:55-67
def bootstrap(self) -> None:
    if self._bootstrapped:
        return
    working_dir = Path(self._tool_context.working_dir)
    skills_dir = working_dir / ".harness" / "skills"
    self._state.skill_catalog = self._skill_registry.discover(skills_dir, working_dir=working_dir)
    self._state.skills_revision = compute_skills_revision(self._state.skill_catalog)
    self._bootstrap_session_messages()
    self._bootstrapped = True
```

**注意：** 此时只读元数据（名称、描述、when-to-use），不读技能正文。正文在模型真正需要时才加载（懒加载）。

`compute_skills_revision` 计算整个目录的版本哈希（基于所有 SKILL.md 的修改时间），用于检测技能是否有更新。

#### 阶段 B — 注入（Injection）

模型通过 `skill` 工具**自主决定**什么时候激活哪个技能。这是关键——不是开发者在代码中硬编码"什么时候用什么技能"，而是模型根据对话上下文自己判断。

skill 工具的完整实现：

```python
# core/tools/builtin/skill.py
SCHEMA = {
    "name": "skill",
    "description": (
        "Load a local skill immediately. The skill instructions are injected into "
        "context now, and the current tool batch stops so you can re-evaluate the "
        "next action with the skill visible."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The skill ID to load (from <available-skills> catalog)",
            },
            "args": {
                "type": "string",
                "description": "Optional arguments to pass to the skill",
            },
        },
        "required": ["skill"],
    },
}

def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    skill_id = args.get("skill", "").strip()
    if not skill_id:
        return ToolResult(output="Missing skill parameter", success=False, error="missing_params")

    state = context.session_state
    registry = context.skill_registry

    # 验证技能存在
    if skill_id not in state.skill_catalog:
        return ToolResult(output=f"Skill not found: {skill_id}", success=False, error="not_found")

    # 懒加载技能内容
    try:
        content = registry.load(skill_id)
    except (ValueError, KeyError) as exc:
        return ToolResult(output=f"Failed to load skill: {exc}", success=False, error="load_failed")

    # 构建注入消息 + 预算检查 + 记录
    try:
        message = apply_skill_invocation(state=state, skill_id=skill_id, content=content, turn=context.turn_count)
    except ValueError as exc:
        return ToolResult(output=str(exc), success=False, error="budget_exceeded")

    # 返回带控制平面信号的 ToolResult
    return ToolResult(
        output=f"Skill loaded: {skill_id}. Re-evaluate your next action using the injected skill guidance.",
        success=True,
        injected_messages=[message],                                    # 技能内容注入对话
        barrier=ExecutionBarrier(stop_after_tool=True, reason="skill_expanded"),  # 触发屏障
    )
```

注意最后返回的 `ToolResult` 携带了两个控制平面信号：
- `injected_messages`：技能的 XML 内容将作为系统消息追加到对话中
- `barrier`：触发 `skill_expanded` 屏障，强制循环回模型

#### 阶段 C — 生效（Effect）

技能的生效依赖多个系统的联动：

**Step 1：** `apply_skill_invocation` 构建 XML 系统消息

```python
# core/skills/runtime.py
def build_skill_runtime_message(skill_id: str, content: SkillContent) -> dict[str, str]:
    lines = [
        "<skill-runtime>",
        f'  <skill id="{skill_id}" source="local-inline">',
        "    <instruction>",
        content.body,             # SKILL.md 的正文——技能的完整指令
        "    </instruction>",
    ]
    if content.reference_bodies:
        lines.append("    <reference-files>")
        for path, body in content.reference_bodies.items():
            lines.append(f'      <file path="{path}">')
            lines.append(body)    # 参考文件的完整内容
            lines.append("      </file>")
        lines.append("    </reference-files>")
    lines.extend(["  </skill>", "</skill-runtime>"])
    return {"role": "system", "content": "\n".join(lines)}
```

**Step 2：** `ensure_inline_skill_budget` 检查 24KB 预算

```python
def ensure_inline_skill_budget(*, state, new_content: str, max_chars: int = 24_000) -> None:
    used_chars = sum(
        len(message.get("content", ""))
        for message in state.conversation_messages
        if message.get("role") == "system" and "<skill-runtime>" in message.get("content", "")
    )
    if used_chars + len(new_content) > max_chars:
        raise ValueError(f"Inline skill budget exceeded: {used_chars + len(new_content)} > {max_chars}")
```

24KB 预算限制了技能注入的总量，防止 token 爆炸。

**Step 3：** `ToolExecutorRuntime` 的 barrier 感知执行

当一批工具调用中包含 `skill` 工具时，Runtime 切换到 `_execute_with_barrier` 模式：

```python
# core/tools/runtime.py:80-82
def execute_batch(self, tool_calls: list[ToolCall]) -> ToolBatchResult:
    if any(call.name == "skill" for call in tool_calls):
        return self._execute_with_barrier(tool_calls)
    # ...
```

`_execute_with_barrier` 顺序执行所有工具。一旦某个工具返回 barrier，后续所有工具都返回跳过结果：

```python
def _execute_with_barrier(self, tool_calls: list[ToolCall]) -> ToolBatchResult:
    ordered_results = {}
    barrier = None

    for pos, call in enumerate(tool_calls):
        if barrier is not None:
            # 屏障之后的调用全部跳过
            ordered_results[call.idx] = ToolResult(
                output=f"(skipped: superseded by {barrier.reason} barrier; re-issue after re-evaluation if still needed)",
                success=False, error="skipped",
            )
            continue

        result = self._run_single(call)
        ordered_results[call.idx] = result
        # ... 收集 injected_messages, context_patches ...
        if result.barrier is not None and result.barrier.stop_after_tool:
            barrier = result.barrier
```

为什么这样做？因为 skill 展开后，模型需要先看到技能内容，才能决定接下来的行动。之前排队的工具调用可能不再合适（技能可能改变了工作流）。

**Step 4：** QueryLoop 应用控制平面

```python
# core/query/loop.py:228-241
# 注入技能内容消息
if batch.injected_messages:
    store.extend(batch.injected_messages)

# 应用控制平面
_apply_batch_control_plane(state, batch)
# → barrier_reason = "skill_expanded"
# → todo_replan_required = True

# barrier 强制回模型
if batch.barrier is not None:
    continue  # 不检查 should_stop，直接回模型
```

**Step 5：** 下一轮循环中，策略系统检测到 `todo_replan_required`

```python
# TodoPlanningPolicy.before_model_call
if run_state.todo_replan_required:
    return [{"role": "user", "content": "<system-reminder>某个 skill 刚刚展开。请先刷新 todo</system-reminder>"}]
```

**Step 6：** 模型看到技能内容 + 重新规划提醒，按照技能指令工作

---

## 7. 三大系统的联动：一个完整的请求生命周期

以用户输入 "帮我用 TDD 方式实现一个计算器" 为例，展示三个系统如何协作：

```
用户输入："帮我用 TDD 方式实现一个计算器"
│
├── SessionEngine.bootstrap()
│   ├── SkillRegistry.discover() → 找到 tdd, debugging 等 skill
│   ├── compute_skills_revision() → 计算版本哈希
│   └── 注入系统提示 + 环境消息
│
├── SessionEngine.submit_user_message("帮我用 TDD 方式实现一个计算器")
│   └── 追加用户消息到 SessionStore
│
└── QueryLoop.run() 进入循环
    │
    ┌─────────── 第 1 轮 ───────────┐
    │                                │
    │  [Policy] before_model_call    │
    │    → TodoPlanning: 无提醒（刚开始）│
    │                                │
    │  [ViewBuilder] 构建视图         │
    │    → messages = [system, env, user]│
    │    → tools = 全部工具           │
    │                                │
    │  [ModelGateway] 调用 Anthropic  │
    │    → 模型返回: tool_calls =     │
    │      [skill("tdd")]            │
    │                                │
    │  [ToolExecutor] execute_batch  │
    │    → 检测到 skill 工具          │
    │    → 切换到 barrier 感知模式     │
    │    → skill.handle() 执行:       │
    │      1. registry.load("tdd")   │
    │      2. build_skill_runtime_message()│
    │      3. 检查 24KB 预算          │
    │      4. 返回 ToolResult(        │
    │           injected_messages=[XML],│
    │           barrier=ExecutionBarrier│
    │             ("skill_expanded")  │
    │         )                       │
    │                                │
    │  [QueryLoop] 处理结果           │
    │    → store.extend(injected_messages)│
    │    → 记录 SkillEvent            │
    │                                │
    │  [Control Plane]               │
    │    _apply_batch_control_plane:  │
    │    → barrier_reason = "skill_expanded"│
    │    → todo_replan_required = True│
    │                                │
    │  [Policy] after_tool_batch     │
    │    → 无消息                     │
    │                                │
    │  [Policy] should_stop          │
    │    → None（继续）               │
    │                                │
    │  barrier != None → continue    │
    │    （强制回模型，不检查其他条件） │
    └────────────────────────────────┘
                    │
    ┌─────────── 第 2 轮 ───────────┐
    │                                │
    │  [Policy] before_model_call    │
    │    → TodoPlanningPolicy 检测到  │
    │      todo_replan_required=True │
    │    → 注入: "某个 skill 刚刚展开, │
    │      请刷新 todo"               │
    │                                │
    │  [ViewBuilder] 构建视图         │
    │    → messages = [system, env,  │
    │       user, assistant,         │
    │       tool_result,             │
    │       <skill-runtime>tdd...</>,│ ← 技能内容可见
    │       system-reminder(replan)] │ ← 策略注入的提醒
    │    → tools = 全部工具           │
    │                                │
    │  [ModelGateway] 调用 Anthropic  │
    │    → 模型看到 TDD 技能指令      │
    │    → 模型返回: tool_calls =     │
    │      [todo(create items),      │
    │       write_file(test),        │
    │       write_file(impl)]        │
    │                                │
    │  [ToolExecutor] execute_batch  │
    │    → 不含 skill → 正常分批执行   │
    │    → todo: 创建计划项           │
    │    → write_file × 2: 写文件    │
    │                                │
    │  [Control Plane]               │
    │    → todo 成功 + 无 barrier    │
    │    → todo_replan_required = False│
    │    → assistant_turns_since_todo = 0│
    │                                │
    │  [Renderer]                    │
    │    → show_progress(todo_items) │
    │                                │
    │  → continue                    │
    └────────────────────────────────┘
                    │
    ┌─────────── 第 3-N 轮 ──────────┐
    │                                │
    │  模型按 TDD 循环执行:           │
    │    write_test → run_test (red) │
    │    → write_impl → run_test (green)│
    │    → refactor → run_test       │
    │    → ...                       │
    │                                │
    │  如果连续 4 轮没碰 todo:        │
    │    [Policy] TodoPlanningPolicy │
    │    → 注入 "计划可能已过时" 提醒  │
    │                                │
    │  最终: 模型返回最终文本          │
    │  → QueryResult(COMPLETED)      │
    └────────────────────────────────┘
```

### 联动总结

| 步骤 | 系统 | 作用 |
|---|---|---|
| 技能发现 | Skills System | 扫描文件，构建目录 |
| 模型决定使用技能 | LLM | 模型自主判断需要 TDD 技能 |
| 技能加载 | Skills System | 懒加载 SKILL.md 正文和参考文件 |
| 内容注入 | Control Plane (`injected_messages`) | 技能 XML 作为系统消息注入对话 |
| 循环中断 | Control Plane (`barrier`) | 强制回模型，让它先看技能内容 |
| 标记重新规划 | Control Plane (`todo_replan_required`) | 写入 RunState |
| 注入重新规划提醒 | Policy System | `TodoPlanningPolicy` 检测标记，注入提醒 |
| 模型看到技能+提醒 | LLM | 按技能指令规划并执行 |
| 更新计划后重置 | Control Plane | `todo_replan_required = False` |
| 定期检查计划 | Policy System | 4 轮未更新时注入过期提醒 |
| 轮次保护 | Policy System | `MaxTurnsPolicy` 防止无限循环 |

**核心观察：** 三个系统（Control Plane、Policy、Skills）中没有任何一个直接调用另一个。它们通过 `RunState` 间接通信：

- Control Plane **写入** `todo_replan_required`
- Policy **读取** `todo_replan_required`
- 两者都不知道对方的存在

---

## 8. 设计原则总结

### 8.1 数据驱动 vs 调用驱动

| | 调用驱动 | 数据驱动（Harness 的做法） |
|---|---|---|
| 工具返回后 | 直接调用回调函数修改循环状态 | 返回数据结构（ToolResult），由 Runtime 聚合 |
| 循环读取 | 状态散落在各处 | 统一从 RunState 读取 |
| 耦合度 | 工具需要 import 循环逻辑 | 工具只 import dataclass |
| 可测试性 | 需要 mock 整个循环 | 构造一个 ToolBatchResult 就能测试 |

### 8.2 钩子优于继承

不把所有逻辑塞进主循环，而是定义清晰的钩子点，让横切关注点通过接口（Protocol）参与。

**新增一个策略**：写一个类 → 注册到 PolicyRunner → QueryLoop 零改动。

### 8.3 单一状态源

`RunState` 是一次查询运行的唯一可变状态。所有跨迭代的信息都通过这个状态对象传递，不散落在全局变量或闭包中。

| 字段 | 写入者 | 读取者 |
|---|---|---|
| `allowed_tools_override` | Control Plane | ViewBuilder |
| `barrier_reason` | Control Plane | QueryLoop |
| `todo_replan_required` | Control Plane | Policy |
| `assistant_turns_since_todo` | QueryLoop | Policy |
| `turn_count` | QueryLoop | Policy (should_stop) |

### 8.4 分层隔离

```
SessionEngine  → 编排层（会话、技能发现、引导）
QueryLoop      → 循环层（模型调用、工具执行、状态流转）
ToolExecutor   → 执行层（并行/串行、屏障、聚合）
Policy/Recovery → 横切层（策略钩子、恢复机制）
```

每层只知道相邻层。`ToolExecutor` 不知道 `Policy` 的存在，`Policy` 不知道 `ToolExecutor` 的存在。它们通过 `RunState` 和 `ToolBatchResult` 间接通信。

### 8.5 懒加载

Skills 系统在 `discover()` 时只读元数据，正文在 `load()` 时才读取。这避免了一次性加载所有技能的 token 开销。

### 8.6 优雅降级

`MaxTurnsPolicy` 不是硬停止，而是"禁用工具，再给模型一次机会输出最终回答"。RecoveryManager 不是遇到空响应就报错，而是注入引导消息让模型重新尝试。

---

## 附录：关键文件索引

| 文件 | 行数 | 核心内容 |
|---|---|---|
| `core/query/loop.py` | 267 | QueryLoop 主循环、`_apply_batch_control_plane` |
| `core/tools/runtime.py` | 321 | ToolExecutorRuntime、ToolBatchResult、barrier 感知执行 |
| `core/tools/context.py` | 155 | ToolResult、ContextPatch、ExecutionBarrier、ToolUseContext |
| `core/policy/base.py` | 39 | RunPolicy Protocol、PolicyRunner 组合器 |
| `core/policy/max_turns.py` | ~15 | MaxTurnsPolicy 终止型策略 |
| `core/policy/todo_tracking.py` | 43 | TodoPlanningPolicy 影响型策略 |
| `core/skills/registry.py` | 171 | SkillRegistry 发现与懒加载 |
| `core/skills/runtime.py` | 46 | 技能注入消息构建、预算检查 |
| `core/skills/models.py` | 57 | SkillMeta、SkillContent、InvokedSkillRecord |
| `core/tools/builtin/skill.py` | 77 | skill 工具实现（控制平面触发点） |
| `core/session/engine.py` | 107 | SessionEngine 编排器 |
| `core/session/view_builder.py` | 24 | MessageViewBuilder（控制平面生效点） |
| `core/query/state.py` | 28 | RunState（控制平面 + 策略的共享状态） |
| `core/query/recovery.py` | 24 | RecoveryManager |
| `core/session/state.py` | 40 | SessionState（会话级状态） |
| `core/session/store.py` | 23 | SessionStore（消息列表操作） |
