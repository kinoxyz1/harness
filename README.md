# Harness

一个用于学习 Agent 运行时内部机制的终端框架。

代码本身就是教材——每个模块职责收窄到一件事，文件顶部都有模块级注释说明"你在数据流中的位置"。目标是读完这个仓库后，你能回答：

- 一个 Agent 的 think-act 循环到底在循环什么？退出条件有哪些？
- 模型看到的"输入"是怎么从内部状态组装出来的？system 和 messages 为什么分开？
- 工具调用的结果怎么回到模型？为什么不让工具直接改状态？
- 上下文窗口不够时怎么办？transcript 截取和 thinking 清理的策略是什么？

## 核心设计

主流 Agent 把"完整对话历史"直接喂给模型。Harness 的做法不同——维护显式状态，每轮重组视图：

1. **显式状态**（`SessionState` + `RunState`）保存运行时真相，不是散落在对话历史里的文本
2. **视图组装器**（`MessageViewBuilder`）每轮从状态重建模型输入，从状态而非历史拼装
3. **Reducer 是唯一状态写入口**——工具只返回结构化 updates，不直接改 QueryLoop 状态

这三个约束把系统从"基于历史消息拼运气"变成"基于状态重组视图"的 Agent runtime。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入 API Key

python 01_agent_loop.py
```

```text
Agent Loop 已启动。输入 exit 或 quit 退出。

>> /skills list
>> 阅读 README.md 并总结
```

## 数据流

```text
用户输入
  → SessionEngine.submit_user_message()
    → QueryLoop.run()
      ┌──────────────────────────────────────────────────────────┐
      │ while True:                                               │
      │   maintenance（文件缓存失效检查）                          │
      │   policy_runner.before_model_call()                       │
      │   PromptAssembler.build_stable() + build_runtime_context()│
      │   ContextGovernor.assess()（上下文预算管理）               │
      │   MessageViewBuilder.build() → ModelInputView            │
      │   ModelGateway.call_once() / stream_once()                │
      │   ── 分支 ────────────────────────────────────────────── │
      │   tool_calls → ToolExecutorRuntime.execute_batch()        │
      │               → reducers 写入状态 → 回到循环顶部          │
      │   最终文本   → return QueryResult                         │
      │   截断响应   → RecoveryManager 注入 "继续" → 回到循环顶部 │
      │   空响应     → RecoveryManager 注入 "请回答" → 重试       │
      └──────────────────────────────────────────────────────────┘
    ← QueryResult
```

退出循环：正常完成 | `max_turns` 强制收尾 | API 错误 | 用户中止（Esc/Ctrl-C） | recovery 失败。

## 项目结构

```text
harness/
├── 01_agent_loop.py          # 入口：REPL + 依赖装配（TerminalLineEditor + RunAbortMonitor）
├── core/
│   ├── llm/                  # API 客户端层
│   │   ├── anthropic_client.py   # Anthropic SDK 封装：thinking 模式、自适应回退、流式输出
│   │   ├── client.py             # ModelGateway：批量/流式调用、错误分类、取消检查
│   │   ├── factory.py            # 创建 Anthropic SDK 实例，处理非 Anthropic 兼容服务
│   │   ├── protocol.py           # 内部消息 → Anthropic API 格式转换、tool_use/tool_result 配对
│   │   └── response.py           # ModelResponse 统一内部响应模型
│   ├── policy/               # 策略框架：before_model_call / should_stop 挂钩
│   │   ├── base.py               # RunPolicy Protocol + PolicyRunner
│   │   ├── max_turns.py          # 轮次上限强制收尾
│   │   ├── task_planning.py      # 复杂任务强制先 plan 再 execute
│   │   ├── todo_tracking.py      # 连续无计划更新时 nudging
│   │   ├── skill_relevance.py    # 检测未加载的相关 skill 并提醒
│   │   └── skill_usage_nudge.py  # 长时间未使用 skill 时 nudging
│   ├── prompt/               # System prompt 三层组装
│   │   ├── assembler.py          # stable（框架+skill 目录）+ runtime（环境+任务+文件）+ overlay
│   │   ├── cache.py              # 稳定 prompt 缓存
│   │   ├── context.py            # PromptContext 数据类
│   │   └── system_context.py     # 框架 prompt + .harness/context/ 用户定制 + 环境信息
│   ├── query/                # 查询循环、状态、reducers、recovery
│   │   ├── loop.py               # QueryLoop.run()：think-act 主循环（核心）
│   │   ├── state.py              # RunState：单次查询的运行时可变状态
│   │   ├── reducers.py           # apply_session_update() / apply_run_update()：唯一状态写入口
│   │   ├── result.py             # QueryResult + StopReason 枚举
│   │   └── recovery.py           # RecoveryManager：截断续写、空响应重试
│   ├── session/              # 会话管理、上下文治理、视图组装
│   │   ├── engine.py             # SessionEngine：用户输入和 QueryLoop 之间的协调者
│   │   ├── state.py              # SessionState：跨查询持久状态 + TodoState
│   │   ├── store.py              # SessionStore：对话消息的唯一写接口
│   │   ├── view_builder.py       # MessageViewBuilder：状态 → ModelInputView
│   │   ├── query_context.py      # PreparedQueryContext + ContextBlock
│   │   ├── governor.py           # ContextGovernor：上下文预算级联策略
│   │   ├── compact_service.py    # LLM 摘要压缩 + 运行时状态恢复
│   │   ├── offloader.py          # 大工具结果落盘到 .harness/sessions/
│   │   ├── microcompact.py       # 按时间替换旧工具结果
│   │   ├── token_budget.py       # token 估算 + 水位线计算
│   │   ├── pairing_repair.py     # tool_use/tool_result 配对修复
│   │   ├── transcript_rewriter.py # 压缩边界 + 摘要消息构建
│   │   ├── content_replacement.py # 工具结果替换追踪
│   │   ├── preview_strip.py      # 压缩时剥离 persisted-output 块
│   │   ├── read_working_set.py   # 压缩后恢复近期文件读取状态
│   │   ├── subagent.py           # SubagentRuntime：隔离子代理（Explore/Plan/General）
│   │   └── commands.py           # /skills 命令路由（list/show/use/off/reload）
│   ├── skills/               # Skill 系统
│   │   ├── registry.py           # SkillRegistry：两阶段 discover + load
│   │   ├── models.py             # SkillMeta / SkillContent / InvokedSkillRecord / SkillEvent
│   │   └── runtime.py            # Skill 运行时内容渲染 + inline budget
│   ├── tasks/                # 任务规划与执行
│   │   ├── models.py             # TaskState / TaskRecord / TaskPacket / TaskRunResult
│   │   ├── planner_runtime.py    # build_task_state()：验证 + 规范化任务列表
│   │   ├── projection.py         # TaskState → TodoItem 映射
│   │   └── dispatcher.py         # compile_task_packet() + normalize_subagent_result()
│   ├── tools/                # 工具系统
│   │   ├── __init__.py           # ToolRegistry + auto_discover() 自动发现 builtin 工具
│   │   ├── context.py            # ToolUseContext + SessionUpdate/RunUpdate 数据结构
│   │   ├── runtime.py            # ToolExecutorRuntime：分批执行（只读并行 / 写串行）
│   │   └── builtin/              # 内置工具实现
│   │       ├── bash.py           # Bash 命令执行（blocked/confirm/timeout/cancel）
│   │       ├── read_file.py      # 文件读取（行号/offset/limit/二进制检测）
│   │       ├── write_file.py     # 文件写入（write/append 模式）
│   │       ├── edit_file.py      # 字符串替换编辑（read-before-write / mtime 检查）
│   │       ├── find.py           # Glob 文件搜索
│   │       ├── todo.py           # Todo 列表管理
│   │       ├── skill.py          # Skill 激活
│   │       ├── task_plan.py      # 任务规划
│   │       └── task_execute.py   # 任务执行（dispatch fresh_subagent）
│   ├── shared/               # 共享基础
│   │   ├── config.py             # 全局配置：从环境变量 / .env 加载
│   │   ├── protocol.py           # SupportsMessageDict Protocol
│   │   ├── types.py              # UsageDelta + MessageBatch
│   │   ├── env_loader.py         # load_project_env()：最小 .env 解析器
│   │   ├── interfaces.py         # LLMClient / ContextPlugin / Renderer Protocol
│   │   ├── run_options.py        # RunDisplayOptions 数据类
│   │   └── stream_events.py      # StreamEvent + StreamAccumulator（流式事件收集）
│   └── ui/                   # 终端渲染
│       └── renderer.py           # RichRenderer（流式 Live widget）+ QuietRenderer + render_markdown()
├── docs/
│   ├── features/             # 功能文档 + 学习路径
│   │   ├── 00-learning-path.md           # 推荐阅读顺序
│   │   ├── 01-agent-loop.md              # Agent 循环设计
│   │   ├── 02-tool-system.md             # 工具系统架构
│   │   ├── 03-tool-control-plane.md      # 工具控制平面
│   │   ├── 04-query-control-plane.md     # 查询控制平面
│   │   ├── 05-todo.md                    # Todo / 任务规划
│   │   ├── 06-skill.md                   # Skill 系统
│   │   ├── 07-subagent.md               # 子代理运行时
│   │   ├── 08-request-lifecycle-walkthrough.md  # 请求生命周期 walkthrough
│   │   ├── 09-state-assembled-runtime.md        # 状态即运行时真相
│   │   ├── 10-anthropic-protocol-boundary.md    # Anthropic 协议边界
│   │   ├── 11-context-management-architecture.md # 上下文管理架构
│   │   └── diagram/                      # 架构图（SVG）
│   └── superpowers/          # 设计文档和演进记录
├── tests/                    # 测试套件（50+ 测试文件）
├── projects/                 # 项目工作区
└── .harness/
    ├── context/              # identity.md, style.md, rules.md — 用户定制 AI 行为
    ├── skills/               # 用户自定义 skill（SKILL.md + references）
    └── sessions/             # 运行时会话数据（工具结果落盘等）
```

## 配置

所有配置通过环境变量或 `.env` 文件加载，详见 `core/shared/config.py`。

### 必须配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 空 | Anthropic 兼容 API Key |

### 模型配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ANTHROPIC_MODEL` | `kimi-k2.5` | 模型标识符 |
| `ANTHROPIC_BASE_URL` | `https://api.kimi.com/coding/` | Anthropic 兼容 API 端点 |
| `LLM_REQUEST_TIMEOUT` | `180` | 单次 HTTP 请求超时（秒） |
| `LLM_RETRY_ATTEMPTS` | `2` | 网关错误（502/503/504）最大重试次数 |
| `LLM_RETRY_BACKOFF_SECONDS` | `1.5` | 重试基础退避时间（秒） |
| `LLM_MAX_TOKENS` | `8192` | 单次输出 token 上限 |

### Thinking 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_THINKING_MODE` | `enabled` | `auto`（自适应）/ `enabled`（固定预算）/ `disabled`（关闭） |
| `LLM_THINKING_BUDGET` | `8192` | enabled 模式下的 thinking token 预算 |
| `LLM_SHOW_THINKING` | `true` | 是否在终端显示 thinking 内容 |
| `LLM_PERSIST_THINKING` | `true` | 是否将 thinking 持久化到对话历史 |
| `LLM_MAX_REASONING_CHARS` | `200000` | 持久化 thinking 最大字符数 |

### 运行时配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BASH_TIMEOUT` | `30` | bash 命令执行超时（秒） |
| `AGENT_MAX_TURNS` | `300` | 单次查询最大工具调用轮次 |
| `MEMORY_REVIEW_INTERVAL` | `1` | 每 N 个真实用户 turn 触发一次后台 memory review；`0` 表示关闭 |
| `MAX_OUTPUT_CHARS` | `100000` | 工具输出截断阈值（字符） |
| `CONTEXT_WINDOW_TOKENS` | `131072` | 上下文窗口大小（需与实际模型匹配） |
| `SKILL_LLM_MATCH` | `false` | 是否用 LLM 做 skill 语义匹配（增加一次 API 调用） |

### 终端渲染配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `UI_MARKDOWN_CODE_THEME` | `friendly` | 代码块 Pygments 主题 |
| `UI_MARKDOWN_INLINE_CODE_STYLE` | `bold cyan` | 行内代码 Rich 样式 |
| `UI_MARKDOWN_CODE_BLOCK_STYLE` | `cyan` | 代码块文本 Rich 样式 |

### Offloader 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TOOL_RESULT_PERSIST_THRESHOLD` | `2000` | 工具结果落盘阈值（字符） |
| `BASH_RESULT_PERSIST_THRESHOLD` | `1000` | bash 结果独立落盘阈值 |
| `TOOL_RESULTS_AGGREGATE_BUDGET` | `30000` | 工具结果聚合上限 |
| `TOOL_RESULT_PREVIEW_BYTES` | `10000` | 落盘后保留的预览字节数 |

### 流式渲染配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `STREAMING_ENABLED` | `true` | 流式输出全局开关 |
| `STREAMING_THINKING_ENABLED` | `true` | thinking 流式展示 |
| `STREAMING_RENDER_FLUSH_MS` | `80` | 流式渲染 flush 窗口（毫秒） |
| `SUBAGENT_STREAMING_MODE` | `replayed` | 子代理流式模式：`replayed` / `live` |
| `PERSIST_PARTIAL_STREAM_OUTPUT` | `false` | 是否持久化未完成的流式输出 |

## 长期记忆怎么调

Harness 的第一层长期记忆是文件后备存储：

- `.harness/memories/USER.md`：记录“这个用户是谁”，例如稳定偏好、沟通方式、长期习惯
- `.harness/memories/MEMORY.md`：记录“这个项目/环境有什么长期事实”，例如约定、坑、设计决策

默认情况下，后台 `memory review` 会比较积极。它会在回答完成后，周期性回看最近几轮对话，并在判断“值得长期记住”时写入上面两个文件。

### 最重要的参数

```bash
# 每 5 个真实用户 turn 才做一次后台记忆回顾
MEMORY_REVIEW_INTERVAL=5
```

如果你只是想先避免 `USER.md` 很快写满，最直接的做法就是把它从默认的 `1` 提高到 `5` 或 `10`。

这里的“每 5 个真实用户 turn”指的是：

- 第 5、10、15... 个用户问题结束后触发一次 review
- 触发时会回看“最近一个对话窗口”
- 不是只总结第 5 次那一条消息

当前实现中，后台 review 默认会检查最近 12 条 message，再决定是否写入 `USER.md` 或 `MEMORY.md`。

推荐起点：

- `MEMORY_REVIEW_INTERVAL=0`：完全关闭自动长期记忆，只保留手动 `memory` 工具
- `MEMORY_REVIEW_INTERVAL=5`：日常开发比较稳妥的起点
- `MEMORY_REVIEW_INTERVAL=10`：更保守，适合你想先观察写入质量的时候

### 推荐 .env 配置

如果你希望长期记忆更克制，建议在 `.env` 里先这样配：

```bash
# ---- 长期记忆建议值 ----
MEMORY_REVIEW_INTERVAL=5

# ---- 工具结果落盘建议值 ----
TOOL_RESULT_PERSIST_THRESHOLD=4000
BASH_RESULT_PERSIST_THRESHOLD=2000
TOOL_RESULTS_AGGREGATE_BUDGET=30000
```

### 什么时候应该写进 USER.md

适合写入 `USER.md` 的内容：

- 明确说出的稳定偏好：喜欢 Python、喜欢“皇上/老奴”式回复
- 长期沟通习惯：偏简洁、偏中文、偏直接
- 长期身份信息：职业背景、持续使用的工作流

不适合写入 `USER.md` 的内容：

- 一次性意图：今天想夜爬梧桐山
- 临时状态：今天查深圳天气、今天心情如何
- 弱推断：查了深圳天气，不等于用户常住深圳
- 单轮细节：某次工具输出、某次临时计划

### 教学上的理解

如果你把 `MEMORY_REVIEW_INTERVAL` 设得很小，例如 `1`，系统会更像“每轮都尝试总结用户画像”。这样虽然容易演示长期记忆机制，但也更容易出现两个问题：

- `USER.md` 很快堆满
- 临时信息被误当成长期偏好

所以更适合教学和日常使用的思路是：

1. 先把 `MEMORY_REVIEW_INTERVAL` 调大
2. 先观察 `USER.md` / `MEMORY.md` 的写入质量
3. 如果写入仍然太多，再考虑继续收紧 review 规则

### 调参后的验证方法

你可以直接观察这两个文件：

```bash
sed -n '1,200p' .harness/memories/USER.md
sed -n '1,200p' .harness/memories/MEMORY.md
```

一个健康的状态通常是：

- `USER.md` 条目数量少，但每条都稳定、长期有效
- `MEMORY.md` 可以略多一些，但仍然应该偏“项目知识”而不是聊天碎片

## 怎么读这个仓库

建议先分两层来读：

- **第一层：核心代码路径** — 直接对照代码，理解一条请求从入口到退出
- **第二层：伴随文档路径** — 补齐"从 0 到 1 做一个 Agent"时最容易缺的桥梁知识

如果你是第一次接触这个仓库，建议先读 [docs/features/00-learning-path.md](docs/features/00-learning-path.md)。

### 第一层：核心代码路径

建议按以下顺序，每个文件都在 200 行以内：

**第一组：主路径** — 理解一条用户输入从头到尾走了什么

1. [`01_agent_loop.py`](01_agent_loop.py) — REPL + 依赖装配
2. [`core/session/engine.py`](core/session/engine.py) — 会话协调者
3. [`core/query/loop.py`](core/query/loop.py) — think-act 主循环（核心）

**第二组：模型输入** — 理解模型"看到"的到底是什么

4. [`core/session/view_builder.py`](core/session/view_builder.py) — 状态 → ModelInputView
5. [`core/prompt/assembler.py`](core/prompt/assembler.py) — system prompt 三层组装

**第三组：协议与工具** — 理解数据怎么进出模型

6. [`core/llm/protocol.py`](core/llm/protocol.py) — 内部格式 → Anthropic 格式转换
7. [`core/query/reducers.py`](core/query/reducers.py) — 唯一状态写入口
8. [`core/tools/runtime.py`](core/tools/runtime.py) — 工具分批执行（并行/串行）

### 第二层：伴随文档路径

当你已经能沿着代码路径走一遍以后，建议继续读这 4 篇：

1. [docs/features/08-request-lifecycle-walkthrough.md](docs/features/08-request-lifecycle-walkthrough.md) — 一次真实请求如何跑完整个运行时
2. [docs/features/09-state-assembled-runtime.md](docs/features/09-state-assembled-runtime.md) — 为什么状态才是运行时真相
3. [docs/features/10-anthropic-protocol-boundary.md](docs/features/10-anthropic-protocol-boundary.md) — 内部消息结构和 Anthropic 协议边界
4. [docs/features/11-context-management-architecture.md](docs/features/11-context-management-architecture.md) — 上下文管理架构

`docs/features` 现在既包含按组件拆开的功能说明，也包含这组面向入门和扩展的伴随文档。完整阅读顺序见 [docs/features/00-learning-path.md](docs/features/00-learning-path.md)。

## License

MIT
