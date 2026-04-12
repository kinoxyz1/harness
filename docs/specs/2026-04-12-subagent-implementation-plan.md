# Subagent 实施计划

> **For agentic workers:** Prefer implementing this plan task-by-task. Keep steps small, verify each stage, and do not start `fork` / background / worktree work in this plan.

**Goal:** 基于 [`2026-04-12-subagent-design-v2.md`](/Users/kino/works/kino/harness/docs/specs/2026-04-12-subagent-design-v2.md) 为 harness 实现第一阶段可落地的 subagent 能力：`subagent` tool 入口、最小 subagent runtime、`explore` / `plan` / `general` 三种 agent type、独立上下文、todo 隔离、quiet 模式、结构化运行结果。

**Architecture:** 保持 “subagent 作为 tool 入口” 不变，但新增 `core/subagent_runtime.py` 负责 agent type、上下文构建、工具过滤、结果收集。`AgentLoop.run()` 升级为可接收外部 `ToolUseContext` 并返回 `AgentRunResult`。quiet 模式贯穿 `agent.py`、`llm_client.py`、`runtime.py`，避免 subagent 刷屏。

**Tech Stack:** Python 3.10+, dataclasses, Enum, rich, pytest

---

## File Structure

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/subagent_runtime.py` | 创建 | Subagent runtime、agent type、请求/结果类型、工具过滤 |
| `core/tools/subagent.py` | 创建 | 对 LLM 暴露的 `subagent` tool |
| `core/agent.py` | 修改 | `AgentRunResult`、外部 `tool_context` 注入、返回 stop reason |
| `core/llm_client.py` | 修改 | 支持 quiet 模式 |
| `core/runtime.py` | 修改 | 支持 quiet 模式 |
| `core/renderer.py` | 修改 | 可选 `QuietRenderer`，但不单独承担静默职责 |
| `core/tools/__init__.py` | 修改 | `get_file_state()` 的 mtime 失效检测、记录受管修改文件 |
| `core/tools/todo.py` | 修改 | `save_snapshot()` / `restore_snapshot()` / `clear_state()` |
| `core/tools/write_file.py` | 修改 | 写成功后记录受管修改路径 |
| `core/tools/edit_file.py` | 修改 | 写成功后记录受管修改路径 |
| `tests/test_subagent_runtime.py` | 创建 | runtime/agent type/tool filtering/todo 隔离测试 |
| `tests/test_agent_run_result.py` | 创建 | `AgentLoop.run()` 新返回值测试 |

---

## Milestone 1: 先把 `AgentLoop` 变成可复用 runtime

**目标:** 不先写 `subagent tool`，先把主循环改造成“外部可驱动、可拿到结构化结果”的形态。这是后续所有能力的基础。

**Files:**
- Modify: `core/agent.py`
- Create: `tests/test_agent_run_result.py`

- [ ] **Step 1: 在 `core/agent.py` 中新增 `AgentRunResult`**

建议字段：

```python
@dataclass
class AgentRunResult:
    final_output: str
    success: bool
    stop_reason: str
    turns_used: int
    files_modified: list[str]
```

- [ ] **Step 2: 修改 `AgentLoop.run()` 签名**

改为：

```python
def run(
    self,
    messages: list[dict[str, Any]],
    *,
    tool_context: ToolUseContext | None = None,
) -> AgentRunResult:
```

要求：

- 调用方未传 `tool_context` 时，保持现有默认行为
- 调用方传入时，必须使用该 context，而不是内部重建

- [ ] **Step 3: 给 `run()` 增加明确的 stop reason**

至少覆盖：

- `completed`
- `max_turns`
- `api_error`
- `tool_error`
- `empty_response`

- [ ] **Step 4: 让 `_run_tool_loop()` 能把 `max_turns` 命中信息传回 `run()`**

不要只在 messages 里塞提示再 break；需要有结构化状态。

- [ ] **Step 5: 让 `run()` 返回真实的 `turns_used`**

来源应是最终使用的 `tool_context.turn_count`，而不是局部变量猜测。

- [ ] **Step 6: 添加测试 `tests/test_agent_run_result.py`**

至少覆盖：

- 无工具调用时 `run()` 仍返回 `AgentRunResult`
- 使用外部 `tool_context` 时不会内部重建
- 达到 `max_turns` 时 `stop_reason == "max_turns"`

- [ ] **Step 7: 手动回归**

验证现有主入口行为不变：

```bash
cd /Users/kino/works/kino/harness && python 01_agent_loop.py
```

---

## Milestone 2: 给运行链路加真正的 quiet 模式

**目标:** subagent 可以静默运行，不向主终端打印内部 llm/runtime 日志。

**Files:**
- Modify: `core/llm_client.py`
- Modify: `core/runtime.py`
- Modify: `core/agent.py`
- Modify: `core/renderer.py`

- [ ] **Step 1: 定义统一显示配置**

建议新增：

```python
@dataclass(frozen=True)
class RunDisplayOptions:
    quiet: bool = False
```

位置可选：

- `core/agent.py`
- 或单独抽到 `core/run_options.py`

- [ ] **Step 2: `OpenAIClient.call()` 支持 quiet**

要求：

- quiet 时不打印 “正在思考...”
- quiet 时不打印 token/finish 日志
- 非 quiet 时保留当前行为

- [ ] **Step 3: `ToolExecutorRuntime` 支持 quiet**

要求：

- quiet 时不打印 batch 信息
- quiet 时不打印执行中进度条
- quiet 时不打印完成/异常日志

- [ ] **Step 4: `AgentLoop` 将 display 选项贯穿到 llm/runtime**

主 agent 默认 `quiet=False`，subagent 运行时显式传 `quiet=True`。

- [ ] **Step 5: 如需要，再补 `QuietRenderer`**

说明：

- `QuietRenderer` 只能作为 renderer 层补充
- 不要把“静默”职责只压给 renderer

- [ ] **Step 6: 手动验证**

验证 quiet 模式下，嵌套运行不会把内部日志打到主终端。

---

## Milestone 3: 完善 `ToolUseContext` 的隔离与文件状态语义

**目标:** 让 subagent 拥有自己的文件认知，同时让主 agent 在子代理改文件后自动失效旧认知。

**Files:**
- Modify: `core/tools/__init__.py`
- Modify: `core/tools/write_file.py`
- Modify: `core/tools/edit_file.py`

- [ ] **Step 1: `get_file_state()` 加 mtime 校验**

要求：

- 文件被外部修改后自动失效
- 文件被删除后自动失效

- [ ] **Step 2: 给 `ToolUseContext` 增加受管修改记录**

建议字段：

```python
self._files_modified: list[str] = []
```

建议 API：

```python
@property
def files_modified(self) -> list[str]: ...

def mark_file_modified(self, path: str) -> None: ...
```

- [ ] **Step 3: `write_file.py` / `edit_file.py` 在成功后调用 `mark_file_modified()`**

要求：

- 去重
- 路径统一用绝对路径字符串

- [ ] **Step 4: 补测试**

至少覆盖：

- mtime 变化后 `get_file_state()` 返回 `None`
- 多次编辑同一文件时 `files_modified` 不重复

---

## Milestone 4: 实现 todo save/restore 隔离

**目标:** subagent 可以单独使用 todo，不污染主 agent 的规划状态。

**Files:**
- Modify: `core/tools/todo.py`
- Create/Modify: `tests/test_subagent_runtime.py`

- [ ] **Step 1: 在 `core/tools/todo.py` 中实现快照 API**

新增：

```python
def save_snapshot() -> PlanningState: ...
def restore_snapshot(snapshot: PlanningState) -> None: ...
def clear_state() -> None: ...
```

要求：

- 快照应深拷贝，不要共享 list 引用
- `rounds_since_update` 一并保存

- [ ] **Step 2: 测试 todo 快照语义**

至少覆盖：

- 保存后清空，再恢复，状态完全一致
- 子代理中的 todo 更新不会污染外部快照

---

## Milestone 5: 实现 `core/subagent_runtime.py`

**目标:** 把所有真正的 subagent 逻辑集中到 runtime，不塞进 tool handler。

**Files:**
- Create: `core/subagent_runtime.py`
- Create: `tests/test_subagent_runtime.py`

- [ ] **Step 1: 定义内部类型**

至少包含：

- `SubagentType`
- `SubagentContextMode`
- `SubagentStopReason`
- `SubagentDefinition`
- `SubagentRequest`
- `SubagentRunResult`

- [ ] **Step 2: 定义三种内置 agent**

- `EXPLORE_AGENT`
- `PLAN_AGENT`
- `GENERAL_AGENT`

要求：

- `explore` / `plan` 只读
- `general` 可读写
- 所有类型都禁止再调 `subagent`

- [ ] **Step 3: 实现工具过滤**

建议提供：

```python
def build_subagent_tools(definition: SubagentDefinition) -> list[dict[str, Any]]:
```

要求：

- `explore` / `plan` 只允许 `find`, `read_file`, `todo`
- `general` 使用全工具集，但排除 `subagent`

- [ ] **Step 4: 实现 system prompt 组装**

要求：

- 保留框架层 system prompt
- 按 definition 决定是否加载项目 context
- 追加 `agent_type` suffix

- [ ] **Step 5: 实现 `SubagentRuntime.run()`**

流程：

1. 选择 definition
2. 组装 prompt/messages
3. 构造独立 `ToolUseContext`
4. 以 quiet 模式调用 `AgentLoop.run()`
5. 收集 `final_output` / `stop_reason` / `turns_used` / `files_modified`
6. 返回 `SubagentRunResult`

- [ ] **Step 6: 实现结果压缩渲染函数**

例如：

```python
def render_subagent_summary(result: SubagentRunResult) -> str:
    ...
```

要求：

- 成功与失败格式不同
- `plan` 类型输出应包含关键文件提示
- 不回传长篇中间过程

- [ ] **Step 7: 补测试**

至少覆盖：

- 三种 agent type 的工具过滤正确
- `general` 禁止 `subagent`
- runtime 使用独立 `ToolUseContext`
- 返回的 `turns_used` / `files_modified` 来自子上下文

---

## Milestone 6: 实现 `core/tools/subagent.py`

**目标:** 对 LLM 暴露稳定的 `subagent` tool。

**Files:**
- Create: `core/tools/subagent.py`
- Modify: `tests/test_subagent_runtime.py`

- [ ] **Step 1: 定义 `SCHEMA`**

参数：

- `task`
- `agent_type`
- `description`
- `max_turns`

- [ ] **Step 2: 实现参数解析**

建议：

- 默认为 `agent_type="general"`
- 未知 `agent_type` 返回 validation error
- `max_turns <= 0` 返回 validation error

- [ ] **Step 3: 在 `handle()` 中做 todo save/restore**

流程：

1. `save_snapshot()`
2. `clear_state()`
3. 调 `SubagentRuntime.run()`
4. `restore_snapshot()`

- [ ] **Step 4: 将 `SubagentRunResult` 渲染为普通 `ToolResult`**

要求：

- `ToolResult.success` 取 `result.success`
- `ToolResult.output` 为低噪声摘要

- [ ] **Step 5: 验证自动发现注册**

确认 `core/tools/__init__.py` 的 auto discovery 能正常发现 `subagent.py`。

---

## Milestone 7: 端到端验证

**目标:** 确认从主 agent 发起 subagent 到结果回传的整条链路真实可用。

**Files:**
- Modify: `tests/test_subagent_runtime.py`
- 可选新增: `tests/test_subagent_integration.py`

- [ ] **Step 1: 写集成测试或最小 fake LLM 测试**

至少覆盖：

- 主 agent 能通过 `subagent` tool 调起子代理
- 子代理结果只以一个 tool result 返回主 agent
- 主 agent 的 `messages` 不会被子代理内部过程污染

- [ ] **Step 2: 手动场景验证 `explore`**

示例任务：

```text
请搜索项目里与 todo 状态管理相关的文件，并总结 PlanningState 的职责。
```

- [ ] **Step 3: 手动场景验证 `plan`**

示例任务：

```text
请规划如何为 runtime 增加 quiet 模式，列出关键文件、修改步骤和验证方式。
```

- [ ] **Step 4: 手动场景验证 `general`**

示例任务：

```text
请在隔离上下文中分析并修复某个小 bug，返回结论和修改文件。
```

- [ ] **Step 5: 回归检查**

确认以下旧行为未被破坏：

- 主 agent 普通工具循环
- todo 工具
- 文件读写工具
- 主终端默认日志输出

---

## Deferred Work

以下内容明确不在本计划内：

- `fork` 模式
- 多 subagent 并发
- 后台 subagent
- worktree 隔离
- remote 隔离
- 子代理自定义模型
- swarm / teammate / mailbox

### 额外记录：subagent 结果重复展示

当前系统存在一个已知 UX 问题：

- `subagent` 的 `ToolResult.output` 会先作为工具结果展示一次
- 主 agent 再读取该结果并生成最终回答，用户会再次看到相近内容

后续建议方案：

1. 分离“写回模型的 tool result”和“展示给用户的 tool result”
2. 对 `subagent` 工具在终端中只显示简短状态，而不是完整摘要
3. 保留较完整的工具结果写回 `messages`，避免主 agent 最终回答质量下降

这个问题暂不在本计划中处理，记录下来供后续 UX 优化使用。

这些能力必须等本计划完成并稳定后，另开新 spec 和新 implementation plan。

---

## 验收标准

本计划完成时，应满足：

1. LLM 可以调用 `subagent` tool
2. `explore` / `plan` / `general` 三种类型可用
3. subagent 有独立 `messages` 和独立 `ToolUseContext`
4. `max_turns` 对 subagent 真正生效
5. subagent 的 todo 状态不会污染主 agent
6. 子代理修改文件后，主 agent 的旧文件认知会自动失效
7. quiet 模式下，subagent 不向主终端输出内部 llm/runtime 日志
8. `AgentLoop.run()` 返回结构化结果，不再只是 `None`
9. 整个系统仍保持单进程、同步、可理解的最小复杂度
