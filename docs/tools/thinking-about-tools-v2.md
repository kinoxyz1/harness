# 关于 Tool 的思考 v2：从 Harness 到 Claude Code 的工具设计实录

> v1 从行业协议层面分析工具的未来方向。v2 基于 Claude Code 源码文档的实际实现，修正了 v1 中的多处假设，以"CC 到底怎么做的"为核心线索重新组织。

---

## 一、v1 到 v2 的认知修正

v1 写了七个"未来工具应具备的能力"，并以 Claude Code 作为"工业级参考"。但当深入 CC 的实际源码后，发现多处假设需要修正：

| v1 假设 | CC 实际做法 | 修正 |
|---------|-----------|------|
| 工具历史需要单独维护（ToolResultRecord） | 历史就是 messages 本身，无需独立结构 | **不需要 ToolResultRecord** |
| file_cache 是简单 dict（path → content） | readFileState 存 {content, timestamp, offset, limit}，做去重/staleness 检测 | **file_state 是"工具对文件系统的认知"，不是缓存** |
| 写工具后需要清空 cache | 写工具后更新 file_state（新内容 + 新 timestamp），不做全量清除 | **增量更新，不是暴力清空** |
| 工具上下文可以分期实现（Phase 1/2/3） | identity + file_state + messages 是一个不可分割的整体 | **一次性设计，不分期** |
| dataclass 加默认值够用 | class + property 精确控制读写权限 | **用 class，不用 dataclass** |
| messages 传给工具需要包装保护 | CC 直接传引用，信任工具不乱改 | **小项目直接传引用** |
| 工具使用指南应拼接到 system prompt 中 | CC 的 `tool.prompt()` 返回值作为工具 schema 的 `description` 字段，不进 system prompt | **工具指南在 schema description，不在 system prompt** |

这些修正的核心教训：**看协议文档和看实际代码得到的结论完全不同**。协议定义"应该怎样"，代码展示"实际怎么做"。

---

## 二、Harness 的工具系统现状

### 2.1 当前设计

Harness 是一个基于 OpenAI 兼容协议的 Agent Loop 框架。工具系统围绕三个抽象构建：

```
Schema（给模型看）→ Dispatch Map（给框架路由）→ ToolResult（给 loop 消费）
```

一个工具是一个 Python 文件（如 `core/tools/bash.py`），暴露三个属性：

| 属性 | 谁消费 | 职责 |
|------|--------|------|
| `SCHEMA` | 大模型 | JSON Schema 函数签名 |
| `READONLY` | 框架 | 布尔标记（存在但未用于调度） |
| `handle(args, context)` | 框架 | 执行逻辑，返回 `ToolResult` |

注册表 `ToolRegistry` 通过 `auto_discover()` 扫描 `core/tools/*.py` 自动注册。Agent Loop 通过 `registry.execute(name, args, context)` 一行查表执行。

这套设计达成了一个目标：**加工具不需要改循环**。

### 2.2 ToolContext 的现状

```python
@dataclass
class ToolContext:
    working_dir: str = ""    # 4 个工具在用
    session_id: str = ""     # 从未使用，始终为空
```

它在 agent loop 中的生命周期：
- 构造一次：`ToolContext(working_dir=os.getcwd())`
- 全程不更新
- 所有工具共享同一个实例

### 2.3 十个局限性

**工程层面（已识别）：**

1. handler 无异常保护 → tool_call/result 数量不匹配 → API 400
2. 无输出截断 → 超长输出浪费 token
3. 无并行执行 → 只读工具也要串行等待
4. 无权限系统 → READONLY 标记存在但从未被消费
5. OpenAI 协议耦合 → 无法适配其他 provider

**设计层面（深层局限）：**

6. ToolResult 只有纯文本 → 不支持结构化/多模态结果
7. ToolContext 极其简陋 → 工具无法感知执行环境
8. 工具是静态的 → 运行时不能动态增删
9. 工具无自省能力 → 无法声明能力边界
10. 工具间无协作机制 → 输出只能回到 LLM 消费

---

## 三、Claude Code 的工具系统：实际实现分析

v1 用一个对照表概括了 CC 的工具接口（12 个方法 vs Harness 的 3 个属性）。v2 聚焦 CC **最核心的设计决策**，而不是功能清单。

### 3.1 readFileState：工具对文件系统的认知

这是 CC 工具系统中**最精妙的设计**，远超"缓存"的概念。

**数据结构**：不是 `path → content` 的简单 dict，而是：

```typescript
readFileState: Map<absolutePath, {
  content: string           // 文件内容（CRLF 归一化）
  timestamp: number         // Math.floor(stats.mtimeMs)
  offset: number | undefined  // 读取偏移（undefined = 全文）
  limit: number | undefined   // 读取行数限制
}>
```

**四种消费场景**：

| 场景 | 工具 | 行为 |
|------|------|------|
| **读后缓存** | FileReadTool | 成功读取后 `readFileState.set(path, {content, timestamp, offset, limit})` |
| **读前去重** | FileReadTool | 检查 readFileState，如果同文件同范围且 timestamp 未变，返回 `file_unchanged` |
| **写前强制读** | FileEditTool | 检查 `readFileState.get(path)` 是否存在，不存在则拒绝执行 |
| **写前 staleness 检测** | FileEditTool | 比较 readFileState.timestamp 与文件当前 mtime，不一致则报错 `FILE_UNEXPECTEDLY_MODIFIED_ERROR` |
| **写后更新** | FileEditTool / BashTool(sed) | 写入后 `readFileState.set(path, {新内容, 新timestamp, undefined, undefined})` |

**一个机制解决四个问题**：
1. edit_file 不再盲目编辑——必须先 read_file
2. read_file 不会重复传输相同内容
3. 外部文件修改能被检测到
4. bash 的 sed 模拟也能保持 cache 一致

**compaction 时的处理**：

CC 的 compaction（上下文压缩）会清空 readFileState，但**保留最常用的 5 个文件**作为附件消息恢复：

```typescript
// compaction 时
const preCompactReadFileState = cacheToObject(context.readFileState)
context.readFileState.clear()

// compaction 后
// 按引用频率排序，取 top 5，总 token 预算 50,000
// 作为 'file_cache' 类型的附件消息恢复
```

### 3.2 ToolUseContext 的实际构造

CC 的 ToolUseContext 不是一次性构造的，而是**分层持有**：

```
QueryEngine（session 级）
  ├── readFileState          ← 跨所有请求持久化
  ├── mutableMessages        ← 消息主存储
  └── abortController        ← 会话级取消

queryLoop() 的 State（请求级）
  ├── messages               ← 当前请求的消息列表（引用 QueryEngine 的）
  ├── toolUseContext          ← 包含上述所有引用
  ├── turnCount              ← 当前轮次
  └── transition             ← 状态转移信息
```

关键设计：**readFileState 的所有权在 QueryEngine（session 级），不是 queryLoop（请求级）**。这意味着用户多次输入时，file_state 跨请求保留。

### 3.3 工具历史的处理：就是 messages

CC **不维护独立的 ToolResultRecord 或 tool_history**。工具调用的完整记录就是 messages 数组本身：

```
messages = [
  {role: "user", content: "帮我修复 bug"},
  {role: "assistant", tool_calls: [{function: {name: "read_file", arguments: ...}}]},
  {role: "tool", tool_call_id: "xxx", content: "文件内容..."},
  {role: "assistant", tool_calls: [{function: {name: "edit_file", arguments: ...}}]},
  {role: "tool", tool_call_id: "yyy", content: "已替换 1 处匹配"},
  ...
]
```

如果需要查"之前 bash 执行了什么命令"，遍历 messages 即可。不需要单独的历史索引。

### 3.4 并发执行与 context 共享

CC 的 `StreamingToolExecutor` 让并发安全的工具并行执行。关键发现：**并发工具共享同一个 ToolUseContext 对象**，不做拷贝。

这意味着 readFileState 在并发读取时是共享的——一个工具读的文件，另一个工具立刻能看到。对于 harness 来说，当前规模不存在并发安全问题，直接共享即可。

子代理的隔离通过 `AsyncLocalStorage` 实现（Node.js 的线程本地存储等价物），不是 context 拷贝。

### 3.5 buildTool() 工厂的安全默认值

CC 的 `buildTool()` 提供**保守默认值**：

```typescript
{
  isEnabled: true,
  isConcurrencySafe: false,   // 默认不可并发
  isReadOnly: false,           // 默认视为写操作
  isDestructive: false,        // 默认非破坏性
  checkPermissions: directAllow // 默认交给通用权限系统
}
```

工具**显式声明**自己安全才能享受优化。这是"默认保守、按需开放"的原则——比 Harness 的 `READONLY: bool`（存在但未用）更进一步。

### 3.6 执行管线中的 context 传递

CC 的工具执行管线（`toolExecution.ts`）在多个环节传递 context：

```
1. Zod schema 校验          → inputSchema.safeParse(input)
2. 业务验证                 → tool.validateInput(input, context)     ← context 首次参与
3. PreToolUse Hooks         → runPreToolUseHooks(tool, input, context) ← hooks 可修改 input
4. 权限检查                 → canUseTool(tool, input, context)        ← context 参与决策
5. 执行                     → tool.call(input, context)               ← context 传给工具
6. PostToolUse Hooks        → runPostToolUseHooks(result, context)    ← context 参与后处理
```

Context 不是只在 `tool.call()` 时才传进来——它在**验证、权限、hooks**等环节都是一等公民。

### 3.7 contextModifier：工具修改后续工具的上下文

CC 有一个 `contextModifier` 模式：工具执行完成后，可以修改后续工具看到的 context。这允许：
- 一个工具设置标志位，后续工具据此改变行为
- 动态调整 readFileState 的范围
- 传播状态而不经过 LLM 中转

---

## 四、ToolUseContext 的设计：从 CC 学到的教训

### 4.1 核心教训

基于 CC 的实际代码，提取出三个核心教训：

**教训 1：file_state 不是缓存，是认知。**

CC 不叫它"cache"，叫它 `readFileState`——工具对文件系统的**认知状态**。缓存是性能优化（可以失效、可以重建），认知是正确性保证（edit_file 必须基于对文件内容的认知才能安全编辑）。

这改变了设计方向：file_state 不应该是"read_file 顺便缓存一下"，而应该是"edit_file 的前置条件——你不知道文件内容就不能编辑"。

**教训 2：历史不需要单独维护。**

消息链就是历史。不需要 ToolResultRecord、不需要 tool_history 列表。如果工具想知道"之前发生了什么"，看 messages 就够了。

**教训 3：一个 context 对象贯穿循环，不重建，只更新。**

CC 在 queryLoop 开始时创建 context，然后在整个循环中传递同一个对象。identity 字段（tool_name 等）在每次调用前更新，file_state 持续累积，messages 自然增长。

### 4.2 Harness 的 ToolUseContext 设计

基于以上教训，设计 harness 的 ToolUseContext：

```python
from dataclasses import dataclass, field
from typing import Any
import os

@dataclass
class FileState:
    """工具对单个文件的认知状态。"""
    content: str                        # 文件内容
    timestamp: float                    # os.path.getmtime() 读取时的修改时间
    offset: int | None = None           # 读取偏移（None = 全文）
    limit: int | None = None            # 读取行数限制（None = 全文）

    @property
    def is_full_read(self) -> bool:
        """是否为完整读取（不是局部读取）。"""
        return self.offset is None and self.limit is None


class ToolUseContext:
    """工具执行上下文。

    参考 Claude Code 的 ToolUseContext 设计：
    - 环境层：构造时设置，工具只读
    - 身份层：每次 tool call 更新，工具只读
    - 文件认知层：工具可读写，框架控制一致性
    - 对话层：只读引用
    - 控制层：外部信号，工具只能查询
    """

    def __init__(self, *, working_dir: str, max_turns: int):
        # ── 环境层（构造时设置，工具只读）──
        self._working_dir = working_dir
        self._max_turns = max_turns

        # ── 身份层（每次 tool call 更新，工具只读）──
        self._tool_name: str = ""
        self._tool_call_id: str = ""
        self._turn_count: int = 0

        # ── 文件认知层（工具可读写）──
        self._file_state: dict[str, FileState] = {}

        # ── 对话层（只读引用）──
        self._messages: list[dict[str, Any]] | None = None

        # ── 控制层（外部信号）──
        self._cancelled: bool = False

    # ── 环境层 ──────────────────────────────────

    @property
    def working_dir(self) -> str:
        return self._working_dir

    @property
    def max_turns(self) -> int:
        return self._max_turns

    # ── 身份层 ──────────────────────────────────

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def tool_call_id(self) -> str:
        return self._tool_call_id

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def _set_call_identity(self, *, name: str, call_id: str, turn: int) -> None:
        """Agent loop 在每次 tool call 前调用。"""
        self._tool_name = name
        self._tool_call_id = call_id
        self._turn_count = turn

    # ── 文件认知层 ──────────────────────────────

    def get_file_state(self, path: str) -> FileState | None:
        """查询工具对某个文件的认知。返回 None 表示未认知（未读过）。"""
        return self._file_state.get(path)

    def set_file_state(self, path: str, state: FileState) -> None:
        """记录对文件的认知（read_file 成功后调用）。"""
        self._file_state[path] = state

    def update_file_state(self, path: str, content: str) -> None:
        """写工具修改文件后更新认知（edit_file / write_file 成功后调用）。"""
        self._file_state[path] = FileState(
            content=content,
            timestamp=os.path.getmtime(path),
        )

    def invalidate_file_state(self, path: str) -> None:
        """使对某个文件的认知失效（文件被外部修改等场景）。"""
        self._file_state.pop(path, None)

    # ── 对话层 ──────────────────────────────────

    def set_messages(self, messages: list[dict[str, Any]]) -> None:
        """Agent loop 构造时调用，传入只读引用。"""
        self._messages = messages

    @property
    def messages(self) -> list[dict[str, Any]] | None:
        return self._messages

    # ── 控制层 ──────────────────────────────────

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _cancel(self) -> None:
        """外部（用户 Ctrl+C）调用，请求取消。"""
        self._cancelled = True
```

### 4.3 Agent Loop 中的注入时机

```python
# agent.py — 修改点标注

def agent_loop(messages):
    client = create_llm_client()
    tools_schema = registry.schemas()

    # ── 构造 context ──
    context = ToolUseContext(working_dir=os.getcwd(), max_turns=MAX_TURNS)
    context.set_messages(messages)                              # ← 对话层注入

    response = _call_llm(client, messages, tools=tools_schema)
    choice = response.choices[0]

    if choice.finish_reason != "tool_calls":
        # ... 直接回复 ...
        return

    turn_count = 0
    while choice.finish_reason == "tool_calls":
        turn_count += 1

        msg_dict = choice.message.model_dump()
        messages.append(msg_dict)

        tool_results = []
        for tool_call in msg.tool_calls:
            args = _parse_tool_args(tool_call.function.arguments)
            name = tool_call.function.name

            # ── 身份层注入 ──
            context._set_call_identity(
                name=name,
                call_id=tool_call.id,
                turn=turn_count,
            )

            result = registry.execute(name, args, context)      # ← context 传入
            print(result.output)

            tool_results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result.output,
            })

        messages.extend(tool_results)
        response = _call_llm(client, messages, tools=tools_schema)
        choice = response.choices[0]
```

### 4.4 工具如何消费 file_state

**read_file — 写入认知：**
```python
def handle(args, context):
    file_path = Path(context.working_dir) / args["path"]
    content = file_path.read_text()
    # ...
    # 成功后记录认知
    context.set_file_state(str(file_path), FileState(
        content=content,
        timestamp=file_path.stat().st_mtime,
        offset=args.get("offset"),
        limit=args.get("limit"),
    ))
    return ToolResult(output=formatted_output, success=True)
```

**edit_file — 强制 read-before-write：**
```python
def handle(args, context):
    file_path = Path(context.working_dir) / args["path"]

    # ── 强制 read-before-write（参考 CC）──
    state = context.get_file_state(str(file_path))
    if not state or not state.is_full_read:
        return ToolResult(
            output="请先使用 read_file 读取此文件，再进行编辑。",
            success=False,
            error="not_read",
        )

    # ── staleness 检测（参考 CC）──
    current_mtime = file_path.stat().st_mtime
    if current_mtime != state.timestamp:
        return ToolResult(
            output="文件在你读取后被修改了，请重新读取。",
            success=False,
            error="stale",
        )

    # ... 执行编辑 ...

    # ── 写后更新认知（参考 CC）──
    context.update_file_state(str(file_path), new_content)

    return ToolResult(output=f"已替换 {count} 处匹配", success=True)
```

**write_file — 写后更新认知：**
```python
def handle(args, context):
    file_path = Path(context.working_dir) / args["path"]
    # ... 写入文件 ...

    # 写后更新认知
    context.update_file_state(str(file_path), args["content"])

    return ToolResult(output=f"已创建 {path} (N 行)", success=True)
```

### 4.5 去掉 ToolResultRecord 的理由

CC 不维护独立的工具历史记录。理由：

1. **messages 已经是完整的历史** — 每次 tool_call 和 tool_result 都在消息链里
2. **避免双写** — 如果同时维护 messages 和 tool_history，两者可能不一致
3. **内存效率** — 不需要额外存一份 ToolResult（output 可能很长）
4. **查询足够** — 如果需要查"之前 bash 执行了什么"，遍历 messages 中 `role: "tool"` 的条目即可

对 harness 来说，直接传 messages 引用就够了。未来如果需要更高效的历史查询，可以加一个轻量索引（指向 messages 中的位置），而不是复制一份完整记录。

---

## 五、从 CC 学到的设计原则

### 原则 1：认知 > 缓存

file_state 不是性能优化手段，而是**正确性保证**。edit_file 要求"你必须先 read_file"不是建议，是强制。这改变了工具之间的关系——不是"工具 A 顺便存一下结果给工具 B 用"，而是"工具 B 的前置条件是工具 A 的输出"。

### 原则 2：一个对象贯穿循环

Context 不重建、不拷贝，只更新。identity 字段在每次调用前被 agent loop 覆写，file_state 持续累积，messages 自然增长。所有工具看到的是同一个 context 实例。

### 原则 3：默认保守，按需开放

CC 的 `buildTool()` 默认 `isConcurrencySafe: false`、`isReadOnly: false`。工具必须显式声明自己安全才能享受优化。这避免了"不小心标记为安全导致并发 bug"的问题。

### 原则 4：历史就是消息

不维护独立的历史索引。messages 是唯一的真相来源。

### 原则 5：Context 参与整个执行管线

CC 不只在 `tool.call()` 时传入 context——它在 validateInput、permission check、hooks 环节都是一等公民。这意味着 context 的设计必须考虑权限、验证等非执行场景的需求。

### 原则 6：工具指南在 schema description，不在 system prompt

这是一个容易犯错的架构决策。CC 的 `tool.prompt()` 方法返回的**不是**要拼接到 system prompt 中的文本，而是工具 JSON schema 的 `description` 字段值。

**CC 的实际做法**（源码 `src/utils/api.ts`）：

```typescript
// tool.prompt() 的返回值成为 API 请求中工具的 description
base = {
  name: tool.name,
  description: await tool.prompt({ ... }),  // ← 这里！
  input_schema,
}
```

**CC 的 system prompt**（`src/constants/prompts.ts`）确实引用了工具名（通过常量），但那是行为指导（"用 Read 而不是 cat"），不是工具的使用说明。

**两者的职责区分**：

| 通道 | 内容 | 谁消费 |
|------|------|--------|
| **SCHEMA description**（= CC 的 `prompt()`） | 详细的工具使用说明：行为要点、参数用法、使用场景、注意事项 | 模型在决策"调用哪个工具、传什么参数"时 |
| **System prompt** | 通用行为准则：身份、工作原则、多工具协作、错误处理策略 | 模型在理解"我应该怎么工作"时 |

**为什么不能把工具指南拼进 system prompt：**
1. system prompt 中硬编码工具名 → 加新工具必须改 system prompt → 违反"加工具不需要改其他文件"的原则
2. system prompt 是全局的，工具指南是局部的 → 混在一起职责不清
3. CC 的工具 schema 有 session 缓存（`toolSchemaCache.ts`），system prompt 变化会破坏缓存

**对 Harness 的影响**：工具的 `PROMPT` 字符串应合并到 `SCHEMA` 的 `description` 字段中（详细的使用说明），`get_system_context()` 只返回通用行为准则，不拼接任何工具 PROMPT。

---

## 六、三层架构：从 Harness 到 CC 的演进路径

### 6.1 从两层到三层

Harness 当前的架构是两层的：

```
Agent Loop（决定做什么 + 执行工具）
    ↓ for tool_call in tool_calls: registry.execute(...)
Tool.handle()（执行）
```

Agent Loop 既负责 LLM 交互和循环控制，又直接 for 循环执行工具。没有编排层——收到几个 tool_call 就串行跑几个。

CC 的实际架构是三层的：

```
Agent Loop          — LLM 决策 + 循环控制 + messages 管理
ToolExecutorRuntime — 分批 + 并发 + 顺序保证 + 异常保护 + context 注入
ToolUseContext      — 文件认知 + 身份 + 环境信息
```

每层职责完全不同：

| 层 | 职责 | 核心问题 |
|---|---|---|
| **Agent Loop** | 调 LLM，拿到 tool_calls，决定循环是否继续 | "下一步做什么" |
| **ToolExecutorRuntime** | 接收 tool_calls 列表，编排执行 | "怎么安全高效地执行这一批" |
| **ToolUseContext** | 在执行过程中传递认知 | "执行时工具需要知道什么" |

为什么叫 **Runtime** 而不是 Executor？因为 Executor 听起来只是"执行工具"的升级版。而这一层管的是运行时策略——并发规则、写顺序、context 隔离、错误传播。这些是**运行时环境**的事，不只是执行。

### 6.2 ToolExecutorRuntime 要解决的五个核心问题

工具发现（`auto_discover()`）已经解决了。真正难的是**编排**：

#### 问题 1：多工具之间如何共存

CC 的答案：工具声明 `isConcurrencySafe()`，Runtime 据此分批。但 CC 的粒度是**工具级别**——read_file 永远安全，edit_file 永远不安全。

对 harness 来说，用已有的 `READONLY` 标记做第一版：

```
READONLY=True  → 可与其他只读工具并行
READONLY=False → 独占执行
```

更精细的调用级检查（同一文件的读写冲突）是远期目标，当前工具级够用。

#### 问题 2：哪些能并发

分批规则：

```
batch 1: 所有 READONLY=True 的工具 → 并行
batch 2+: READONLY=False 的工具，各自独占一个 batch → 串行
```

示例：
```
LLM 返回: [read("a.py"), read("b.py"), edit("a.py"), find("*.py")]
  → batch 1 (并行): read("a.py"), read("b.py"), find("*.py")  — 全是只读
  → batch 2 (串行): edit("a.py")                               — 写操作，独占
```

#### 问题 3：并发时如何保证回写顺序稳定

LLM 期望 tool_result 按 tool_call 顺序返回。并行执行时完成顺序不确定，但**回写 messages 时必须按原始 tool_call 顺序排列**。

```python
# 并行执行，但结果按原始顺序排列
futures = {idx: executor.submit(run, call) for idx, call in enumerate(readonly_calls)}
results = {idx: f.result() for idx, f in futures.items()}
# 按 idx 排序回写
ordered_results = [results[i] for i in sorted(results)]
```

#### 问题 4：并发时如何避免共享 context 被抢写

file_state 的并发写入是核心问题。三种策略：

**策略 A（推荐，CC 的做法）：只读工具不写 file_state，只有串行的写工具写。**
- 并行的全是 READONLY，它们只读不写 file_state
- 等并行 batch 完成后，串行 batch 才开始写
- 天然无冲突

**策略 B：并行工具各自用 context 快照，执行完后合并。**
- 每个并行工具拿到 context 的浅拷贝
- 执行完后，按 tool_call 顺序依次合并 file_state
- 冲突时后执行的覆盖

**策略 C：file_state 加锁。**
- 每个文件一个锁，读前加读锁，写前加写锁
- 最精细但也最复杂

对 harness 当前规模，**策略 A** 最合适：只读工具并行时暂不更新 file_state，串行处理时再更新。

#### 问题 5：工具报错时是否中止其他工具

CC 的做法：bash 出错时**级联取消**所有兄弟工具（`siblingAbortController`）。

对 harness 当前阶段，用**宽松策略**：一个工具报错，其他继续执行。所有结果（含错误）一起回传给 LLM，让 LLM 自己决定怎么办。比框架自作主张取消更安全——LLM 拥有最完整的上下文来判断下一步。

### 6.3 ToolExecutorRuntime 设计

```python
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolCall:
    """对 API 返回的 tool_call 的内部表示。"""
    idx: int                    # 在原始 tool_calls 列表中的位置（保证回写顺序）
    name: str                   # 工具名
    call_id: str                # API 的 tool_call.id
    args: dict[str, Any]        # 解析后的参数


@dataclass
class Batch:
    """一批可一起执行的 tool call。"""
    calls: list[ToolCall]
    parallel: bool              # True = 可并行，False = 必须串行（独占）


class ToolExecutorRuntime:
    """工具执行运行时：管理一批 tool_call 的编排执行。

    职责：分批、并发控制、回写顺序保证、异常保护、context 注入。
    不负责：LLM 调用、循环控制、messages 管理（这些是 Agent Loop 的事）。
    """

    def __init__(self, registry, context):
        self._registry = registry
        self._context = context

    def execute_batch(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """接收一批 tool_call，分批执行，返回有序结果。"""
        # 1. 分批
        batches = self._partition(tool_calls)

        all_results: dict[int, ToolResult] = {}

        for batch in batches:
            if batch.parallel:
                batch_results = self._execute_parallel(batch)
            else:
                batch_results = self._execute_serial(batch)
            all_results.update(batch_results)

        # 2. 按原始 tool_call 顺序返回
        return [all_results[i] for i in range(len(tool_calls))]

    def _partition(self, calls: list[ToolCall]) -> list[Batch]:
        """将 tool_calls 按 READONLY 标记分成批次。

        规则：连续的只读工具归入一个并行 batch，写工具各自独占一个串行 batch。
        """
        batches: list[Batch] = []
        current_parallel: list[ToolCall] = []

        for call in calls:
            is_readonly = self._registry.is_readonly(call.name)

            if is_readonly:
                current_parallel.append(call)
            else:
                # 遇到写操作：先 flush 当前并行 batch，再开一个独占 batch
                if current_parallel:
                    batches.append(Batch(current_parallel, parallel=True))
                    current_parallel = []
                batches.append(Batch([call], parallel=False))

        if current_parallel:
            batches.append(Batch(current_parallel, parallel=True))

        return batches

    def _execute_parallel(self, batch: Batch) -> dict[int, ToolResult]:
        """并行执行只读工具。结果按 idx 排序。"""
        results: dict[int, ToolResult] = {}

        with ThreadPoolExecutor(max_workers=len(batch.calls)) as pool:
            futures = {
                pool.submit(self._run_single, call): call.idx
                for call in batch.calls
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()

        return results

    def _execute_serial(self, batch: Batch) -> dict[int, ToolResult]:
        """串行执行写工具（独占）。"""
        results: dict[int, ToolResult] = {}
        for call in batch.calls:
            results[call.idx] = self._run_single(call)
        return results

    def _run_single(self, call: ToolCall) -> ToolResult:
        """执行单个工具调用（带异常保护 + context 注入）。"""
        # 注入身份
        self._context._set_call_identity(
            name=call.name, call_id=call.call_id, turn=self._context.turn_count
        )
        try:
            return self._registry.execute(call.name, call.args, self._context)
        except Exception as e:
            return ToolResult(
                output=f"Internal error: {e}",
                success=False,
                error="internal_error",
            )
```

**这个设计回答了五个问题：**

| 问题 | 解决方案 |
|------|---------|
| 共存 | `_partition()` 按 READONLY 分批 |
| 并发 | 只读 batch 用 ThreadPoolExecutor 并行 |
| 回写顺序 | `all_results` 按 idx 排序后返回 |
| context 抢写 | 并行 batch 全是只读工具（不写 file_state），写工具独占串行 |
| 错误传播 | 宽松策略——错误作为 ToolResult 返回，不取消其他工具 |

### 6.4 Agent Loop 的简化

有了 Runtime，agent loop 中的工具执行逻辑从 for 循环变为一行委托：

```python
# 之前：agent loop 自己编排执行
for tool_call in msg.tool_calls:
    args = _parse_tool_args(tool_call.function.arguments)
    name = tool_call.function.name
    result = registry.execute(name, args, tool_context)
    tool_results.append(...)

# 之后：agent loop 委托给 Runtime
runtime = ToolExecutorRuntime(registry, context)
tool_calls = [
    ToolCall(idx=i, name=tc.function.name, call_id=tc.id,
             args=_parse_tool_args(tc.function.arguments))
    for i, tc in enumerate(msg.tool_calls)
]
tool_result_list = runtime.execute_batch(tool_calls)

# 回写 messages（保持原始顺序）
tool_results = [
    {"role": "tool", "tool_call_id": tc.call_id, "content": r.output}
    for tc, r in zip(tool_calls, tool_result_list)
]
messages.extend(tool_results)
```

Agent loop 不再知道"工具是串行还是并行执行的"——它只管"给 Runtime 一批 tool_call，拿回一批 ToolResult"。

---

## 七、Harness 改进路线（修正版）

基于三层架构，修正改进路线。Phase 之间的依赖关系：

```
Phase 1: ToolUseContext + FileState + read-before-write    ✅ 已实现
    ↓ （context 就绪后 Runtime 才能工作）
Phase 2: ToolExecutorRuntime（分批 + 并发 + 异常保护）     ✅ 已实现
    ↓ （Runtime 就绪后才能做精细权限）
Phase 3: 工具注解 + 输出截断                                ✅ 已实现
    ↓
Phase 3.5: System Prompt 上下文系统                        ✅ 已实现
    ↓
Phase 4: 协议抽象 + 动态工具发现
```

### Phase 1：ToolUseContext + FileState（一次实现）✅

**修改文件：**

| 文件 | 变更 |
|------|------|
| `core/tools/__init__.py` | ToolUseContext class + FileState dataclass；去掉 ToolContext |
| `core/agent.py` | 构造 context、注入 identity 和 messages |
| `core/tools/read_file.py` | 成功后调用 `context.set_file_state()` |
| `core/tools/edit_file.py` | 强制 read-before-write + staleness 检测 + 写后更新 |
| `core/tools/write_file.py` | 写后调用 `context.update_file_state()` |
| `core/tools/bash.py` | 无变更 |
| `core/tools/find.py` | 无变更 |

**关键行为变更：**
- edit_file 必须 read_file 先执行，否则拒绝编辑
- edit_file 检测文件外部修改，防止编辑 stale 内容
- read_file 的结果被后续工具共享（不再只存在于 LLM 上下文中）

### Phase 2：ToolExecutorRuntime（分批 + 并发 + 异常保护）✅

**新增文件：**

| 文件 | 内容 |
|------|------|
| `core/runtime.py` | ToolExecutorRuntime + ToolCall + Batch |

**修改文件：**

| 文件 | 变更 |
|------|------|
| `core/agent.py` | for 循环改为 `runtime.execute_batch()` |
| `core/tools/__init__.py` | ToolRegistry 增加 `is_readonly()` 方法 |

### Phase 3：工具注解 + 输出截断 ✅

- READONLY → ANNOTATIONS（destructive / idempotent / concurrency_safe）
- ToolResult 增加 `truncated: bool`，超长输出截断

### Phase 3.5：System Prompt 上下文系统 ✅

**核心设计**（参考 CC 的 `prompt()` → schema description 模式）：

- 每个工具的 SCHEMA `description` 字段包含详细使用指南（行为要点、参数用法、使用场景）
- `core/context.py` 的 `get_system_context()` 只返回通用行为准则，不引用具体工具名
- `get_user_context()` 返回环境信息（OS、cwd、date）
- 加新工具只需要在工具文件里写好 SCHEMA description，不需要改 context.py 或 agent.py

**关键认知修正**：CC 的 `tool.prompt()` 返回值是工具 schema 的 `description` 字段，不是拼接到 system prompt 的文本。

### Phase 4：协议抽象 + 远期目标

- agent.py 和 API 之间加协议抽象层
- 动态工具发现（运行时注册/注销）
- 工具间管道（一个工具的输出直接传给下一个）

---

## 八、结论

v1 的结论是"工具从被动执行器变为主动能力提供者"。v2 的结论是**三层架构**：

```
Agent Loop          — "做什么"（LLM 决策 + 循环控制）
ToolExecutorRuntime — "怎么做"（分批 + 并发 + 顺序保证 + 异常保护）
ToolUseContext      — "知道什么"（文件认知 + 身份 + 环境）
```

ToolUseContext 解决的核心问题：将 tool 从无状态函数变为**有认知的执行单元**。edit_file 必须先认知（read_file）才能行动（edit），这是从"函数调用"到"认知-行动循环"的范式转变。

ToolExecutorRuntime 解决的核心问题：管理多工具执行的**运行时策略**。不是 agent loop 直接 for 循环跑工具，而是委托给 Runtime 做分批、并发、顺序保证和异常保护。Agent loop 从"执行器"变成"编排者"。

这两者合在一起，才是从"简单 Agent 框架"到"可用 Agent 系统"的关键跃迁。

---

## 参考资料

1. [MCP Specification 2025-03-26 — Tool Annotations](https://spec.modelcontextprotocol.io/specification/2025-03-26/server/tools/#tool-annotations)
2. [MCP Tools Concept](https://modelcontextprotocol.io/docs/concepts/tools)
3. [OpenAI Function Calling Guide](https://platform.openai.com/docs/guides/function-calling)
4. [Anthropic Tool Use Documentation](https://docs.anthropic.com/en/docs/build-with-claude/tool-use)
5. [Building Effective Agents — Anthropic Research](https://www.anthropic.com/research/building-effective-agents)
6. [LangChain Tools Concept](https://python.langchain.com/docs/concepts/tools/)
7. Claude Code 源码 `src/Tool.ts` — Tool 接口定义、buildTool 工厂
8. Claude Code 源码 `src/context.ts` — getSystemContext()（gitStatus + cacheBreaker）、getUserContext()（claudeMd + currentDate）
9. Claude Code 源码 `src/constants/prompts.ts` — 主 system prompt 模板（~915 行），引用工具名常量做行为指导
10. Claude Code 源码 `src/utils/api.ts` — tool.prompt() 返回值成为 schema description 字段（关键发现）
11. Claude Code 源码 `src/constants/systemPromptSections.ts` — 分段缓存机制（memoized vs volatile）
12. Claude Code 源码 `src/utils/toolSchemaCache.ts` — 工具 schema 的 session 缓存
8. Claude Code 源码 `src/services/tools/toolExecution.ts` — 执行管线
9. Claude Code 源码 `src/tools/BashTool/BashTool.tsx` — sed 模拟更新 readFileState
10. Claude Code 文档 `docs/04-tools.md` — ToolUseContext 字段表、buildTool 默认值
11. Claude Code 文档 `docs/03-agent-loop.md` — State 构造、QueryEngine 持久化 readFileState
12. Claude Code 文档 `docs/11-tools-file-operations.md` — readFileState 读写路径、staleness 检测
13. Claude Code 文档 `docs/13-tools-execution.md` — BashTool sed 模拟
14. Claude Code 文档 `docs/24-services-compact.md` — compaction 时 readFileState 的处理