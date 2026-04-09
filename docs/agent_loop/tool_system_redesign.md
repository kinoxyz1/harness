# 工具系统重构设计

> 核心原则：加工具不需要改循环。循环永远不变。

## 1. 三个稳定点

工具系统的设计围绕三个接口边界，每一环的消费方不同：

```
User → LLM → Tool Dispatch → tool_result → 回到 LLM
                  ↑
              这里是分界线
```

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Schema    │     │ Dispatch Map│     │  ToolResult  │
│  (给模型看)  │     │ (给框架路由) │     │ (给loop消费) │
└─────────────┘     └─────────────┘     └─────────────┘
  模型据此决策        name → handler      loop 不关心内部
  调谁、传什么参      一行查表，永不改     只消费统一格式
```

这不是在描述"工具内部长什么样"（input→process→output），而是在描述**工具怎么接入循环才不会破坏循环**。

- **Schema**：给大模型看的契约。模型据此决定调不调、怎么调。工具最懂自己，所以 description 应该由工具自己写，跟着工具走。
- **Dispatch Map**：工具名 → 执行函数的映射表。它不是锦上添花的优化，而是让循环和工具解耦的架构保证。没有它，每加一个工具就要改循环体（if/elif）。
- **ToolResult**：统一的结果封装。loop 只消费这个格式，不关心是哪个工具、内部怎么执行。

## 2. 核心类型

### ToolResult（统一返回格式）

所有 handler 返回同一个 dataclass，替代最初的 loose dict：

```python
@dataclass
class ToolResult:
    output: str           # 工具输出内容（会传回给模型）
    success: bool         # 是否成功
    error: str | None     # 错误类型（可选）
```

相比 loose dict 的优势：IDE 补全、类型检查、防止 key 拼写错误。

### ToolContext（执行上下文）

工具执行时的环境信息，替代最初的空 dict：

```python
@dataclass
class ToolContext:
    working_dir: str = ""     # 当前工作目录
    session_id: str = ""      # 会话标识
```

agent loop 启动时构造，传给每个工具。工具可直接访问 `context.working_dir`，不再需要自己猜测环境状态。未来需要新字段时只需扩展 dataclass。

## 3. 单个工具的完整定义

一个工具是一个 Python 文件，包含三个部分：

```python
# core/tools/bash.py

from . import ToolContext, ToolResult

# ─── Schema（给模型看）──────────────────────────────

SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "在终端执行一条 Shell 命令。返回标准输出和标准错误的合并结果。"
            "命令在子进程中执行，不保留环境变量变更。"
            "超时设置为 {timeout} 秒，长时间运行的命令会被自动终止。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 Shell 命令",
                },
            },
            "required": ["command"],
        },
    },
}

# ─── 元信息（给框架看）───────────────────────────────

READONLY = False

# ─── Handler（执行逻辑）─────────────────────────────

def handle(args: dict, context: ToolContext) -> ToolResult:
    """执行工具，返回 ToolResult。"""
    command = args["command"]
    # ... 安全检查、执行、返回结果
    return ToolResult(output="...", success=True)
```

各属性的职责：

| 属性 | 谁消费 | 职责 |
|------|--------|------|
| `SCHEMA` | 大模型 | 让模型知道工具存在、参数格式、何时调用 |
| `SCHEMA.function.description` | 大模型 | 工具自己写的专业提示词：能力边界、注意事项 |
| `READONLY` | 框架 | 决定能否与其他工具并行执行 |
| `handle(args, context)` | 框架 | 实际执行逻辑 |
| `handle` 的返回值 | agent loop | 统一的 `ToolResult` 格式 |

提示词放在工具定义里而不是 system prompt 里——**工具定义跟着工具走**，加新工具时自动带上专业提示，不用改全局 prompt。

## 4. Dispatch Map（注册表）

```python
class ToolRegistry:
    """工具注册表：自动发现、查询、路由。"""

    def __init__(self):
        self._handlers: dict[str, Callable] = {}
        self._schemas: list[dict] = []
        self._readonly: dict[str, bool] = {}
        self._required_params: dict[str, list[str]] = {}

    def register(self, module):
        name = module.SCHEMA["function"]["name"]
        self._handlers[name] = module.handle
        self._schemas.append(module.SCHEMA)
        self._readonly[name] = getattr(module, "READONLY", False)
        required = module.SCHEMA.get("function", {}).get("parameters", {}).get("required", [])
        self._required_params[name] = required

    def execute(self, name: str, args: dict, context: ToolContext) -> ToolResult:
        handler = self._handlers.get(name)
        if not handler:
            return ToolResult(output=f"Unknown tool '{name}'", success=False, error="not_found")

        # 验证必填参数
        missing = [p for p in self._required_params.get(name, []) if p not in args]
        if missing:
            return ToolResult(
                output=f"Missing required parameters: {', '.join(missing)}",
                success=False, error="missing_params",
            )

        return handler(args, context)

def auto_discover() -> ToolRegistry:
    registry = ToolRegistry()
    tools_dir = pathlib.Path(__file__).parent
    for file in tools_dir.glob("*.py"):
        if file.name.startswith("_"):
            continue
        module = importlib.import_module(f"core.tools.{file.stem}")
        registry.register(module)
    return registry
```

关键保证：

- **参数验证**：注册时从 SCHEMA 提取 `required` 列表，执行前检查缺失参数。LLM 漏传时返回 `ToolResult(error="missing_params")`，不 crash。
- **替代 if/elif**：`registry.execute(name, args, context)` 一行查表，永不改动。之前每加一个工具就要加一个 elif 分支。
- **自动发现**：启动时扫描 `core/tools/*.py`，新增工具 = 新建文件，不改任何现有代码。

## 5. agent loop 的改造

改造前后对比：

```python
# ── 改造前 ──────────────────────────────────────

from .tools import TOOLS, execute_tool

for tool_call in msg.tool_calls:
    command = args["command"]              # ❌ 知道 bash 的参数名
    output = execute_tool("bash", args)     # ❌ 硬编码工具名
    print(output)

# ── 改造后 ──────────────────────────────────────

from .tools import ToolContext, registry

tool_context = ToolContext(working_dir=os.getcwd())

for tool_call in msg.tool_calls:
    name = tool_call.function.name                    # ✅ 从模型返回中取工具名
    result = registry.execute(name, args, tool_context)  # ✅ 查表 + 验证 + 执行
    print(result.output)                               # ✅ 统一格式
```

agent.py 完全不知道具体有哪些工具、工具参数叫什么。

## 6. 当前工具清单

```
core/tools/
├── __init__.py      # ToolRegistry + ToolResult + ToolContext + auto_discover
├── bash.py          # Shell 命令执行
├── read_file.py     # 读取文件内容
├── edit_file.py     # 字符串替换编辑
├── write_file.py    # 完整内容写入
└── find.py          # 按模式搜索文件
```

| 工具 | 类型 | 用途 | 关键设计 |
|------|------|------|---------|
| `bash` | 写入 | Shell 命令 | 黑名单 + 确认列表安全围栏 |
| `read_file` | 只读 | 读取文件 | offset/limit 分段，带行号输出，自动截断 |
| `edit_file` | 写入 | 替换编辑 | 精确匹配，多处匹配时拒绝执行（防误改） |
| `write_file` | 写入 | 写入文件 | 自动创建父目录，返回创建/覆盖 + 行数 |
| `find` | 只读 | 搜索文件 | glob 模式，按修改时间排序，最多 200 条 |

新增工具流程：创建 `core/tools/xxx.py`（定义 SCHEMA + handle，返回 ToolResult）。完成。

## 7. 已知问题与待改进

### handler 异常保护（应优先修复）

当前 `registry.execute()` 中，`handler(args, context)` 没有 try/except 包裹。如果 handler 抛出未预期的异常：

```
assistant 消息包含 3 个 tool_call
  → handler 1 成功，result 1 append
  → handler 2 成功，result 2 append
  → handler 3 崩溃，异常穿透
  → 只有 2 个 result 被加入 messages
  → 下次 API 调用：tool_call 数量 ≠ tool_result 数量 → 400 错误
```

修复方向：在 `registry.execute()` 中用 try/except 包裹 handler 调用，异常时返回 `ToolResult(success=False, error="internal_error")`。

### 消息格式适配（远期）

当前消息链路硬编码了 OpenAI 兼容协议（`tool_call_id`、`role: "tool"`、`finish_reason: "tool_calls"`）。如果换用 Anthropic 等 provider，整条消息链路的格式不同：

| 字段 | OpenAI | Anthropic |
|------|--------|-----------|
| 工具调用 ID | `tool_call_id` | `tool_use_id` |
| 结果角色 | `role: "tool"` | `role: "user"` + `type: "tool_result"` |
| 调用标识 | `finish_reason: "tool_calls"` | `stop_reason: "tool_use"` |

正确做法是在 agent.py 的 API 调用前加一层 normalize，把内部 messages 格式转换为特定 provider 的格式。换 provider 时只改 normalize 函数，不改循环。当前只用百炼平台（OpenAI 兼容），暂不实现。

### 工具 schema 的按需传入（远期）

当前每次 API 调用都传入所有工具的 schema。5 个工具约 800 token，在 200k 窗口中占比 <0.5%，可忽略。等工具数量过 15-20 个、schema 总量过万 token 时，需要根据用户问题筛选相关工具，只传入对应的 schema。

## 8. 实现记录

- [x] **Phase 1：框架搭建** — ToolRegistry + auto_discover + bash 工具迁移 + agent.py 改造
- [x] **Phase 2：类型强化** — ToolResult / ToolContext dataclass + 必填参数验证
- [x] **Phase 3：工具扩展** — read_file / edit_file / write_file / find，零改动验证通过
- [x] **Bug 修复** — 最终响应不再做多余的流式调用，直接使用 while 循环最后一次结果
- [ ] **Phase 4：健壮性** — handler 异常保护

## 参考资料

- Claude Code 源码 `src/Tool.ts` — Tool 接口定义
- Claude Code 源码 `src/tools.ts` — 注册表 + 自动发现
- Claude Code 源码 `src/services/tools/toolExecution.ts` — 执行管线
