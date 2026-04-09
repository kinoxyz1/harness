# Agent Loop 设计笔记：从 Claude Code 架构学习

> 基于 Claude Code 源码架构分析，对比当前 harness 项目的 agent loop 实现，
> 梳理值得借鉴的设计思路，作为后续迭代的参考。

---

## 1. 现状概览

### 当前代码结构

```
harness/
├── 01_agent_loop.py      # 入口：用户输入循环
├── core/
│   ├── config.py         # 配置（环境变量）
│   ├── llm.py            # LLM 客户端工厂
│   ├── tools.py          # 工具定义与执行
│   └── agent.py          # agent loop 核心
```

### 当前流程

```
用户输入 → agent_loop(messages)
  → LLM API 调用（非流式）
  → 如果有 tool_calls → while 循环执行工具 → 再次调用 LLM
  → 如果无 tool_calls → 打印回复，结束
  → 最终回复以流式输出
```

### Claude Code 的流程（对比）

```
用户输入 → QueryEngine.submitMessage()
  → 构建 systemPrompt + userContext
  → processUserInput()（斜杠命令、附件）
  → query() 核心循环（while true）:
      → 上下文预处理管线（7 步）
      → API 调用（流式，带错误恢复）
      → 工具执行（分区并行/串行）
      → 循环决策：继续 or 终止
  → Stop Hooks（记忆提取、提示建议等）
  → 后处理（消息规范化、usage 追踪）
```

**核心差距**：我们是"一个函数跑到底"，Claude Code 是"多层管线 + 状态机"。

---

## 2. 差异全景（6 大类 18 项）

### 一、循环安全保障

| # | Claude Code 设计 | 当前状态 | 说明 |
|---|---|---|---|
| 1 | **循环控制**：主循环靠 Token 预算弹性约束，子代理用 `maxTurns` 轮次限制 | while 无限循环，无任何退出保障 | CC 主循环无硬编码轮次上限，靠三套系统（maxTurns / TOKEN_BUDGET / taskBudget）分层控制 |
| 2 | **Token 阻塞检查** `calculateTokenWarningState()` | 无 | 发请求前先算 token 数，超限直接拒绝，不浪费 API 调用 |
| 3 | **API 错误恢复**（重试 / 模型降级） | 无 try/except | 一次网络抖动整个会话崩溃 |

### 二、上下文管理

| # | Claude Code 设计 | 当前状态 | 说明 |
|---|---|---|---|
| 4 | **上下文预处理管线** — 7 步依次执行 | 无 | 每次迭代前做：边界提取→预算裁剪→历史裁剪→微压缩→上下文折叠→自动压缩→token检查 |
| 5 | **工具结果预算** `applyToolResultBudget()` | 工具输出原样塞入 messages | 一个 `ls -R /` 可能输出 100k token，直接撑爆上下文 |
| 6 | **自动压缩** `autoCompactIfNeeded()` | 无 | 超阈值时自动压缩历史消息，长对话的命脉 |
| 7 | **历史裁剪** `snipCompactIfNeeded()` | 无 | 丢弃过于陈旧的消息，给新消息腾空间 |

### 三、用户交互与控制

| # | Claude Code 设计 | 当前状态 | 说明 |
|---|---|---|---|
| 8 | **中断机制** `AbortController` | 无 | API 调用和 shell 执行都无法取消 |
| 9 | **权限系统** `canUseTool()` | 只有硬编码黑名单 | 每个工具调用前都有权限检查，支持允许/拒绝/本次允许 |
| 10 | **工具使用摘要** `emitToolUseSummaries` | 执行后只打印原始输出 | 没有对工具执行结果的结构化摘要展示 |

### 四、架构模式

| # | Claude Code 设计 | 当前状态 | 说明 |
|---|---|---|---|
| 11 | **QueryEngine 会话对象** | `history` 列表裸传 | 用对象管理 messages、usage、abort、fileCache、discoveredSkills 等 |
| 12 | **每次迭代 State 对象** | 直接修改外部 `messages` | 每轮创建新 State（messages, turnCount, autoCompactTracking...）|
| 13 | **依赖注入 QueryDeps** | 直接 import | 把 callModel、compact 等作为参数注入，方便 mock 测试 |
| 14 | **不可变配置快照 QueryConfig** | 散落在各模块的全局变量 | 运行时配置一次性快照，不会被中途修改 |

### 五、工具执行策略

| # | Claude Code 设计 | 当前状态 | 说明 |
|---|---|---|---|
| 15 | **工具分区执行** — 只读并行、写入串行 | 只有 bash 一个工具 | 加新工具时立刻需要 |
| 16 | **流式工具执行** `StreamingToolExecutor` | 等 API 全部返回后才执行 | LLM 还在输出 tool_use 时就开始并行执行已收到的工具调用 |

### 六、可观测性与可扩展性

| # | Claude Code 设计 | 当前状态 | 说明 |
|---|---|---|---|
| 17 | **Token 用量追踪** `totalUsage` | 无 | 不知道每次调用花了多少钱、多少 token |
| 18 | **Stop Hooks / 生命周期钩子** | 无 | 循环结束后触发记忆提取、提示建议、状态清理等 |

---

## 3. 详细设计文档

每项改进的详细分析和实现方案见独立文档：

| 编号 | 主题 | 文档 |
|------|------|------|
| 01 | 循环控制：迭代上限与 Token 预算 | [agent_loop/01_max_turns.md](agent_loop/01_max_turns.md) |
| 02 | Token 阻塞检查 | _(待写)_ |
| 03 | API 错误恢复 | [agent_loop/03_api_error_recovery.md](agent_loop/03_api_error_recovery.md) |
| 04 | 上下文预处理管线 | _(待写)_ |
| 05 | 工具结果截断 | [agent_loop/05_tool_output_budget.md](agent_loop/05_tool_output_budget.md) |
| 06 | 自动压缩 | _(待写)_ |
| 07 | 历史裁剪 | _(待写)_ |
| 08 | 中断机制 | _(待写)_ |
| 09 | 权限系统 | _(待写)_ |
| 10 | 工具使用摘要 | _(待写)_ |
| 11 | QueryEngine 会话对象 | _(待写)_ |
| 12 | 迭代 State 对象 | _(待写)_ |
| 13 | 依赖注入 | _(待写)_ |
| 14 | 配置快照 | _(待写)_ |
| 15 | 工具分区执行 | _(待写)_ |
| 16 | 流式工具执行 | _(待写)_ |
| 17 | 状态显示与 Token 追踪 | [agent_loop/17_status_display.md](agent_loop/17_status_display.md) |
| 18 | 生命周期钩子 | _(待写)_ |

---

## 4. 后续迭代路线图

按"投入产出比"排序的实施建议：

### Phase 1：安全网 + 基础体验

- [x] [01 - 循环控制：迭代上限与 Token 预算](agent_loop/01_max_turns.md)
- [x] [03 - API 错误恢复](agent_loop/03_api_error_recovery.md)
- [x] [05 - 工具结果截断](agent_loop/05_tool_output_budget.md)
- [x] [17 - 状态显示与 Token 追踪](agent_loop/17_status_display.md)
- [ ] Token 阻塞检查（发请求前估算）

### Phase 2：上下文管理

- [ ] 消息数量上限（简单版上下文管理）
- [ ] 基础自动压缩

### Phase 3：架构重构

- [ ] 引入 Session 类替代裸 `messages` 列表
- [ ] 每次迭代 State 对象
- [ ] 依赖注入（方便测试）
- [ ] 配置快照

### Phase 4：用户体验

- [ ] 中断机制（AbortController）
- [ ] 权限系统升级
- [ ] 生命周期钩子

### Phase 5：高级特性（远期）

- [ ] 工具分区执行（只读并行 / 写入串行）
- [ ] 流式工具执行
- [ ] 多工具支持

---

## 参考资料

- Claude Code Agent Loop 架构分析：`/Users/kino/works/opensource/Claude-Code-doc/docs/03-agent-loop.md`
- smolagents 框架：`max_steps` 参数，默认值 20
- Claude Code 源码关键文件：
  - `src/QueryEngine.ts` — 会话管理
  - `src/query.ts` — 核心循环
  - `src/query/config.ts` — 不可变配置
  - `src/query/deps.ts` — 依赖注入