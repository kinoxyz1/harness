# Todo Tool 化重构设计

## 问题

当前 todo 系统采用"框架管理"模式：独立的 plan_phase 调用、标记解析、上下文注入、提醒逻辑。这导致：
- 每次对话多一次 LLM 调用（plan_phase）
- 标记解析不可靠（依赖 LLM 在回复中输出特定格式）
- 框架侧代码复杂（TodoContextPlugin、reminder 阈值、完成检测）
- LLM 的规划和执行被强行分离，不够自然

## 方案

将 todo 从"框架管理的上下文注入"改为"LLM 可调用的工具"。参考 s03_todo_write.py 和 Claude Code 的 TodoWrite 设计：

- 一个 `todo_manage` 工具，LLM 在需要时主动调用
- 每次调用传入完整任务列表（替换旧列表），不是增量更新
- 删除 plan_phase、标记解析、TodoContextPlugin 等过度工程化部分
- LLM 在同一次循环中同时做规划和执行

## 工具定义

新建 `core/tools/todo.py`，遵循项目统一的工具模式（SCHEMA + handle）。

### Schema

```json
{
  "name": "todo_manage",
  "description": "管理当前会话的任务计划。用于多步骤工作时追踪进度。传入完整任务列表来更新计划。",
  "input_schema": {
    "type": "object",
    "properties": {
      "items": {
        "type": "array",
        "description": "完整的任务列表（替换旧列表）",
        "items": {
          "type": "object",
          "properties": {
            "content": {"type": "string", "description": "任务描述"},
            "status": {
              "type": "string",
              "enum": ["pending", "in_progress", "completed", "failed"]
            }
          },
          "required": ["content", "status"]
        }
      }
    },
    "required": ["items"]
  }
}
```

### handle() 行为

```python
def handle(args, context) -> ToolResult:
    items = args["items"]
    # 验证：最多 12 个，最多 1 个 in_progress
    # 替换内部状态
    # 返回渲染后的进度文本
    return ToolResult(output=rendered_text, success=True)
```

验证规则：
- 最多 12 个任务
- 最多 1 个 `in_progress`（保持聚焦）
- 每项必须有 `content` 和有效的 `status`

### READONLY

`READONLY = False` — 虽然不写文件，但修改内部计划状态，应串行执行。

## 状态管理

工具内部维护模块级 `PlanningState` 单例：

```python
@dataclass
class PlanItem:
    content: str
    status: str = "pending"

@dataclass
class PlanningState:
    items: list[PlanItem] = field(default_factory=list)
    rounds_since_update: int = 0

_state = PlanningState()
```

`handle()` 更新 `_state` 并返回渲染文本。`rounds_since_update` 在 `_run_tool_loop` 中递增，工具被调用时重置。

## 进度显示

两层机制：

1. **LLM 看到**：`handle()` 返回的 ToolResult.output 包含渲染后的进度文本（`[x]`/`[>]`/`[ ]` 图标 + 完成计数）
2. **用户看到**：`_run_tool_loop` 检测到 `todo_manage` 被调用后，用 `renderer.show_progress()` 展示彩色面板

实现方式：`_execute_tool_turn` 返回 `(llm_resp, called_tools)` 元组，`_run_tool_loop` 据此决定是否渲染进度。

## 提醒逻辑

在 `_run_tool_loop` 中：如果连续 N 轮（阈值 3）没调用 `todo_manage`，注入提醒消息。计数由 `_state.rounds_since_update` 追踪，在 `_run_tool_loop` 中每轮递增，工具被调用时重置为 0。

## 删除清单

### 删除整个文件
- `core/planner.py` — DefaultPlanner、_parse_plan_response、_PLAN_PROMPT

### 从 core/todo.py 删除
- `TodoManager` 类
- `parse_todo_updates()` 函数
- `strip_todo_markers()` 函数
- `TODO_UPDATE_PATTERN` 正则
- 保留 `TodoStatus` 枚举和 `TodoItem` 数据类（工具内部使用）

### 从 core/agent.py 删除
- `TodoContextPlugin` 类
- `_REMINDER_THRESHOLD` 常量
- `_run_tool_loop` 中的所有 todo 跟踪：标记解析、进度显示、上下文注入、提醒、完成检测
- `plan_phase()` 向后兼容函数
- `DefaultPlanner` import
- `agent_loop()` 中对 DefaultPlanner 的使用
- `AgentLoop.__init__` 的 `planner` 参数

### 从 core/interfaces.py 删除
- `Planner` Protocol

### 从 01_agent_loop.py 删除
- `DefaultPlanner` import 和实例化
- `planner.plan(history, llm)` 调用
- `TodoContextPlugin` import 和注册
- 计划显示逻辑
- `todo_manager` 参数传递

## 变化后的架构

### AgentLoop 简化

`_run_tool_loop` 从 ~130 行缩减到 ~60 行，只剩循环控制 + todo 进度渲染：

```
while True:
    if is_tool_call:
        llm_resp, called_tools = execute_tool_turn(...)
        if "todo_manage" in called_tools:
            renderer.show_progress(state.items)
            _state.rounds_since_update = 0
        else:
            _state.rounds_since_update += 1
        if _state.rounds_since_update >= 3:
            注入提醒
        continue
    if has_content:
        break
    空内容 → 重试
```

`AgentLoop.__init__` 签名变为：

```python
def __init__(self, llm, renderer, context, tools_schema=None):
```

不再有 `planner` 参数。`run()` 不再接收 `todo_manager` 参数。

### 入口简化

```python
def main():
    renderer = RichRenderer(console)
    llm = OpenAIClient()

    while True:
        query = input(">> ")
        history.append({"role": "user", "content": query})

        context = ContextPipeline()
        context.register(SystemContextPlugin())
        context.register(UserContextPlugin())

        AgentLoop(llm, renderer, context).run(history)
```

没有 planner，没有 todos，没有 TodoContextPlugin。LLM 自己决定什么时候调 todo_manage。

## 文件变化总结

| 文件 | 操作 |
|------|------|
| `core/tools/todo.py` | 新增 |
| `core/planner.py` | 删除 |
| `core/todo.py` | 精简（删除 TodoManager 和标记解析） |
| `core/agent.py` | 精简（删除 todo 跟踪 + planner） |
| `core/interfaces.py` | 精简（删除 Planner Protocol） |
| `01_agent_loop.py` | 精简（删除规划流程） |
| `core/context.py` | 不变 |
| `core/llm_client.py` | 不变 |
| `core/renderer.py` | 不变 |

## 设计决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 工具粒度 | 单一 todo_manage | LLM 传完整列表，比多工具简单 |
| 状态管理 | 模块级单例 | 和 s03 一致，工具内部持有，外部不感知 |
| 进度显示 | 两层（LLM 看文本 + 用户看 Rich） | LLM 需要结构化反馈，用户需要美观展示 |
| plan_phase | 删除 | 省一次 API 调用，LLM 自然规划 |
| 标记解析 | 删除 | 工具调用比文本标记可靠 |
| 提醒逻辑 | 保留但简化 | 计数移到工具内部，agent.py 只做阈值检查 |
