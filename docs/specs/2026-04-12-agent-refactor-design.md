# Agent 模块重构设计

## 问题

`core/agent.py` 有 705 行，承担 6 个不同职责：LLM 调用、显示渲染、上下文注入、任务规划、Todo 集成、循环控制。职责混杂导致：
- 难以单独测试某一层逻辑
- 添加新功能（如新的上下文类型、新的显示方式）必须修改 agent.py
- 全局状态（`console` 实例、client 传参）散落各处

## 方案

用 `typing.Protocol` 定义 4 个插件接口，将 agent.py 的 6 个职责拆分为 5 个独立模块。`AgentLoop` 类通过组合持有各接口实例，实现可替换、可测试、可扩展。

依赖关系单向：`interfaces.py` → 实现模块 → `agent.py`，无循环依赖。

```
interfaces.py（Protocol 定义，无依赖）
     ↑
llm_client.py  ← interfaces, config
renderer.py    ← interfaces, todo（类型提示）
planner.py     ← interfaces, todo
context.py     ← interfaces, todo
     ↑
agent.py       ← interfaces, llm_client, renderer, planner, context, runtime, tools
```

## 文件结构

```
core/
  interfaces.py     ← 4 个 Protocol（~60 行，新增）
  llm_client.py     ← LLMResponse + OpenAIClient（~155 行，从 agent.py 搬出）
  renderer.py       ← RichRenderer（~100 行，从 agent.py 搬出）
  planner.py        ← DefaultPlanner + parse 函数（~100 行，从 agent.py 搬出）
  context.py        ← ContextPipeline + 2 个 plugin（~120 行，扩展现有 80 行）
  agent.py          ← AgentLoop + TodoContextPlugin + 循环控制（~360 行，原 705 行）
  todo.py           ← 不变
  protocol.py       ← 不变
  runtime.py        ← 不变
  config.py         ← 不变
  tools/            ← 不变
```

## Protocol 接口

### LLMClient

```python
class LLMClient(Protocol):
    def call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Any: ...
```

`call` 返回 `Any` 而非 `LLMResponse` — 因为 `LLMResponse` 定义在 `llm_client.py`，interfaces 不依赖实现模块，避免循环引用。调用者 import `LLMResponse` 自行使用。

### ContextPlugin

```python
class ContextPlugin(Protocol):
    def inject(self, messages: list[dict[str, Any]]) -> None: ...
```

每个插件自行保证幂等性（通过 HTML comment marker 检查）。`ContextPipeline` 按注册顺序调用所有插件。

实现：
- `SystemContextPlugin` — 注入系统提示词（原 `_inject_system_context`）
- `UserContextPlugin` — 注入环境信息（原 `_inject_user_context`）
- `TodoContextPlugin` — 注入 todo 进度（原 `_inject_todo_context`，定义在 agent.py 内，因为与循环状态耦合）

### Renderer

```python
class Renderer(Protocol):
    def show_thinking(self, title: str, reasoning: str) -> None: ...
    def show_assistant(self, content: str) -> None: ...
    def show_timing(self, elapsed: float, prompt_tokens: int, completion_tokens: int, finish_reason: str) -> None: ...
    def show_current_todo(self, item: TodoItem, completed: int, total: int) -> None: ...
    def show_progress(self, items: list[TodoItem]) -> None: ...
    def show_completion_summary(self, completed: int, total: int, elapsed: float) -> None: ...
    def show_tool_call(self, name: str, args: dict) -> None: ...
    def show_tool_result(self, name: str, output: str) -> None: ...
    def show_error(self, message: str) -> None: ...
    def show_status(self, message: str) -> None: ...
```

10 个方法，每个对应一种显示场景。`RichRenderer` 实现基于 `rich.Console`，从 agent.py 中提取所有 `console.print` / `print` 调用。

### Planner

```python
class Planner(Protocol):
    def plan(self, messages: list[dict[str, Any]], client: LLMClient) -> TodoManager | None: ...
```

`plan` 接收 `client` 参数而非自己创建 — 方便测试时 mock。`DefaultPlanner` 内部使用 `_PLAN_PROMPT` 和 `_parse_plan_response`。

## AgentLoop 编排器

```python
class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        renderer: Renderer,
        planner: Planner,
        context: ContextPipeline,
        tools_schema: list[dict] | None = None,
    ):
        self._llm = llm
        self._renderer = renderer
        self._planner = planner
        self._context = context
        self._tools_schema = tools_schema or registry.schemas()

    def run(self, messages: list[dict], todo_manager: TodoManager | None = None) -> None:
        """入口，等价于原 agent_loop()。"""
        ...

    def _execute_tool_turn(self, llm_resp, messages, tool_ctx) -> ...: ...
    def _ensure_final_response(self, llm_resp, messages) -> ...: ...
    def _run_tool_loop(self, llm_resp, messages, tool_ctx, todo_manager) -> ...: ...
```

### 向后兼容

保留一个模块级函数作为快捷入口：

```python
def agent_loop(messages, todo_manager=None):
    """向后兼容入口。内部装配默认依赖。"""
    llm = OpenAIClient()
    renderer = RichRenderer()
    planner = DefaultPlanner(llm, renderer)
    context = ContextPipeline()
    context.register(SystemContextPlugin())
    context.register(UserContextPlugin())
    AgentLoop(llm, renderer, planner, context).run(messages, todo_manager)
```

## 实现细节

### llm_client.py

搬出内容：
- `LLMResponse` 类（原 agent.py L27-91）
- `_call_llm` → `OpenAIClient.call` 方法（client 绑在实例上，不再作为参数传入）
- `_parse_tool_args` 函数（LLM 层的工具函数）

`OpenAIClient.__init__` 内部调用 `create_llm_client()` 创建 OpenAI SDK client。计时线程逻辑不变。

### renderer.py

`RichRenderer` 持有 `Console` 实例，实现 Renderer 的 10 个方法。所有 `console.print` / `print` 调用从 agent.py 消除。

### planner.py

- 模块常量 `_PLAN_PROMPT`（原 agent.py L235-252）
- 模块函数 `_parse_plan_response`（原 agent.py L255-285）
- `DefaultPlanner` 类（原 `plan_phase` 函数的面向对象封装），规划思考通过 `renderer.show_thinking()` 展示

### context.py 扩展

保留现有 `get_system_context()`、`get_user_context()` 函数。新增：
- `ContextPipeline` 类 — `register(plugin)` + `inject_all(messages)`
- `SystemContextPlugin` — 包装原 `_inject_system_context`
- `UserContextPlugin` — 包装原 `_inject_user_context`

### agent.py 剩余内容

| 内容 | 行数 | 说明 |
|------|------|------|
| imports + 常量 | ~20 | 缩减 |
| `TodoContextPlugin` | ~30 | 原 `_inject_todo_context` |
| `AgentLoop._execute_tool_turn` | ~80 | 改用 `self._renderer` / `self._llm` |
| `AgentLoop._ensure_final_response` | ~60 | 同上 |
| `AgentLoop._run_tool_loop` | ~120 | todo 跟踪逻辑不变 |
| `AgentLoop.run` | ~50 | 原入口函数逻辑 |
| 合计 | ~360 | 原 705 行 |

## 入口变更

`01_agent_loop.py` 从隐式调用改为显式装配：

```python
# 装配（循环外一次）
renderer = RichRenderer()
llm = OpenAIClient()
planner = DefaultPlanner(llm, renderer)

# 每轮对话
context = ContextPipeline()
context.register(SystemContextPlugin())
context.register(UserContextPlugin())
if todos:
    context.register(TodoContextPlugin(todos))
AgentLoop(llm, renderer, planner, context).run(history, todo_manager=todos)
```

## 迁移路径

4 步，每步独立可验证：

1. **新增 `interfaces.py`** — 纯新增，零影响
2. **抽出 `llm_client.py`** — 搬出 `LLMResponse` + `OpenAIClient`，agent.py 改为 import
3. **抽出 `renderer.py` + `planner.py` + `context.py` 扩展** — 同步搬出，agent.py 内部改用实例方法
4. **改造 `agent.py` 为类 + 更新入口文件** — 运行端到端验证

每步完成后 `pytest tests/` 确认无回归。

## 不变的部分

- `core/todo.py` — 数据模型
- `core/protocol.py` — 消息规范化
- `core/runtime.py` — 工具执行引擎
- `core/tools/` — 所有工具实现
- `core/config.py` — 环境变量配置
- 现有测试用例（仅调整 import 路径）

## 设计决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 接口风格 | Protocol 而非 ABC | 不要求继承，松耦合，Pythonic |
| LLMClient.call 返回类型 | Any 而非 LLMResponse | 避免 interfaces 依赖实现模块 |
| TodoContextPlugin 位置 | agent.py 而非 context.py | 与循环状态紧密耦合 |
| ContextPipeline 每轮重建 | 是 | todo plugin 每轮不同，pipeline 需要反映当前状态 |
| 向后兼容 | 保留 agent_loop() 函数 | 降低迁移风险 |
