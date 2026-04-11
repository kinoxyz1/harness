# Harness 工具系统演进记录

> 本文档是工具系统和系统提示词迭代的完整记录。旧的分散文档（thinking-about-tools.md、v2、
> tool-system-tasks.md 等）已被本文档吸收，可以安全删除。

---

## 一、起点：一个最简 agent loop

最初的 harness 只有两个文件：`agent.py` 和 `tools.py`。工具系统极其简陋——
`tools.py` 里一个 `run_bash` 函数，agent loop 用 if/elif 硬编码路由：

```python
if tool_name == "run_bash":
    result = run_bash(args)
elif tool_name == "read_file":
    result = read_file(args)
```

这个阶段建立了 agent loop 的基本骨架（LLM 调用 → 工具执行 → 回写 → 循环），
但添加新工具必须改循环体，工具返回裸字符串没有统一格式，也没有只读/写的区分。

---

## 二、Phase 1：注册表 + 结构化返回

### 2.1 三个稳定点

工具系统的设计围绕三个接口边界，每一环的消费方不同：

```
Schema（给模型看）→ Dispatch Map（给框架路由）→ ToolResult（给 loop 消费）
```

- **Schema**：JSON Schema 函数签名。模型据此决定调不调、怎么调。工具最懂自己，
  所以 description 应该由工具自己写，跟着工具走。
- **Dispatch Map**：工具名 → handler 的映射表。让循环和工具解耦——
  加工具不需要改循环体（if/elif），注册表自动发现。
- **ToolResult**：统一的 `dataclass(output, success, error)`。
  loop 只消费这个格式，不关心是哪个工具、内部怎么执行。

### 2.2 一个工具 = 一个 Python 文件

每个工具是 `core/tools/` 下的一个独立模块，暴露三个属性：

| 属性 | 谁消费 | 职责 |
|------|--------|------|
| `SCHEMA` | 大模型 | JSON Schema 函数签名 + 详细使用说明 |
| `READONLY` / `ANNOTATIONS` | 框架 | 安全标记（是否只读、是否破坏性等） |
| `handle(args, context)` | 框架 | 执行逻辑，返回 `ToolResult` |

`ToolRegistry` 通过 `auto_discover()` 扫描目录自动注册。
核心保证：**加工具不需要改循环，不需要改 agent.py。**

### 2.3 工具指南的架构决策

一个容易犯错的决策：工具使用指南应该放在哪里？

**错误做法**：拼接到 system prompt 中。
问题：system prompt 中硬编码工具名 → 加新工具必须改 system prompt →
违反"加工具不需要改其他文件"的原则。

**正确做法**（学自 CC）：工具的 SCHEMA `description` 字段包含详细使用说明，
`get_system_context()` 只返回通用行为准则，不引用具体工具名。

CC 的源码 `src/utils/api.ts` 证实了这一点：
`tool.prompt()` 返回值成为 API 请求中工具的 `description` 字段，
不是拼接到 system prompt 的文本。

---

## 三、Phase 2：认知层 + 执行运行时

这是最大的设计跃迁。v1 从行业协议层面分析工具未来方向，
但深入 CC 源码后发现多处假设需要修正。

### 3.1 v1 到 v2 的认知修正

| v1 假设 | CC 实际做法 | 修正 |
|---------|-----------|------|
| 工具历史需要单独维护（ToolResultRecord） | 历史就是 messages 本身 | **不需要 ToolResultRecord** |
| file_cache 是简单 dict（path → content） | readFileState 存 {content, timestamp, offset, limit}，做 staleness 检测 | **file_state 是认知，不是缓存** |
| 写工具后需要清空 cache | 写工具后更新 file_state（新内容 + 新 timestamp） | **增量更新，不是暴力清空** |
| 工具上下文可以分期实现 | identity + file_state + messages 是不可分割的整体 | **一次性设计** |
| dataclass 加默认值够用 | class + property 精确控制读写权限 | **用 class，不用 dataclass** |
| messages 传给工具需要包装保护 | CC 直接传引用，信任工具不乱改 | **小项目直接传引用** |
| 工具使用指南应拼接到 system prompt 中 | CC 的 prompt() 作为 schema 的 description 字段 | **指南在 schema，不在 prompt** |

核心教训：**看协议文档和看实际代码得到的结论完全不同。**
协议定义"应该怎样"，代码展示"实际怎么做"。

### 3.2 FileState：认知，不是缓存

这是从 CC 学到的**最精妙的设计**。

CC 不叫它"cache"，叫它 `readFileState`——工具对文件系统的**认知状态**。
缓存是性能优化（可以失效、可以重建），认知是正确性保证
（edit_file 必须基于对文件内容的认知才能安全编辑）。

```python
@dataclass
class FileState:
    content: str                # 文件内容
    timestamp: float            # os.path.getmtime() 读取时的修改时间
    offset: int | None = None   # 读取偏移（None = 全文）
    limit: int | None = None    # 读取行数限制（None = 全文）
```

四种消费场景：

| 场景 | 工具 | 行为 |
|------|------|------|
| 读后认知 | read_file | 成功读取后 `set_file_state()` |
| 编辑前强制读 | edit_file | 检查 `get_file_state()` 是否存在，不存在则拒绝 |
| staleness 检测 | edit_file | 比较 timestamp 与文件当前 mtime，不一致则报错 |
| 写后更新 | write_file / edit_file | 写入后 `update_file_state()` |

**一个机制解决四个问题**：
1. edit_file 不再盲目编辑——必须先 read_file
2. read_file 不会重复传输相同内容
3. 外部文件修改能被检测到
4. 写操作后认知自动更新

### 3.3 ToolUseContext：五层设计

```python
class ToolUseContext:
    """工具执行上下文，一个对象贯穿整个循环。"""

    # 环境层：构造时设置，工具只读
    _working_dir: str
    _max_turns: int

    # 身份层：每次 tool call 更新，工具只读
    _tool_name: str
    _tool_call_id: str
    _turn_count: int

    # 文件认知层：工具可读写，框架控制一致性
    _file_state: dict[str, FileState]

    # 对话层：只读引用
    _messages: list[dict] | None

    # 控制层：外部信号，工具只能查询
    _cancelled: bool
```

**核心设计原则**：
- **一个对象贯穿循环**：Context 不重建、不拷贝，只更新。
  identity 字段在每次调用前被 agent loop 覆写，file_state 持续累积。
- **历史不需要单独维护**：messages 就是历史，不需要 ToolResultRecord。
- **用 class 不用 dataclass**：property 精确控制读写权限。

### 3.4 ToolExecutorRuntime：三层架构

从两层到三层的跃迁：

```
# 之前（两层）：agent loop 直接 for 循环执行工具
for tool_call in tool_calls:
    result = registry.execute(name, args, context)

# 之后（三层）：agent loop 委托给 Runtime
runtime = ToolExecutorRuntime(registry, context)
results = runtime.execute_batch(tool_calls)
```

三层各有独立职责：

| 层 | 职责 | 核心问题 |
|---|---|---|
| **Agent Loop** | LLM 决策 + 循环控制 + messages 管理 | "下一步做什么" |
| **ToolExecutorRuntime** | 分批 + 并发 + 顺序保证 + 异常保护 | "怎么安全高效地执行这一批" |
| **ToolUseContext** | 文件认知 + 身份 + 环境信息 | "执行时工具需要知道什么" |

### 3.5 并发编排的五个核心问题

1. **多工具共存**：READONLY 工具可并行，写工具独占串行。
   `_partition()` 连续只读归一个并行 batch，写操作各自独占一个串行 batch。

2. **并发执行**：只读 batch 用 `ThreadPoolExecutor` 并行，写 batch 串行。

3. **回写顺序**：并行执行完成顺序不确定，但结果按原始 idx 排序返回。
   LLM 期望 tool_result 按 tool_call 顺序。

4. **context 并发安全**：并行 batch 全是只读工具（不写 file_state），
   写工具独占串行。天然无冲突。

5. **错误传播**：宽松策略——错误作为 ToolResult 返回，不取消其他工具。
   LLM 拥有最完整的上下文来判断下一步。

### 3.6 消息规范化层

引入 `normalize_messages`（`core/protocol.py`）解决三类问题：

1. **字段清洗**：`reasoning_content`、`refusal` 等 API 不接受的字段需剥离，
   但思考模型（kimi-k2-thinking）又要求保留 `reasoning_content`。
   通过 `enable_thinking` 参数控制。

2. **tool_call 配对**：确保每个 tool_call 都有匹配的 tool_result。
   MAX_TURNS 截断等场景可能产生孤立的 tool_call。

3. **角色交替**：OpenAI API 要求 user/assistant 严格交替。
   连续同角色消息需要合并。

---

## 四、Phase 3：系统提示词上下文系统

### 4.1 三层组装

```
框架层（_FRAMEWORK_PROMPT）  ← 核心身份和工作原则，始终存在，不可覆盖
用户定制层（.harness/context/*.md）  ← 项目特定的身份、风格、规则
环境信息层（get_user_context）  ← 动态环境信息（OS、cwd、date），每次请求生成
```

### 4.2 设计原则

- 框架层是**不可覆盖的底线**（先理解再行动、先读后改、错误先分析再处理）
- 用户层通过文件定制（identity.md、style.md、rules.md），支持多套人设切换
- 环境信息以 `<environment>` 标签注入在第一条 user 消息前
- 用 HTML 注释标记避免重复注入

### 4.3 实践教训

初始的 identity.md（145 行）和 style.md（170 行）过于冗长，
导致模型过度表演（简单"你好"也触发工具调用）。
精简到各 ~15 行后，模型行为更自然。

核心发现：**角色设定越精炼越好。模型不需要 30 个示例来理解"像朋友聊天"。**

---

## 五、Phase 3.5：多模型适配

最初只考虑了 deepseek-v3.2，切换到 kimi-k2-thinking 后暴露了多个问题。

### 5.1 思考模型的三个坑

**坑 1：空内容退出**
思考模型执行工具后可能返回 `finish_reason=stop` 但 `content` 为空
（所有 token 花在了内部推理上）。
旧代码的 `_run_tool_loop` 在 `is_tool_call=False` 时立即退出。

修复：循环内增加空内容检测，重试最多 3 次，prompt 模型继续完成任务。

**坑 2：reasoning_content 必填**
kimi API 在思考模式下要求带 `tool_calls` 的 assistant 消息必须包含
`reasoning_content` 字段。`normalize_messages` 之前会剥离此字段。

修复：`_clean_message` 根据 `enable_thinking` 参数决定是否保留 `reasoning_content`，
带 tool_calls 但缺失时补空字符串。

**坑 3：思考 token 吃掉输出预算**
`enable_thinking: True` 时，reasoning 和 content 共享 `max_tokens` 预算。
模型可能花 7000 token 思考，只剩 1000 token 给工具参数，导致 JSON 截断。

修复：`ENABLE_THINKING` 改为可配置，`MAX_TOKENS` 需要相应增大。

### 5.2 配置矩阵

| 配置项 | 作用 | 默认值 |
|--------|------|--------|
| `LLM_MODEL` | 模型选择 | deepseek-v3.2 |
| `LLM_MAX_TOKENS` | 输出 token 上限 | 8192 |
| `LLM_ENABLE_THINKING` | API 是否开启思考模式 | false |
| `LLM_SHOW_THINKING` | 终端是否显示思考面板 | true |
| `AGENT_MAX_TURNS` | 工具循环安全上限 | 300 |

---

## 六、Phase 4：韧性增强

### 6.1 LLMResponse 结构化封装

所有 API 响应通过 `LLMResponse` 类访问，提供语义化属性：
- `has_content`：是否有可见文字（忽略纯空白）
- `is_tool_call`：是否请求工具调用（含防御性检查）
- `is_truncated`：是否被 token 限制截断

### 6.2 write_file 分块写入

大文件写入时 JSON 参数可能超过 `max_tokens` 被截断。
新增 `mode='append'` 参数支持分块写入：
先 `write` 创建文件，再多次 `append` 追加内容，每块控制在 100 行以内。

### 6.3 截断错误恢复

JSON 解析失败时根据工具类型给出针对性恢复建议：
- `write_file` → 引导使用 append 分块写入
- 其他工具 → 提示减少参数内容

---

## 七、与 Claude Code 的差距：未来探索方向

### 7.1 已实现的核心思想

- [x] 注册表 + 自动发现的工具系统
- [x] 结构化 ToolResult 返回
- [x] 只读并行 / 写独占串行的调度
- [x] FileState 文件认知（read-before-write + staleness 检测）
- [x] 消息规范化（字段清洗、配对、角色交替）
- [x] 系统提示词三层组装
- [x] 工具循环韧性（空内容重试、截断恢复、JSON 解析处理）
- [x] 多模型适配（思考/非思考模型）

### 7.2 值得探索的方向

**1. edit_file 的 staleness 检测**
FileState 有 timestamp 字段，edit_file 应该比较它与文件当前 mtime，
不一致时拒绝编辑并提示重新读取。
CC 的 `FILE_UNEXPECTEDLY_MODIFIED_ERROR` 就是这个机制。
当前代码有这个检查，但可以做得更完善（比如提示具体哪个字段变了）。

**2. 输出预算控制（CC 的 emitToolUseSummaries）**
CC 对工具输出做智能摘要——大输出不是简单截断，而是提取关键信息。
我们目前是硬截断（MAX_OUTPUT_CHARS），丢失了上下文。
这是提升 token 效率最直接的手段。

**3. 工具使用指南的动态注入**
CC 的 `tool.prompt()` 可以根据上下文动态生成不同的使用说明。
我们的 PROMPT 是静态的，每次全量发送。
动态注入更省 token，且可以根据场景提供更有针对性的建议。

**4. 工具错误分类与恢复**
CC 将错误分为可重试（timeout、rate limit）和不可重试（参数错误、权限拒绝），
对不同类型采取不同策略。我们目前是统一的 `ToolResult(success=False)`。

**5. 流式响应**
CC 支持流式输出，用户实时看到模型回复。
我们有 stream 参数但返回原始 Stream 对象，流式模式下无法使用工具循环。

**6. 权限与安全**
CC 有完整的权限系统：敏感操作需要用户确认，工具声明 destructive 标记。
我们有 ANNOTATIONS 字段但未接入实际的安全检查。

**7. contextModifier 模式**
CC 允许工具执行完成后修改后续工具看到的 context，
实现工具间状态传播而不经过 LLM 中转。
这是一个高级特性，在复杂编排场景下很有用。

**8. compaction 时的 file_state 处理**
CC 在上下文压缩时清空 readFileState，但保留最常用的 5 个文件作为附件恢复。
我们的 file_state 目前跨请求不保留（每次 agent_loop 调用新建 context）。

---

## 八、从 CC 学到的六条设计原则

1. **认知 > 缓存**：file_state 不是性能优化手段，而是正确性保证。
   edit_file 要求"你必须先 read_file"不是建议，是强制。

2. **一个对象贯穿循环**：Context 不重建、不拷贝，只更新。
   所有工具看到的是同一个 context 实例。

3. **默认保守，按需开放**：CC 的 buildTool() 默认不可并发、不可只读。
   工具必须显式声明自己安全才能享受优化。

4. **历史就是消息**：messages 是唯一的真相来源，不需要独立的历史索引。

5. **工具指南在 schema description**：不在 system prompt。
   加工具不需要改其他文件。

6. **看代码，不看文档**：协议定义"应该怎样"，代码展示"实际怎么做"。
   很多设计假设在看到实际实现后被推翻。

---

## 九、架构总览

```
┌─────────────────────────────────────────────────┐
│ agent_loop（入口）                               │
│  ├─ _inject_system_context（框架+用户定制）      │
│  ├─ _inject_user_context（环境信息）             │
│  ├─ _call_llm → LLMResponse（结构化响应封装）    │
│  ├─ _run_tool_loop（工具循环 + 空内容重试）      │
│  │   └─ _execute_tool_turn                       │
│  │       ├─ ToolExecutorRuntime（分批调度）      │
│  │       │   ├─ 只读工具并行（ThreadPool）       │
│  │       │   └─ 写工具串行（独占）               │
│  │       └─ ToolUseContext（五层上下文）          │
│  └─ _ensure_final_response（兜底重试）           │
├─────────────────────────────────────────────────┤
│ normalize_messages（协议层）                      │
│  ├─ _clean_message（字段清洗+reasoning处理）     │
│  ├─ _pair_tool_results（tool_call配对）          │
│  └─ _merge_consecutive_roles（角色交替）         │
├─────────────────────────────────────────────────┤
│ ToolRegistry（注册表，自动发现）                  │
│  └─ bash / read_file / edit_file / write_file / find │
└─────────────────────────────────────────────────┘
```

代码量：core/ 共 ~1900 行。

---

## 附录：原始文档索引

| 文档 | 时期 | 状态 |
|------|------|------|
| `docs/agent_loop/05_tool_output_budget.md` | Phase 0 | 已被本文档吸收 |
| `docs/agent_loop/tool_system_redesign.md` | Phase 1 | 已被本文档吸收 |
| `docs/thinking-about-tools.md` | Phase 2 (v1) | 已被 v2 和本文档吸收 |
| `docs/thinking-about-tools-v2.md` | Phase 2 (v2) | 最深入的设计文档，可保留参考 |
| `docs/system-prompt-context-design.md` | Phase 3 | 已被本文档吸收 |
| `docs/tool-system-tasks.md` | Phase 2 | 任务拆解已完成，可删除 |
