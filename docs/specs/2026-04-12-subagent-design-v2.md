# Subagent 设计 V2

> 本文档为新增设计稿，不替换也不修改已有的 `2026-04-12-subagent-design.md`。
> 本稿吸收 Claude Code 的架构经验，但约束在当前 harness 的真实代码形态内，目标是得到一个可落地、可验证、可演进的 V2 方案。

## 问题重述

大型任务运行时，主 agent 的 `messages` 会不断膨胀。

典型场景：

- 为了定位一个 bug，需要搜索、读取、对比大量文件
- 为了规划一次改动，需要先理解模块边界和依赖关系
- 为了验证某个推断，需要跑若干只读命令和局部测试

这些中间步骤虽然必要，但并不都应该永久污染主 agent 上下文。最终真正需要留给主 agent 的，通常只是：

- 一个简洁结论
- 若干关键证据
- 修改了哪些文件
- 下一步建议

因此，subagent 的核心价值不是“并行”或“炫技”，而是：

**将高噪声、高探索性的中间工作隔离到独立上下文里运行，再把低噪声结果带回主 agent。**

## 来自 Claude Code 的启发

阅读 Claude Code 文档后，可以提炼出几个关键事实：

1. `subagent` 对外是一个 tool，但对内不是“一个普通工具函数”。
2. Claude Code 不只有一种 subagent，而是有明确的 agent type，例如 `general-purpose`、`Explore`、`Plan`、`fork`。
3. 不同 agent type 对应不同的：
   - system prompt
   - 工具权限
   - 上下文策略
   - 模型策略
4. `fork` 和普通子代理不是一回事：
   - 普通子代理使用独立提示词和独立任务
   - `fork` 子代理才继承父上下文
5. 子代理运行有明确生命周期和终态，而不是只返回一段字符串。

对 harness 的借鉴结论：

- **保留“subagent 作为 tool 入口”**
- **放弃“只加一个 `core/tools/subagent.py` 就够了”的判断**
- **引入最小必要的 subagent runtime 概念**
- **第一阶段先做同步、进程内、可控的子代理**
- **明确 agent type，而不是只做一个模糊的通用 subagent**

## 设计目标

### 必须达成

1. 子代理有独立的 `messages`
2. 子代理有独立的 `ToolUseContext`
3. 子代理可被限制最大执行轮次，防止死循环
4. 子代理完成后，主 agent 只接收简洁结果
5. 子代理内部的 todo 状态不污染主 agent
6. 主 agent 能准确知道子代理是：
   - 成功完成
   - 达到上限
   - 运行失败
   - 无有效输出
7. 不修改旧 spec，新增独立实现方案

### 希望达成

1. 借鉴 Claude Code 的 agent type 思路
2. 为未来的 `fork`、后台执行、worktree 隔离预留接口
3. 将“静默执行”做成真实能力，而不是 UI 假象

## 非目标

V2 不做以下能力：

- 后台运行 subagent
- 多 subagent 并发执行
- worktree 隔离
- remote 隔离
- agent 间消息通信
- subagent 自定义模型选择
- 完整的 swarm/team 系统

这些能力在 Claude Code 中成立，是因为其内部已有完整的任务系统和执行后端。当前 harness 尚未具备对应基础设施，不应在 V2 一次性引入。

## 核心设计结论

### 1. `subagent` 继续作为 tool 暴露给 LLM

保留此方向，原因不变：

- 何时委派给子代理，最懂任务语义的是 LLM
- 与现有 tool 机制一致
- 主 agent 不需要引入新的显式控制语法

但这里必须补一句：

**`subagent` 只是入口，不是全部实现。**

对外：

- `core/tools/subagent.py`

对内：

- `core/subagent_runtime.py` 或 `core/subagent.py`
- 负责 agent definition、上下文构建、tool filtering、run result、失败语义

### 2. 引入 agent type，而不是只做一个通用子代理

V2 建议先支持 3 种类型：

| 类型 | 用途 | 上下文策略 | 工具权限 |
|------|------|-----------|---------|
| `explore` | 搜索代码、读文件、定位信息 | `fresh` | 只读工具 |
| `plan` | 输出实现计划、拆解改动 | `fresh` | 只读工具 |
| `general` | 完成一个独立子任务，可读可写 | `fresh` | 完整工具集（排除 `subagent`） |

说明：

- `explore` 和 `plan` 对应 Claude Code 的 Explore / Plan agent
- `general` 对应 Claude Code 的 general-purpose agent
- `fork` 不进入 V2 首发实现，但数据结构和接口为其预留

### 3. V2 默认只做 `fresh` 上下文，不做 `fork`

这里借鉴 Claude Code，但不直接照搬。

Claude Code 将普通子代理和 `fork` 区分开，这一点非常值得借鉴。对当前 harness，建议：

- V2 默认只支持 `fresh`
- V2 的内部类型中预留 `fork`
- 等主循环、运行结果、静默模式稳定后，再实现 `fork`

原因：

- `fresh` 已经能解决“上下文隔离”这个主问题
- `fork` 需要复制父消息、继承系统提示、处理 tool_result 占位等，更复杂
- 当前 harness 的 `AgentLoop.run()` 还没有返回结构化结果，先上 `fork` 风险过高

## 为什么旧方案需要修正

旧 spec 的总体方向正确，但以下结论不再成立：

### 1. “零框架改动”不成立

当前 `AgentLoop.run()` 会在内部直接构造新的 `ToolUseContext`，并固定使用全局 `MAX_TURNS`。

因此如果不改 `agent.py`：

- subagent 自己构造的 `ToolUseContext` 无法真正被 loop 使用
- subagent 自己传入的 `max_turns` 不会真正生效
- subagent 无法拿到真实 `turns_used`
- subagent 无法可靠收集 `files_modified`

结论：

**V2 必须接受“最小框架改动”，但仍然不需要大改框架。**

### 2. “只加 QuietRenderer 就能静默”不成立

当前 stdout 输出来自至少三层：

- `renderer.py`
- `llm_client.py`
- `runtime.py`

其中后两者直接写 stdout，不经过 renderer。

结论：

**V2 必须引入真正的 quiet/silent 运行选项，而不是只加一个 QuietRenderer 类。**

### 3. “空 ContextPipeline 即可”过于激进

如果 subagent 完全不用框架层 system prompt，那么它虽然拿到了工具，但未必继承主框架的行为规则。

尤其当前 harness 的框架级规则里包含：

- 多步任务应使用 todo
- 优先用工具而非纯文字

结论：

**V2 的 `fresh` 也应保留框架层 system prompt；只是默认不复制主对话历史。**

## V2 总体架构

```text
主 AgentLoop
  │
  │ tool_call: subagent(task=..., agent_type=...)
  ▼
subagent tool
  │
  │ 1. 解析请求
  │ 2. 根据 agent_type 选择 SubagentDefinition
  │ 3. 保存主 todo 状态
  │ 4. 构造独立的 subagent messages / ToolUseContext / tools_schema
  │ 5. 调用 SubagentRuntime.run()
  │ 6. 恢复主 todo 状态
  │ 7. 将结构化结果压缩为简洁 ToolResult.output
  ▼
ToolResult(output="简洁摘要", success=...)
  │
  ▼
主 agent 获得低噪声结果，继续工作
```

## Python 数据结构设计

### 1. 对 LLM 暴露的 tool schema

V2 建议去掉旧稿中的 `context` 字段。

原因：

- 将“任务”和“上下文”拆成两个自由文本字段，容易让模型把必要信息散落在两个位置
- Claude Code 的实践更偏向单一 `prompt`，要求 prompt 自包含
- 对子代理而言，最重要的是任务描述要完整，而不是字段数量多

建议 schema：

```python
SCHEMA = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": (
            "Delegate a substantial subtask to an isolated sub-agent. "
            "Use for codebase exploration, implementation planning, "
            "or isolated multi-step work that would otherwise bloat the main context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "A self-contained task prompt for the sub-agent. "
                        "Include all necessary context, constraints, and expected output format."
                    ),
                },
                "agent_type": {
                    "type": "string",
                    "enum": ["explore", "plan", "general"],
                    "description": "Sub-agent type. Default is general.",
                },
                "description": {
                    "type": "string",
                    "description": "A short label for status display, ideally 3-8 words.",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Optional per-subagent turn limit.",
                },
            },
            "required": ["task"],
        },
    },
}
```

### 2. 内部枚举

```python
class SubagentType(str, Enum):
    EXPLORE = "explore"
    PLAN = "plan"
    GENERAL = "general"
    FORK = "fork"  # 预留，V2 不启用


class SubagentContextMode(str, Enum):
    FRESH = "fresh"
    FORK = "fork"


class SubagentStopReason(str, Enum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    API_ERROR = "api_error"
    TOOL_ERROR = "tool_error"
    EMPTY_RESPONSE = "empty_response"
    CANCELLED = "cancelled"
```

### 3. 子代理定义

```python
@dataclass(frozen=True)
class SubagentDefinition:
    agent_type: SubagentType
    context_mode: SubagentContextMode
    default_max_turns: int
    include_project_context: bool
    allowed_tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    system_prompt_suffix: str = ""
```

说明：

- `allowed_tools=None` 表示默认使用当前注册工具全集，再应用黑名单
- `disallowed_tools` 用于显式排除危险或不适合该类型的工具
- `system_prompt_suffix` 只追加类型特定规则，不重写整个系统提示

### 4. Subagent 请求对象

```python
@dataclass
class SubagentRequest:
    task: str
    agent_type: SubagentType = SubagentType.GENERAL
    description: str | None = None
    max_turns: int | None = None
```

### 5. Agent 运行结果

V2 不应让 `AgentLoop.run()` 只返回 `None`。

建议新增：

```python
@dataclass
class AgentRunResult:
    final_output: str
    success: bool
    stop_reason: SubagentStopReason | str
    turns_used: int
    files_modified: list[str]
```

说明：

- 主 agent 和 subagent 都可以复用此结构
- 对主 agent 而言，先只用到 `final_output`
- 对 subagent 而言，`success` 和 `stop_reason` 是必须的

### 6. Subagent 运行结果

```python
@dataclass
class SubagentRunResult:
    request: SubagentRequest
    output: str
    success: bool
    stop_reason: SubagentStopReason
    turns_used: int
    files_modified: list[str]
```

## agent type 定义

### `explore`

用途：

- 搜索代码位置
- 阅读多个文件
- 分析 bug 原因
- 收集证据

特点：

- 只读
- 默认短轮次
- 结果应以“发现了什么”为主，而不是直接改文件

建议定义：

```python
EXPLORE_AGENT = SubagentDefinition(
    agent_type=SubagentType.EXPLORE,
    context_mode=SubagentContextMode.FRESH,
    default_max_turns=10,
    include_project_context=False,
    allowed_tools=("find", "read_file", "todo"),
    system_prompt_suffix=\"\"\"
你是只读探索代理。
- 只允许搜索、读取、分析
- 不要修改文件
- 输出聚焦于发现、证据、可能原因
\"\"\",
)
```

注意：

当前 harness 的 `bash` 工具不是只读工具，不能安全给 `explore`。

因此在 V2 中：

- `explore` 不开放 `bash`
- 如果未来需要只读 shell，应新增只读 shell 工具或给 `bash` 增加只读模式

### `plan`

用途：

- 设计实现步骤
- 列出关键文件
- 提供风险点与验证建议

特点：

- 只读
- 输出结构化计划
- 不直接修改文件

建议定义：

```python
PLAN_AGENT = SubagentDefinition(
    agent_type=SubagentType.PLAN,
    context_mode=SubagentContextMode.FRESH,
    default_max_turns=12,
    include_project_context=False,
    allowed_tools=("find", "read_file", "todo"),
    system_prompt_suffix=\"\"\"
你是规划代理。
- 只做分析与规划，不修改文件
- 输出应包含实施步骤、关键文件、风险点、验证方式
\"\"\",
)
```

### `general`

用途：

- 完成一个相对独立的多步子任务
- 可探索、可修改、可验证

特点：

- 读写都允许
- 适合“分析后直接实现”
- 仍然禁止继续生成 subagent

建议定义：

```python
GENERAL_AGENT = SubagentDefinition(
    agent_type=SubagentType.GENERAL,
    context_mode=SubagentContextMode.FRESH,
    default_max_turns=20,
    include_project_context=True,
    disallowed_tools=("subagent",),
    system_prompt_suffix=\"\"\"
你是通用子代理。
- 在隔离上下文中完成被分配的任务
- 如修改文件，回复中列出修改点
- 结论要简洁，避免把完整中间过程带回主代理
\"\"\",
)
```

### `fork`（预留）

V2 只预留，不实现。

未来语义：

- 继承父系统提示
- 继承父消息历史
- 用于高上下文重叠的并行执行

## Prompt 与上下文策略

### 1. `task` 必须自包含

借鉴 Claude Code 的 worker prompt 原则，主 agent 传给 subagent 的 `task` 应满足：

- 不引用“刚才那个”“上面那个报错”
- 明确目标
- 明确约束
- 明确期望输出
- 必要时包含关键路径、错误信息、代码片段

错误示例：

```text
看下刚才那个 bug，顺手修一下。
```

正确示例：

```text
请分析并修复 core/runtime.py 中 tool 执行日志刷屏的问题。
要求：
1. 子代理运行时不应向主终端输出 runtime 调试日志
2. 不改变主代理默认显示行为
3. 修改后说明影响的文件
如果无法完整修复，请说明阻塞点。
```

### 2. `fresh` 上下文的消息构造

V2 的 `fresh` 模式使用：

- 框架层 system prompt
- 可选的项目级 context
- 一个 user task

不复制：

- 主 agent 的历史 messages
- 主 agent 的工具结果历史

建议构造：

```python
sub_messages = [
    {"role": "system", "content": assembled_system_prompt},
    {"role": "user", "content": request.task},
]
```

### 3. system prompt 组合策略

不建议单独硬编码一份完全脱离主框架的 `_SUBAGENT_SYSTEM_PROMPT`。

建议策略：

```text
框架层 system prompt
+ 可选项目 context
+ agent_type 专属 suffix
```

原因：

- 保留框架共识
- 保留工具使用规则
- 只在子代理类型差异处追加约束

## 工具池设计

### 1. 总原则

Claude Code 的启发不是“所有子代理都全工具”，而是：

**工具池必须和 agent type 一起定义。**

### 2. V2 工具过滤规则

#### `explore`

- 允许：`find`, `read_file`, `todo`
- 禁止：`edit_file`, `write_file`, `bash`, `subagent`

#### `plan`

- 允许：`find`, `read_file`, `todo`
- 禁止：`edit_file`, `write_file`, `bash`, `subagent`

#### `general`

- 允许：全部已注册工具
- 禁止：`subagent`

### 3. 为什么 `explore` / `plan` 先禁用 `bash`

当前 harness 的 `bash` 定义为写工具，不是只读工具，也没有只读模式。

如果允许 `explore` 使用 `bash`，则“只读代理”这个语义会被破坏。

因此 V2 选择保守策略：

- 不给只读子代理 `bash`
- 未来如有需求，再引入 `bash_readonly` 或 `bash(mode='readonly')`

## 运行时设计

### 1. 新增 `SubagentRuntime`

建议新增模块：

- `core/subagent_runtime.py`

职责：

1. 解析 `SubagentRequest`
2. 选择 `SubagentDefinition`
3. 组装 subagent 的 system prompt
4. 过滤工具集
5. 构造独立 `ToolUseContext`
6. 调用 `AgentLoop.run(...)`
7. 收集并返回 `SubagentRunResult`

这部分不应塞进 `core/tools/subagent.py`。

`core/tools/subagent.py` 只做：

- schema
- 参数校验
- 调 runtime
- 将结构化结果压缩为 `ToolResult`

### 2. `AgentLoop.run()` 的新签名

建议改为：

```python
def run(
    self,
    messages: list[dict[str, Any]],
    *,
    tool_context: ToolUseContext | None = None,
) -> AgentRunResult:
```

语义：

- 如果调用方未传 `tool_context`，行为与今天一致
- 如果传入，则使用调用方提供的 context
- 无论如何都返回 `AgentRunResult`

### 3. `AgentRunResult.stop_reason`

V2 需要明确 stop reason，至少区分：

- `completed`
- `max_turns`
- `api_error`
- `tool_error`
- `empty_response`

原因：

- 主 agent 不能把所有 subagent 结果都当作成功
- 后续做后台执行、状态栏、日志摘要时也需要此字段

### 4. `ToolUseContext` 独立性

subagent 必须拥有独立 `ToolUseContext`，包括：

- `working_dir`
- `max_turns`
- `_file_state`
- `_messages`
- `_turn_count`

主 agent 和 subagent 不共享此对象。

## Todo 隔离

当前 `todo` 工具使用模块级单例。

V2 继续采用 save/restore 策略，但必须显式实现：

```python
def save_snapshot() -> PlanningState: ...
def restore_snapshot(snapshot: PlanningState) -> None: ...
def clear_state() -> None: ...
```

subagent 运行时：

```python
snapshot = save_snapshot()
try:
    clear_state()
    result = runtime.run(...)
finally:
    restore_snapshot(snapshot)
```

这是 V2 的最小方案。

长期来看，如果未来要支持并发 subagent，则 todo 状态必须从模块级单例迁移到 session / context 作用域。

## 文件状态一致性

主 agent 与 subagent 的文件认知必须彼此隔离，但主 agent 的旧认知在子代理改动文件后必须失效。

V2 继续采用旧稿中正确的那部分：

- `ToolUseContext.get_file_state()` 做 mtime 校验
- 如果文件被外部修改，则自动失效

建议实现：

```python
def get_file_state(self, path: str) -> FileState | None:
    state = self._file_state.get(path)
    if state is None:
        return None
    try:
        if os.path.getmtime(path) != state.timestamp:
            del self._file_state[path]
            return None
    except OSError:
        del self._file_state[path]
        return None
    return state
```

### 关于 `files_modified`

V2 保留 `files_modified`，但要明确定义语义：

- **它是“通过受管写工具修改的文件列表”**
- **不是“进程级真实全部修改文件列表”**

因此：

- `write_file`、`edit_file` 应记录修改路径
- `bash` 导致的改动不保证能被记录到 `files_modified`
- 但主 agent 的 `file_state` 仍能通过 mtime 机制自动失效

这两个目标不要混为一谈。

## 静默模式

### 1. 设计要求

当 subagent 运行时，主终端不应被其内部日志刷屏。

### 2. 现状问题

当前至少以下模块会直接写 stdout：

- `core/llm_client.py`
- `core/runtime.py`
- `core/renderer.py`

因此 V2 需要真正的 quiet 机制。

### 3. 建议方案

新增统一运行显示选项：

```python
@dataclass(frozen=True)
class RunDisplayOptions:
    quiet: bool = False
```

并将其贯穿：

- `OpenAIClient.call(..., display: RunDisplayOptions | None = None)`
- `ToolExecutorRuntime(..., display: RunDisplayOptions | None = None)`
- `AgentLoop(..., display: RunDisplayOptions | None = None)`

subagent 运行时传入：

```python
RunDisplayOptions(quiet=True)
```

主 agent 默认：

```python
RunDisplayOptions(quiet=False)
```

注意：

- 这比只加 `QuietRenderer` 更完整
- `QuietRenderer` 仍然有价值，但只能解决 renderer 层问题

## 子代理返回给主代理的内容格式

V2 内部使用结构化结果，但由于现有 `ToolResult.output` 仍是字符串，对主 agent 的回写建议压缩为如下格式：

```text
子代理已完成（type=explore, turns=6）。

结论：
- ...
- ...

关键证据：
- ...

修改文件：
- core/foo.py
- core/bar.py
```

如果失败：

```text
子代理未成功完成（reason=max_turns, turns=12）。

当前结论：
- ...

阻塞点：
- ...
```

要求：

- 尽量不带长篇中间过程
- 结果可直接被主 agent 消费
- `plan` 类型应强制输出“关键文件”

## `subagent` 工具的 handle 骨架

```python
def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    request = parse_subagent_request(args)
    snapshot = save_snapshot()
    try:
        clear_state()
        runtime = SubagentRuntime(parent_context=context)
        result = runtime.run(request)
        output = render_subagent_tool_output(result)
        return ToolResult(output=output, success=result.success)
    except Exception as e:
        return ToolResult(
            output=f"子代理执行失败: {e}",
            success=False,
            error="subagent_error",
        )
    finally:
        restore_snapshot(snapshot)
```

注意：

- `handle()` 不直接操纵消息循环细节
- `handle()` 不自己拼装全部运行逻辑
- 运行逻辑下沉到 `SubagentRuntime`

## 需要修改的文件

### 新增文件

| 文件 | 说明 |
|------|------|
| `core/tools/subagent.py` | 对 LLM 暴露的 subagent tool |
| `core/subagent_runtime.py` | 子代理运行时、agent type、上下文构建、结果收集 |

### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `core/agent.py` | `run()` 支持外部 `tool_context` 并返回 `AgentRunResult` |
| `core/runtime.py` | 支持 quiet 模式，避免 subagent 刷屏 |
| `core/llm_client.py` | 支持 quiet 模式，避免 subagent 刷屏 |
| `core/renderer.py` | 可保留 `QuietRenderer`，但作为整体 quiet 机制的一部分 |
| `core/tools/__init__.py` | `get_file_state()` 加 mtime 校验；记录受管修改路径 |
| `core/tools/todo.py` | 新增 `save_snapshot()` / `restore_snapshot()` / `clear_state()` |
| `core/tools/write_file.py` | 记录受管修改路径 |
| `core/tools/edit_file.py` | 记录受管修改路径 |

## 不建议在 V2 做的修改

- 不要引入 worktree 逻辑
- 不要为 subagent 引入远程执行
- 不要在 V2 做 teammate / swarm
- 不要让 subagent 默认后台运行
- 不要在没有结构化 `AgentRunResult` 的情况下强行做多子代理并发

## 实现阶段建议

### Phase 1: 最小可运行版

目标：

- `general` / `explore` / `plan` 三种 agent type
- 同步执行
- 独立 `messages`
- 独立 `ToolUseContext`
- todo save/restore
- `AgentRunResult`
- quiet 模式

### Phase 2: `fork` 模式

前提：

- Phase 1 稳定
- `AgentLoop.run()` 的返回结果可靠
- subagent 输出压缩质量稳定

能力：

- `fork` agent type
- 父系统提示继承
- 必要消息历史继承
- 防止 fork 内再 fork

### Phase 3: 后台与隔离执行

能力：

- 后台 subagent
- worktree 隔离
- 更完整的任务状态管理

## 测试要求

V2 至少补以下测试：

1. `subagent` 使用自定义 `max_turns` 时，确实触发而不是回退到全局值
2. subagent 运行后，主 agent 的 todo 状态恢复正确
3. subagent 修改文件后，主 agent 的 `get_file_state()` 返回失效
4. `explore` / `plan` 类型无法调用写工具
5. subagent 失败时不会错误返回 success
6. quiet 模式下，subagent 不向主终端打印 llm/runtime 日志
7. `plan` 类型返回内容中包含关键文件

## 已知 UX 问题

### Subagent 结果会被用户看到两次

现象：

- `subagent` 作为工具执行完成时，`ToolResult.output` 会先在工具结果区域显示一次
- 随后主 agent 再基于这份 tool result 生成最终回答，用户又会再看到一次相近内容

结果：

- 用户观感上像“subagent 说了一遍，主 agent 又重复说了一遍”
- 对分析类任务尤其明显，因为 `render_subagent_summary()` 当前返回的是可直接面向用户阅读的长摘要

根因：

- 当前系统把“写回给主 agent 的工具结果”和“终端展示给用户的工具结果”视为同一份字符串
- 对普通工具这通常问题不大，但对 `subagent` 这种“工具内部又做了完整分析”的工具，会产生重复展示

推荐解法：

1. **分离模型可见结果和用户可见展示**
   - 写回 `messages` 的 tool result 保留中等长度、高信息密度摘要，供主 agent 继续推理
   - 终端展示只显示简短状态，例如：
     - `subagent 已完成（explore, turns=10，返回 3 条关键发现）`

2. **不要单纯把 tool result 极度压短**
   - 如果压缩的是写回主 agent 的内容，可能降低主 agent 最终回答质量
   - 子代理回传仍应包含：
     - 关键发现
     - 关键文件
     - 修改文件
     - stop reason（必要时）

3. **如果短期不做展示分离**
   - 可将 `render_subagent_summary()` 从“长报告”收敛为“中等长度高密度摘要”
   - 这是折中方案，不是最终方案

当前决策：

- V2 先接受这个 UX 限制，不阻塞 subagent 架构落地
- 后续如继续优化 subagent 体验，应优先处理这个问题

## 最终决策

V2 采用如下路线：

1. **保留 subagent 作为 tool 入口**
2. **新增最小 subagent runtime，而不是把所有逻辑塞进 tool handler**
3. **引入 agent type：`explore` / `plan` / `general`**
4. **默认只做 `fresh` 上下文，不在 V2 实现 `fork`**
5. **接受最小必要的框架改动：`AgentLoop.run()` 返回结构化结果并支持外部 context**
6. **将 quiet 做成真正的运行时能力**
7. **继续用 todo save/restore 作为当前阶段的最小隔离方案**

这条路线既吸收了 Claude Code 的核心经验，也尊重当前 harness 的体量和实现现实。
