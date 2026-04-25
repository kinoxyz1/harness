# 07: Subagent — 任务委派机制

> 一个人做不完所有事，AI 也一样。Subagent 让主 Agent 可以委派子任务给
> 专门的"小助手"，各自在独立的环境中工作，互不干扰。

---

## 你将理解什么

读完这篇，你会知道：

1. 为什么主 Agent 需要委派子任务
2. 三种预定义的子 Agent 类型有什么区别
3. 子 Agent 的运行环境为什么必须是隔离的
4. 子 Agent 和主 Agent 之间怎么传递结果
5. 子 Agent 有哪些安全约束

---

## 第一个问题：为什么需要委派

### 场景：用户说"分析整个项目的技术债务"

#### 单 Agent 做法

```text
主 Agent：
  → 读 src/auth.py（轮次 1）
  → 读 src/billing.py（轮次 2）
  → 读 src/api.py（轮次 3）
  → 读 src/core.py（轮次 4）
  → 读 src/utils.py（轮次 5）
  → 分析 auth 模块（轮次 6-8）
  → 分析 billing 模块（轮次 9-11）
  → 分析 api 模块（轮次 12-14）
  → 分析 core 模块（轮次 15-17）
  → 分析 utils 模块（轮次 18-20）
  → 汇总写报告（轮次 21-22）

问题：
  - 22 轮才完成
  - 对话历史变得很长（可能超出上下文窗口）
  - 中间的分析细节会稀释最终的报告质量
```

#### 多 Agent 做法

```text
主 Agent（项目经理）：
  "我来拆分任务"

  子 Agent 1（探索员 - EXPLORE）：
    任务："扫描项目结构，列出所有模块和文件数量"
    环境：隔离的，只能用 find 和 read_file
    结果："5 个模块：auth(8文件), billing(12文件), api(6文件), core(4文件), utils(3文件)"
    用了 3 轮

  子 Agent 2（分析师 - PLAN）：
    任务："分析每个模块的代码复杂度，识别技术债务"
    环境：隔离的，只能用 find 和 read_file
    结果："billing 模块复杂度最高（3个超200行的函数），api 模块有2个TODO未处理..."
    用了 7 轮

  主 Agent：
    看到子 Agent 的结果
    → 汇总写报告（2 轮）

总轮次：
  主 Agent: 4 轮（拆分 + 汇总）
  子 Agent 1: 3 轮
  子 Agent 2: 7 轮
  主 Agent 对话历史：只包含子任务的摘要，不包含中间细节
```

核心好处：

| 维度 | 单 Agent | 多 Agent |
|---|---|---|
| 主 Agent 上下文 | 很长（所有细节） | 短（只有摘要） |
| 并行性 | 串行 | 可以并行 |
| 安全性 | 所有工具都可用 | 子 Agent 工具受限 |
| 可控性 | 难以控制轮次 | 子 Agent 有独立轮次限制 |

---

## 预定义的 Agent 类型

### 三种内置 Agent

```python
# core/session/subagent.py

class SubagentType(StrEnum):
    EXPLORE = "explore"     # 只读探索
    PLAN = "plan"           # 分析和规划
    GENERAL = "general"     # 通用任务执行
```

| 类型 | 最大轮次 | 可用工具 | 禁用工具 | 适用场景 |
|---|---|---|---|---|
| **EXPLORE** | 10 | find, read_file, todo | subagent | 扫描项目、阅读代码 |
| **PLAN** | 12 | find, read_file, todo | subagent | 分析代码、制定方案 |
| **GENERAL** | 20 | 除 subagent 外的所有工具 | subagent | 实现功能、修改代码 |

### 为什么是这三种

**EXPLORE（探索员）：**
```text
任务："这个项目的目录结构是什么样的？"
  → 只需要看，不需要改
  → 10 轮够用（扫描目录 + 读几个文件）
  → 只给 find 和 read_file，确保不会误改任何东西
```

**PLAN（分析师）：**
```text
任务："分析 auth 模块的架构，给出重构建议"
  → 需要深入阅读代码
  → 可能需要制定计划（所以有 todo 工具）
  → 但仍然只读，不写代码
  → 12 轮够用（比 EXPLORE 多一点，因为分析更深入）
```

**GENERAL（执行者）：**
```text
任务："实现一个 CSV 导出功能"
  → 需要读写文件、执行命令
  → 20 轮够用（实现一个功能通常 10-15 轮）
  → 不能创建子 Agent（防止递归）
```

### 为什么所有子 Agent 都禁用 subagent

防止递归：

```text
主 Agent
  → 创建子 Agent
    → 子 Agent 创建子子 Agent
      → 子子 Agent 创建子子子 Agent
        → ...无限递归
        → 资源耗尽
```

`disallowed_tools=("subagent",)` 阻断了递归链条。

---

## 核心数据结构

### SubagentDefinition — Agent 的配置蓝图

```python
@dataclass(slots=True)
class SubagentDefinition:
    agent_type: SubagentType           # 类型
    context_mode: SubagentContextMode  # FRESH 或 FORK
    max_turns: int                     # 轮次上限
    allowed_tools: tuple[str, ...]     # 允许的工具名
    disallowed_tools: tuple[str, ...]  # 禁止的工具名
    system_prompt_suffix: str          # 追加到 system prompt 的指令
```

### SubagentRequest — 发起请求

```python
@dataclass(slots=True)
class SubagentRequest:
    task: str                          # 任务描述
    agent_type: SubagentType           # Agent 类型
    max_turns: int | None = None       # 可选的轮次覆盖
```

### SubagentRunResult — 执行结果

```python
@dataclass(slots=True)
class SubagentRunResult:
    output: str                        # 最终文字输出
    success: bool                      # 是否成功
    stop_reason: SubagentStopReason    # 为什么结束
    turns_used: int                    # 用了几轮
    files_modified: list[str]          # 修改了哪些文件
```

---

## 执行流程详解

### SubagentRuntime.run()

```python
# core/session/subagent.py
class SubagentRuntime:
    def run(self, request: SubagentRequest) -> SubagentRunResult:
        # 1. 获取 Agent 定义
        definition = _get_definition(request.agent_type)

        # 2. 计算允许的工具名
        allowed_names = _compute_allowed_names(definition)

        # 3. 创建过滤后的工具注册表
        filtered_registry = registry.filtered(allowed_names)

        # 4. 创建独立的子引擎
        engine = SessionEngine(
            model_gateway=new_model_gateway(),
            tool_runtime=new_tool_runtime(filtered_registry),
            view_builder=new_view_builder(filtered_registry),
            policy_runner=new_policy_runner(definition.max_turns),
        )

        # 5. 设置自定义 system prompt
        engine.state.system_prompt_override = build_system_prompt(definition)

        # 6. 运行子任务
        result = engine.submit_user_message(request.task)

        # 7. 返回结果
        return SubagentRunResult(
            output=result.final_output,
            success=result.success,
            turns_used=result.turns_used,
            files_modified=result.files_modified,
        )
```

### 逐步展开

#### 步骤 2-3：工具过滤

```text
完整的工具注册表：
  [bash, read_file, write_file, edit_file, find, skill, todo, subagent]

EXPLORE 类型的过滤：
  allowed_tools = ("find", "read_file", "todo")
  filtered_registry 只包含这 3 个工具

效果：
  子 Agent 调 API 时，tools 参数只有 3 个工具的 Schema
  模型看不到其他工具，自然不会调用
```

#### 步骤 4：创建独立引擎

```text
主 Agent 的 SessionEngine:
  session_state = {messages: [...50条...], invoked_skills: {...}, todo_state: {...}}
  tool_registry = [全部 7 个工具 + subagent]
  policy = [MaxTurnsPolicy(300), TodoPlanningPolicy()]

子 Agent 的 SessionEngine:
  session_state = {messages: [], invoked_skills: {}, todo_state: {}}  ← 全新的！
  tool_registry = [find, read_file, todo]  ← 过滤后的！
  policy = [MaxTurnsPolicy(10)]  ← 子 Agent 自己的轮次限制
```

完全隔离。子 Agent 看不到主 Agent 的对话历史和状态。

#### 步骤 5：自定义 system prompt

```python
system_prompt = f"""你是一个{agent_type}类型的 AI 助手。

{definition.system_prompt_suffix}

任务：{request.task}

可用工具：{', '.join(allowed_names)}
最大轮次：{definition.max_turns}
"""
```

子 Agent 的 system prompt 包含：
- 角色说明（"你是一个探索型助手"）
- 自定义追加指令
- 任务描述
- 工具限制提醒

#### 步骤 6：运行子任务

```text
engine.submit_user_message("扫描项目结构，列出所有模块")
  │
  ▼
  内部创建新的 RunState()
  │
  ▼
  QueryLoop.run()  ← 完整的 think-act 循环
  │  但只有 find, read_file, todo 可用
  │  最多 10 轮
  │
  ▼
  QueryResult(final_output="项目有 5 个模块...", turns_used=3)
```

### 完整流程图

```text
主 Agent 运行中
  │
  ▼
SubagentRuntime.run({
    task: "扫描项目结构，列出所有模块",
    agent_type: EXPLORE,
})
  │
  ▼
┌─── 创建隔离环境 ─────────────────────────────────────────┐
│                                                           │
│  1. 过滤工具注册表                                         │
│     全部工具: [bash, read, write, edit, find, skill, todo] │
│     过滤后:   [find, read_file, todo]                     │
│                                                           │
│  2. 创建新的 SessionEngine                                │
│     ├─ new SessionState()     ← 空的对话历史              │
│     ├─ filtered ToolRegistry  ← 只有 3 个工具             │
│     ├─ new ModelGateway()     ← 独立的 API 连接           │
│     └─ MaxTurnsPolicy(10)     ← 最多 10 轮                │
│                                                           │
│  3. 设置 system prompt                                    │
│     "你是一个只读探索 Agent。任务：扫描项目结构..."          │
│                                                           │
├─── 子 Agent 独立运行 ────────────────────────────────────┤
│                                                           │
│  QueryLoop.run()                                          │
│    │                                                      │
│    ├─ 轮次 1: find("**/*.py") → 找到 30 个文件             │
│    ├─ 轮次 2: read_file("src/auth/...") → 看目录结构       │
│    └─ 轮次 3: 文字回复 "项目有 5 个模块..."                │
│                                                           │
│  QueryResult(                                             │
│    final_output="项目有 5 个模块：auth, billing, ...",     │
│    turns_used=3,                                          │
│    success=True                                           │
│  )                                                        │
│                                                           │
└─── 子 Agent 结束 ──────────────────────────────────────────┘
  │
  ▼
render_subagent_summary(result)
  → "子任务完成（3 轮）
     结论：项目有 5 个模块：auth, billing, api, core, utils
     auth 模块有 8 个文件，billing 模块有 12 个文件..."
  │
  ▼
主 Agent 看到这段摘要，继续工作
```

---

## 三重隔离

### 1. 工具隔离

```text
EXPLORE 类型子 Agent 看到的工具列表：
  tools: [
    {name: "find", description: "搜索文件", ...},
    {name: "read_file", description: "读取文件", ...},
    {name: "todo", description: "更新计划", ...},
  ]
  ← 只有 3 个，bash、write_file、edit_file 完全不可见

模型看不到的工具 = 模型不可能调用的工具
```

### 2. 状态隔离

```text
主 Agent 的 session_state:
  conversation_messages: [50 条消息...]
  invoked_skills: {"analysis-report": InvokedSkillRecord(...)}
  todo_state: TodoState(items=[...])
  read_file_state: {"config.yaml": FileState(...), ...}

子 Agent 的 session_state:
  conversation_messages: []              ← 空
  invoked_skills: {}                     ← 空
  todo_state: TodoState(items=[])        ← 空
  read_file_state: {}                    ← 空

子 Agent 完全不知道主 Agent 在做什么。
它只知道自己收到的任务描述。
```

### 3. 轮次隔离

```text
主 Agent: turn_count = 5（已经跑了 5 轮）
子 Agent: turn_count = 0 → 1 → 2 → 3（独立计数）

子 Agent 用完 10 轮 → 强制停止 → 不影响主 Agent
主 Agent 继续正常工作
```

---

## 结果回传

### render_subagent_summary()

```python
def render_subagent_summary(result: SubagentRunResult) -> str:
    parts = []
    if result.success:
        parts.append("子任务完成")
    else:
        parts.append(f"子任务失败（{result.stop_reason}）")

    parts.append(f"结论：{result.output}")

    if result.files_modified:
        parts.append(f"修改的文件：{', '.join(result.files_modified)}")

    return "\n".join(parts)
```

### 为什么结果只是一段文字

```text
为什么不是：
  子 Agent 的完整对话历史？
  子 Agent 的 session_state？
  子 Agent 用了哪些工具？

原因：
  1. 主 Agent 不需要理解子 Agent 的过程，只需要结论
  2. 传递完整历史会污染主 Agent 的上下文
  3. 一段文字是最简洁、最稳定的接口

类比：
  你问实习生"调查结果是什么"
  实习生说"项目有 5 个模块，billing 复杂度最高"
  你不需要知道实习生翻了几个文件夹才得出结论
```

---

## FORK 模式（预留）

```python
class SubagentContextMode(StrEnum):
    FRESH = "fresh"    # 全新上下文
    FORK = "fork"      # 继承主 Agent 的上下文
```

| 模式 | 对话历史 | 适用场景 | 状态 |
|---|---|---|---|
| `FRESH` | 空白 | 独立的探索和分析 | 已实现 |
| `FORK` | 复制主 Agent 的历史 | 在已有上下文中继续 | 未实现 |

FORK 的典型场景：

```text
主 Agent 已经分析了 10 轮，收集了大量上下文

现在需要子 Agent 做一个深入分析
  → 子 Agent 需要看到之前的分析结果
  → 用 FORK 模式：复制对话历史
  → 子 Agent 在已有上下文中继续
```

---

## 常见疑问

### Q: 子 Agent 可以并行运行吗？

A: 当前实现是串行的（`SubagentRuntime.run()` 是同步方法）。要并行需要在上层用线程池或异步。架构上已经做好了隔离，并行是可行的。

### Q: 子 Agent 修改的文件，主 Agent 能看到吗？

A: 能。文件系统的修改是共享的。子 Agent 修改了 `report.md`，主 Agent 可以直接 `read_file("report.md")` 看到修改后的内容。隔离的是**对话历史和内存状态**，不是文件系统。

### Q: 如果子 Agent 超过轮次限制怎么办？

A: 和主 Agent 一样——到达 `max_turns` 后注入收尾消息，给一轮机会给出最终回复。如果仍然不结束，强制退出。返回的 `SubagentRunResult.stop_reason` 会是 `"max_turns"`。

### Q: 子 Agent 的 API 调用和主 Agent 共享 token 额度吗？

A: 不共享。子 Agent 创建了独立的 `ModelGateway` 和 `AnthropicClient`，是独立的 API 连接。

---

## 关键文件索引

| 文件 | 职责 | 行数 |
|---|---|---|
| `core/session/subagent.py` | `SubagentRuntime`, Agent 类型定义, 执行逻辑, 结果渲染 | ~230 行 |
| `core/tools/__init__.py` | `ToolRegistry.filtered()` — 创建受限的工具注册表 | ~15 行 |
| `core/session/engine.py` | `SessionEngine` — 子 Agent 也用它运行查询循环 | 被 subagent 调用 |

---

## 一句话记住

**Subagent 让主 Agent 把任务委派给专门的"小助手"：EXPLORE 只能读、PLAN 只能分析、GENERAL 可以执行。每个子 Agent 在完全隔离的环境中运行（独立的对话历史、受限的工具、自己的轮次限制），完成后把结论返回给主 Agent。**
