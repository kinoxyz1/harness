# Todo 系统设计

## 问题

长任务在三个维度上质量下降：

- **目标偏移**：随着 tool call 填满上下文窗口，原始用户意图被稀释
- **步骤遗漏**：没有完整的"待办清单"——LLM 依赖短期记忆行动
- **重复劳动**：没有已完成事项的记录

根本原因：随着对话增长，原始用户目标在总上下文中的占比越来越小。LLM 逐渐忘记自己要做什么。

## 方案概述

新增一个 **todo 系统** —— 一个持久化的进度锚点，每一轮 loop 迭代都能看到。LLM 始终知道"我要去哪、我到哪了、还剩什么"。

两阶段 agent loop：

```
用户消息 → plan_phase() → TodoManager
                ↓
         agent_loop(messages, todo_manager=todos)
         每轮: 注入 todo 上下文 → LLM 执行 → 更新状态
                ↓
         所有 todo 完成 → 汇总回复用户
```

## 数据模型

单层扁平列表，无子任务嵌套。

```python
class TodoStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class TodoItem:
    id: int                    # 自增序号，从 1 开始
    content: str               # 待办描述（如"读取项目配置文件"）
    status: TodoStatus = TodoStatus.PENDING

@dataclass
class TodoManager:
    """Todo 管理器：管理待办列表的完整生命周期。"""
    items: list[TodoItem] = field(default_factory=list)
    _next_id: int = 1

    def add(self, content: str, status=TodoStatus.PENDING) -> TodoItem:
        """添加新的待办项。"""
        item = TodoItem(id=self._next_id, content=content, status=status)
        self.items.append(item)
        self._next_id += 1
        return item

    def update_status(self, id: int, status: TodoStatus) -> TodoItem | None:
        """按 id 更新指定项的状态。"""
        for item in self.items:
            if item.id == id:
                item.status = status
                return item
        return None

    def remove(self, id: int) -> None:
        """按 id 移除一项。"""
        self.items = [item for item in self.items if item.id != id]

    def reorder(self, ids: list[int]) -> None:
        """重排。ids 为新的 id 顺序。"""
        id_to_item = {item.id: item for item in self.items}
        self.items = [id_to_item[i] for i in ids if i in id_to_item]

    def current(self) -> TodoItem | None:
        """返回第一个未完成的项（当前聚焦）。"""
        for item in self.items:
            if item.status != TodoStatus.COMPLETED:
                return item
        return None

    def progress(self) -> tuple[int, int]:
        """返回 (已完成数, 总数)。"""
        completed = sum(1 for item in self.items if item.status == TodoStatus.COMPLETED)
        return (completed, len(self.items))

    def summary(self) -> str:
        """格式化为可注入 LLM 上下文的文本块。"""
        completed, total = self.progress()
        lines = [f"## 任务进度 ({completed}/{total} 已完成)\n"]
        for item in self.items:
            mark = "x" if item.status == TodoStatus.COMPLETED else " "
            lines.append(f"- [{mark}] {item.id}. {item.content}")
        current_item = self.current()
        if current_item:
            lines.append(f"\n当前聚焦: #{current_item.id} {current_item.content}")
        return "\n".join(lines)
```

## 控制流

### 阶段一：规划 (`plan_phase`)

一次独立的 LLM 调用，产出结构化的 todo 列表。此调用不携带 tools schema。

```python
def plan_phase(messages: list[dict], client) -> TodoManager | None:
    """规划阶段：LLM 分析任务并产出 TodoManager。

    Returns:
        TodoManager — 如果任务需要多步执行。
        None — 如果任务足够简单，可以直接处理。
    """
```

**规划 prompt**（plan_phase 的独立 system message，不混入主 system prompt）：

```
分析用户的请求，判断是否需要多步执行。

如果需要多步执行，按以下格式输出你的计划：
PLAN:
1. [描述]
2. [描述]
...

如果你可以直接回答而无需工具使用，输出：
DIRECT: [你的回答]
```

**解析逻辑** (`_parse_plan_response`)：
- 回复以 `PLAN:` 开头 → 提取编号项 → 创建 `TodoManager`
- 回复以 `DIRECT:` 开头 → 返回 `None`（跳过执行阶段，直接回复用户）
- 模糊情况 → 按 `DIRECT` 处理（安全降级到简单路径）

### 阶段二：执行 (`agent_loop` with todo)

现有 `agent_loop` 新增 `todo_manager` 参数：

```python
def agent_loop(messages: list[dict], todo_manager: TodoManager | None = None) -> None:
```

当 `todo_manager` 不为 `None` 时：
1. 每轮 `_call_llm` 调用前：通过 `_inject_todo_context` 注入 todo 摘要
2. 每轮 `_call_llm` 调用前：检查 `rounds_since_update`，超过阈值时注入提醒
3. 每轮迭代后：解析 LLM 回复中的 `[TODO-UPDATE: ...]` 标记
4. 所有 todo 完成后：自然退出循环

简单任务（`plan_phase` 返回 `None`）走现有 `agent_loop` 路径，不变。

## 上下文注入

### Todo 上下文

通过新的 `_inject_todo_context` 函数注入。与 `_inject_user_context`（使用 marker 幂等性保证跨 REPL 轮次不重复注入）不同，todo 上下文必须**每轮刷新**以反映最新进度：

```python
_TODO_MARKER = "<!-- todo-context -->"

def _inject_todo_context(messages: list[dict], todo_manager: TodoManager) -> None:
    """注入当前 todo 进度。先移除旧的再注入新的。"""
    # 移除上一轮的 todo 上下文（最多一条）
    messages[:] = [
        msg for msg in messages
        if not (msg.get("role") == "user" and _TODO_MARKER in (msg.get("content") or ""))
    ]

    todo_text = todo_manager.summary()
    content = f"{_TODO_MARKER}\n{todo_text}"
    messages.append({"role": "user", "content": content})
```

**注入格式**（LLM 看到的内容）：

```
<!-- todo-context -->
## 任务进度 (2/5 已完成)

- [x] 1. 读取项目配置文件
- [x] 2. 分析现有代码结构
- [ ] 3. 实现数据解析模块
- [ ] 4. 编写单元测试
- [ ] 5. 更新文档

当前聚焦: #3 实现数据解析模块
```

### 提醒注入

当 `rounds_since_update >= 3` 时，在 LLM 调用前注入提醒：

```python
if rounds_since_update >= 3:
    messages.append({
        "role": "user",
        "content": "<reminder>重新评估你的计划，更新进度后再继续。</reminder>",
    })
```

这是被动提醒——LLM 自然地重新审视进度，并输出状态更新标记。

## 状态自动更新

### 轻量标记

在规划 prompt 中指示 LLM：完成一个 todo 时在回复中包含标记：

```
当你完成一项任务时，在回复中包含：
[TODO-UPDATE: status=completed, id=N]
```

### 解析

```python
import re

TODO_UPDATE_PATTERN = re.compile(
    r'\[TODO-UPDATE:\s*status=(\w+),\s*id=(\d+)\]'
)

def parse_todo_updates(text: str) -> list[tuple[str, int]]:
    """解析 [TODO-UPDATE: status=X, id=N] 标记。

    返回 (status, id) 元组列表。
    """
    matches = TODO_UPDATE_PATTERN.findall(text)
    return [(status, int(id_str)) for status, id_str in matches]
```

### 更新流程

在 tool loop 中，每次 LLM 回复后：

1. 从回复中提取文本内容
2. 对文本运行 `parse_todo_updates`
3. 对每个匹配项，调用 `todo_manager.update_status(id, status)`
4. 如果有任何更新发生，重置 `rounds_since_update = 0`
5. 否则，`rounds_since_update += 1`

标记在展示给用户之前从回复文本中剥离。

## 集成点

### 文件变更

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `core/todo.py` | **新增** | `TodoManager`, `TodoItem`, `TodoStatus`, `parse_todo_updates()`（约 80 行） |
| `core/context.py` | **新增** `get_todo_context()` | 将 todo 摘要格式化为可注入文本 |
| `core/agent.py` | **新增** `plan_phase()` | 规划阶段：prompt + 解析（约 60 行） |
| `core/agent.py` | **修改** `agent_loop()` | 接受 `todo_manager` 参数；注入上下文；跟踪轮次；解析标记 |
| `core/agent.py` | **新增** `_inject_todo_context()` | 每轮 LLM 调用前注入 todo 摘要 |
| `core/agent.py` | **新增** `_inject_reminder()` | 轮次超阈值时注入提醒 |

不涉及的文件：`protocol.py`、`runtime.py`、`tools/`、`config.py`。

### 入口变更

`01_agent_loop.py` 需要在 `agent_loop` 之前调用 `plan_phase`：

```python
# 修改前
agent_loop(history)

# 修改后
todos = plan_phase(history, client)
if todos:
    agent_loop(history, todo_manager=todos)
else:
    agent_loop(history)
```

## 后续扩展（V1 不做）

- **Todo tool**：让 LLM 通过 `todo_manage` 工具显式执行增删重排操作
- **子任务**：todo 项内的嵌套任务分解
- **进度持久化**：跨会话保存 todo 状态
- **智能截断**：上下文窗口压力大时，摘要已完成项以节省 token

## 设计决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 层级结构 | 单层（无子任务） | YAGNI — 一层就能解决核心问题 |
| 注入方式 | user context（V1），tool（后续） | 最小改动，复用已有模式 |
| 规划时机 | 独立的前置调用 | 规划与执行职责清晰分离 |
| 动态调整 | 仅状态更新（V1），完整 CRUD（后续） | 先验证核心假设 |
| 完成检测 | 轻量标记 + 被动提醒 | 可靠、低耦合 |
| 提醒阈值 | 3 轮 | 足够工作空间，又不至于长时间无反馈 |
