# Harness 当前架构设计

> 这份文档只描述当前代码，不讨论历史版本或替代方案。

---

## 1. 总览

Harness 当前实现可以概括成一句话：

> Agent 不是把“完整历史”直接喂给模型，而是把显式状态重组成一份当前视图，再用 transcript slice 补充最近交互轨迹。

当前代码围绕这个原则拆成六层：

1. 入口与依赖装配
2. 会话状态与命令入口
3. 查询循环
4. 模型输入组装
5. 工具运行时与 reducer
6. Anthropic 协议归一化

---

## 2. 主调用链

```text
01_agent_loop.py
  -> handle_input()
       +-- /skills ... -> SessionEngine.handle_command()
       \-- normal text -> SessionEngine.submit_user_message()
                            -> bootstrap()
                            -> SessionStore.append(user)
                            -> QueryLoop.run(...)
                                 -> maintenance updates
                                 -> policy before_model_call
                                 -> MessageViewBuilder.build(...)
                                 -> ModelGateway.call_once(...)
                                 -> SessionStore.append(assistant)
                                 -> ToolExecutorRuntime.execute_batch(...)   # if tool_calls
                                 -> reducers apply updates
                                 -> continue / return QueryResult
```

这条路径里，每个组件的职责刻意收窄：

- `01_agent_loop.py` 只负责 REPL 和依赖装配
- `SessionEngine` 只负责会话级协调
- `QueryLoop` 只负责一次 query 的循环
- `MessageViewBuilder` 只负责构建模型输入视图
- `ToolExecutorRuntime` 只负责执行工具批次
- reducer 是唯一的状态写入口

---

## 3. 会话级与查询级状态

### 3.1 SessionState

`SessionState` 是跨整个 REPL 会话持久存在的 runtime truth。

当前关键字段：

```text
conversation_messages
prompt_cache
discovered_tools
skill_catalog
skill_events
invoked_skills
skills_revision
read_file_state
system_prompt_override
session_metadata
usage_totals
todo_state
```

这些字段大致可以分成四组：

- transcript
  - `conversation_messages`
- skill 相关
  - `skill_catalog`
  - `skill_events`
  - `invoked_skills`
  - `skills_revision`
- 文件与任务 runtime
  - `read_file_state`
  - `todo_state`
- 组装辅助
  - `prompt_cache`
  - `system_prompt_override`

### 3.2 RunState

`RunState` 只在一次 `QueryLoop.run()` 中存在。

当前关键字段：

```text
turn_count
empty_retry_count
stop_reason
last_model_response
tool_calls_executed
files_modified
usage_delta
transition
allowed_tools_override
model_override
effort_override
assistant_turns_since_todo
last_displayed_todo_items
```

它负责表达“本次 query 目前运行到了什么程度，处于什么控制状态”。

---

## 4. SessionEngine：会话编排层

`SessionEngine` 当前做三件事：

### 4.1 bootstrap

首次触发时：

- 从 `working_dir/.harness/skills/` 扫描本地 skills
- 生成 `skill_catalog`
- 计算 `skills_revision`

它是幂等的，多次调用只执行一次。

### 4.2 命令入口

`/skills` 命令不进入 QueryLoop，而是直接走 `handle_command()`：

- `/skills list`
- `/skills show <id>`
- `/skills use <id>`
- `/skills off <id>`
- `/skills reload`

这些命令直接改 `SessionState`，不会向 transcript 注入 `<skill-runtime>` system message。

### 4.3 普通用户消息

普通文本消息会：

1. 先 bootstrap
2. 追加一条 user message 到 `conversation_messages`
3. 把控制权交给 `QueryLoop.run()`

---

## 5. QueryLoop：一次 query 的主循环

`QueryLoop.run()` 可以近似理解为：

```text
while True:
  A. 应用 runtime maintenance updates
  B. policy 注入前置消息
  C. 构建 ModelInputView
  D. 调用模型
  E. 记录 assistant 消息
  F. 若模型要调工具:
       - 执行工具批次
       - 记录 tool messages
       - 应用 session/run updates
       - 更新计数与 transition
       - 检查 stop policy
       - continue
  G. 若模型有最终文本:
       - return QueryResult
  H. 否则走 recovery
```

### 5.1 每轮开始的 maintenance

`collect_runtime_maintenance_updates(session_state)` 会检查 `read_file_state` 里的缓存文件：

- 如果磁盘 `mtime` 和缓存记录不一致
- 就生成 `INVALIDATE_FILE_STATE`

这一步保证文件认知不会因为外部修改而静默过期。

### 5.2 前置策略

当前默认的 `PolicyRunner` 包含：

- `MaxTurnsPolicy`
- `TodoPlanningPolicy`

其中：

- `MaxTurnsPolicy.before_model_call()` 当前不注入任何消息
- `TodoPlanningPolicy.before_model_call()` 会在 Todo 长时间未刷新时注入 `<system-reminder type="todo_stale">`

### 5.3 模型调用后写回 transcript

无论模型是否调用工具，`QueryLoop` 都会先执行：

```text
store.append(model_resp.to_message())
```

因此 assistant 消息总会进入 `conversation_messages`，包括：

- 普通文本
- tool_calls
- reasoning / reasoning_signature（当配置允许）

### 5.4 最大轮次的处理方式

最大轮次不是“立刻报错退出”，而是两阶段处理：

1. 工具批次后发现达到上限
2. 把 `state.stop_reason` 设为 `"max_turns"`
3. 注入一条 user 提醒，要求模型收尾
4. 下一轮调用模型时不再传 `tools`

如果模型在“禁止工具”的最后一轮还试图继续调工具，才返回失败的 `QueryResult(stop_reason=MAX_TURNS)`。

### 5.5 recovery 的真实边界

当前 recovery 很窄，只处理两种情况：

- `finish_reason == "length"`：注入“请继续输出。”
- 无文本且无工具：注入“请直接给出最终答复。”

它不是一个通用错误处理系统，也不处理 API 失败重试。

---

## 6. ModelInputView：模型看到的是一份视图

`MessageViewBuilder.build()` 返回：

```text
ModelInputView
  - system
  - messages
  - tools
  - internal_runtime_view
```

### 6.1 `system`

`system` 由三层拼接：

```text
stable context
runtime context
query overlay
```

当前 `query overlay` 预留为空，主内容集中在前两层。

### 6.2 `messages`

`messages` 是 transcript slice，而不是完整历史。

当前截取算法：

1. 从最后一条消息向前遍历
2. 按字符预算贪心保留
3. 最新消息必保
4. 若某条 `tool` 消息被选中，则向前补齐产生它的 assistant `tool_use`

### 6.3 thinking 清理

`_strip_old_thinking()` 的策略是：

- 找到所有 assistant 消息
- 只保留最近两个 assistant 的 `reasoning`
- 更早的 `reasoning` / `reasoning_signature` 删除
- 但携带 `tool_calls` 的 assistant 消息不会因此被删掉

### 6.4 tools 过滤

如果 `RunState.allowed_tools_override` 不为空，`MessageViewBuilder` 会把工具列表过滤为白名单子集。

当前主线代码里，这个 override 已被 reducer 支持，但默认 builtin tools 中没有广泛使用它。

---

## 7. PromptAssembler：每轮重建 system

`PromptAssembler` 是当前架构的关键之一，因为它负责把“显式状态”重新渲染成模型可消费的上下文。

### 7.1 stable context

来源：

- `_FRAMEWORK_PROMPT`
- `.harness/context/identity.md`
- `.harness/context/style.md`
- `.harness/context/rules.md`
- `<available-skills>`
- `state.system_prompt_override`

缓存 key：

```text
stable_system_prompt:{skills_revision}:{system_prompt_digest}
```

这样可以同时覆盖：

- 技能目录变化
- 系统 prompt 文本变化

### 7.2 runtime context

当前 runtime context 固定包含：

- `<environment>`
- `<active-skills>`
- `<todo-state>`
- `<file-runtime>`

其中 `<file-runtime>` 会：

- 按文件时间倒序渲染
- 只展示内容摘要
- 受单独字符预算限制

### 7.3 internal_runtime_view

`build_internal_runtime_view()` 不发给模型，但会记录：

- `invoked_skills`
- `todo_items`
- `read_file_state`
- `transition`
- `transcript_slice`

这主要用于调试和测试。

---

## 8. Skill 系统：discover/load/activate 三段式

### 8.1 discover

`SkillRegistry.discover()` 只负责扫描目录并解析元信息：

- `name`
- `description`
- `when-to-use`
- `references`

它不会把完整 skill body 提前加载进内存。

### 8.2 load

`SkillRegistry.load(skill_id)` 才会读取：

- skill body
- 声明的 reference 文件
- 或自动发现的 `.md` 文件

并生成 `SkillContent`。

### 8.3 activate

激活 skill 有两条路径：

- `/skills use <id>`
- 模型调用 `skill` 工具

二者最终都会生成 `InvokedSkillRecord`，内容来自 `build_invoked_skill_record()`，核心结构是：

```xml
<skill-runtime>
  <skill id="...">
    Base directory for this skill: ...
    <instruction>...</instruction>
    <reference-files>...</reference-files>
  </skill>
</skill-runtime>
```

### 8.4 引用文件规则

如果 frontmatter 显式声明 `references`：

- 只加载声明的文件
- 路径禁止逃逸 skill 目录

如果没有声明：

- 自动发现 skill 目录里所有 `.md` 文件
- `SKILL.md` 除外

### 8.5 inline budget

激活后的 skill runtime body 会累计占用上下文预算。当前默认总预算是 500,000 字符，超限直接拒绝激活。

---

## 9. Todo 系统：计划是显式状态，不是旁路展示

`todo` 工具当前的特点：

- 输入必须是完整的计划列表，不是 patch
- 最多 20 项
- 最多一个 `in_progress`
- 每项必须有 `content`、`active_form`、`status`

工具返回后会产生：

- `SessionUpdateKind.SET_TODO_ITEMS`
- `RunUpdateKind.RESET_TODO_TURN_COUNTER`

特殊行为：

- 如果提交的全部项都是 `completed`，`todo_state.items` 会清空
- 但 `todo_state.last_completed_items` 会保存刚完成的快照

UI 层利用这些状态来显示：

- 完整进度条
- 当前 in-progress 项
- 全部完成总结

---

## 10. 文件认知系统

### 10.1 FileState

`FileState` 当前保存：

- `content`
- `timestamp`
- `offset`
- `limit`
- `total_lines`

`is_full_read` 用 `offset/limit is None` 判断。

### 10.2 read_file

当前行为：

- 带行号输出
- 单次最多 2000 行
- 二进制文件拒绝
- 大文件优先在工具内部自分页，而不是等 runtime 统一截断
- 成功后写 `UPSERT_FILE_STATE`

### 10.3 edit_file

它依赖 `context.get_file_state()`，只有在以下条件都满足时才允许编辑：

- 有文件缓存
- 缓存是完整读取
- 磁盘 `mtime` 与缓存一致

这是当前代码里非常明确的 read-before-write 设计。

### 10.4 write_file

它负责：

- 创建/覆盖/追加
- 成功后刷新 `UPSERT_FILE_STATE`
- 标记 `MARK_FILE_MODIFIED`

### 10.5 文件失效回收

不是在工具内部偷偷清理，而是每轮 query 开始统一检查并生成 maintenance update，这让失效行为也落在统一 reducer 通道里。

---

## 11. ToolExecutorRuntime：工具批处理执行器

### 11.1 批次划分

`_partition()` 的规则非常简单：

- 连续 readonly 调用组成一个并行批次
- 每个写工具单独形成一个串行批次

### 11.2 执行模型

每个工具调用都跑在独立线程中。这样 runtime 能：

- 轮询执行时长
- 在 debug trace 下打印内部耗时日志
- 在 compact 模式下只发一条轻量状态消息

### 11.3 输出截断

当前 runtime 只截断第一条 tool message 的 content，而且截断点是 `MAX_OUTPUT_CHARS`。这是统一保护，不等于工具自己的语义分页。

例如：

- `read_file` 会先尽量自分页
- runtime 截断只是最后一层保护

### 11.4 扁平化结果

`ToolBatchResult.messages` 由 `_flatten_outcome_messages()` 生成：

- 保证每条消息都有 `role="tool"`
- 保证每条消息都有 `tool_call_id`
- 保持工具调用顺序

---

## 12. reducer：统一 runtime 更新语言

当前 reducer 入口只有三个：

```text
apply_session_update()
apply_run_update()
apply_transition()
```

### 12.1 SessionUpdateKind

当前支持：

- `INVOKE_SKILL`
- `SET_TODO_ITEMS`
- `UPSERT_FILE_STATE`
- `INVALIDATE_FILE_STATE`
- `APPEND_SKILL_EVENT`

### 12.2 RunUpdateKind

当前支持：

- `MARK_FILE_MODIFIED`
- `NARROW_ALLOWED_TOOLS`
- `SET_MODEL_OVERRIDE`
- `SET_EFFORT_OVERRIDE`
- `RESET_TODO_TURN_COUNTER`

这个设计的意义不是“好看”，而是让 QueryLoop 不需要理解某个工具的特殊副作用。

---

## 13. Anthropic 协议归一化

`normalize_messages()` 负责把内部消息格式映射到 Anthropic messages API。

### 13.1 system 抽离

内部的多条 `role=system` 消息会合并成一个顶层 `system` 字符串。

### 13.2 assistant/tool 转 block

assistant 会被转成 content blocks：

- `thinking`
- `text`
- `tool_use`

tool 消息会被合并成一条 user 消息中的多个 `tool_result` blocks。

### 13.3 未闭合 tool_use

如果列表里已经存在至少一条 tool 消息，但某个 assistant `tool_use` 没有对应 `tool_result`，协议层会自动插入一个 `(cancelled)` 占位结果，保证配对完整。

### 13.4 连续同角色消息合并

最终输出会满足 user/assistant 交替约束，避免 Anthropic API 拒绝。

---

## 14. UI 与显示层

当前显示分两层：

- `RichRenderer`
- `RunDisplayOptions`

`RichRenderer` 负责：

- 展示 thinking
- 展示 assistant 文本
- 展示 tool call / tool result
- 展示 todo 进度

`RunDisplayOptions.runtime_trace` 当前有两种模式：

- `compact`
- `debug`

默认是 `compact`。这意味着：

- 不打印 `[Runtime] ...` 内部追踪日志
- 只在必要时显示简洁状态

---

## 15. 当前默认能力边界

默认注册给模型的工具只有：

```text
bash
edit_file
find
read_file
skill
todo
write_file
```

当前仓库虽然保留了：

- `core/session/subagent.py`

但主入口没有把它注册成 builtin tool，因此它属于内部 runtime 代码，不属于当前默认 REPL 能力的一部分。

---

## 16. 推荐阅读顺序

按当前代码结构，推荐按下面顺序读：

1. [`01_agent_loop.py`](/Users/kino/works/kino/harness/01_agent_loop.py)
2. [`core/session/engine.py`](/Users/kino/works/kino/harness/core/session/engine.py)
3. [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py)
4. [`core/session/view_builder.py`](/Users/kino/works/kino/harness/core/session/view_builder.py)
5. [`core/prompt/assembler.py`](/Users/kino/works/kino/harness/core/prompt/assembler.py)
6. [`core/llm/protocol.py`](/Users/kino/works/kino/harness/core/llm/protocol.py)
7. [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py)
8. [`core/query/reducers.py`](/Users/kino/works/kino/harness/core/query/reducers.py)
9. [`tests/session/test_state_assembled_runtime.py`](/Users/kino/works/kino/harness/tests/session/test_state_assembled_runtime.py)
10. [`tests/test_protocol.py`](/Users/kino/works/kino/harness/tests/test_protocol.py)

---

## 17. 总结

Harness 当前实现最重要的设计不是“做了哪些工具”，而是这三个约束：

1. runtime truth 用显式状态保存
2. 每轮模型输入由视图组装器重建
3. 工具只返回结构化 updates，真正写状态统一经 reducer 落地

这三点把系统从“基于历史消息拼运气”变成了“基于状态重组视图”的 Agent runtime。
