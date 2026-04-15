# Harness - 极简 AI Agent 框架

一个受 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 启发的终端 AI Agent 框架。用最少的代码实现一个能读写文件、执行命令、自主规划任务的编码助手。

**适合谁：** 想理解 AI Agent 工作原理的开发者。代码总量约 2000 行，所有核心逻辑一目了然。

## 30 秒上手

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
cp .env.example .env
# 编辑 .env，填入你的 API Key

# 3. 启动
python 01_agent_loop.py
```

启动后进入交互式终端：

```
Agent Loop 已启动。输入 exit 或 quit 退出。

>> 你好
你好！有什么可以帮你的吗？

>> 阅读 test/test.txt 并用 Python 解决问题
⚡ 读取 test/test.txt 文件内容
⚡ 编写 Python 代码解答
✅ 完成题目
✅ 清理临时文件
```

## 它能做什么

Agent 收到你的指令后，会自主决定是否使用工具：

| 工具 | 作用 |
|------|------|
| `read_file` | 读取文件内容（带行号） |
| `write_file` | 写入或追加文件 |
| `edit_file` | 精确替换文件中的文本 |
| `find` | 按模式搜索文件 |
| `bash` | 执行终端命令 |
| `todo` | 管理多步骤任务的计划 |

对于简单的问答（"什么是递归"），Agent 直接回答，不使用工具。对于复杂任务（"重构这个函数并运行测试"），Agent 会用 `todo` 创建计划，逐步执行。

## 项目结构

```
harness/
├── 01_agent_loop.py       # 入口：启动交互式 REPL
├── core/
│   ├── agent.py           # 核心：AgentLoop 编排器
│   ├── config.py          # 配置：从环境变量加载
│   ├── context.py         # 上下文：系统提示词 + 用户定制
│   ├── interfaces.py      # 接口：Protocol 定义（LLM/Renderer/Context）
│   ├── llm_client.py      # LLM：API 调用 + 响应封装
│   ├── renderer.py        # 显示：终端彩色输出
│   ├── runtime.py         # 执行：工具并行/串行调度
│   ├── protocol.py        # 协议：消息规范化
│   ├── todo.py            # 类型：TodoStatus / TodoItem
│   └── tools/             # 工具：每个文件一个工具，自动发现
│       ├── __init__.py    #   ToolRegistry + ToolUseContext
│       ├── bash.py        #   终端命令执行
│       ├── read_file.py   #   文件读取
│       ├── write_file.py  #   文件写入
│       ├── edit_file.py   #   文件编辑
│       ├── find.py        #   文件搜索
│       └── todo.py        #   任务计划管理
├── tests/                 # 测试
├── docs/                  # 设计文档
└── .harness/context/      # 用户定制提示词
    ├── identity.md        #   AI 身份定义
    └── style.md           #   沟通风格
```

## 工作原理

整个框架的核心是一个循环：

```
用户输入 → LLM 思考 → 需要工具？
                         ├─ 是 → 执行工具 → 结果送回 LLM → 继续思考
                         └─ 否 → 输出回答给用户
```

**关键设计：**

- **工具自动发现**：在 `core/tools/` 下新建一个 `.py` 文件，框架自动注册，无需改其他代码
- **并行/串行调度**：只读工具（如 read_file、find）并行执行，写工具（如 write_file、edit_file）串行执行
- **安全兜底**：工具调用轮次上限（默认 300），防止无限循环
- **思考模型兼容**：支持 kimi-k2、deepseek-r1 等带推理过程的模型

## 配置

所有配置通过环境变量（`.env` 文件）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | （必填） | Anthropic API Key |
| `ANTHROPIC_MODEL` | `kimi-k2.5` | 模型 ID |
| `ANTHROPIC_BASE_URL` | （空） | 可选，用于兼容 Anthropic 协议的服务 |
| `LLM_MAX_TOKENS` | `8192` | 单次请求最大输出 token |
| `LLM_ENABLE_THINKING` | `true` | 启用推理模式 |
| `LLM_SHOW_THINKING` | `true` | 显示推理过程 |
| `BASH_TIMEOUT` | `120` | bash 命令超时（秒） |
| `AGENT_MAX_TURNS` | `300` | 工具调用轮次上限 |

### 支持的模型

只要 API 兼容 Anthropic messages 协议就能用。已测试：

| 模型 | Provider | 说明 |
|------|----------|------|
| claude-sonnet-4-6 | Anthropic 官方 | 推荐模型 |
| claude-opus-4-6 | Anthropic 官方 | 高能力模型 |

切换模型只需修改 `.env` 中的 `ANTHROPIC_MODEL` 和 `ANTHROPIC_BASE_URL`。

### 自定义 AI 行为

编辑 `.harness/context/` 下的文件来定制 Agent：

- `identity.md` — 定义 AI 身份（如"你是一个 Python 专家"）
- `style.md` — 定义沟通风格（如"回答简洁，用代码说话"）
- `rules.md` — 额外规则（如"不要修改配置文件"）

## 添加新工具

3 步完成：

**1. 创建工具文件** `core/tools/my_tool.py`：

```python
from typing import Any
from . import ToolResult, ToolUseContext

SCHEMA: dict[str, Any] = {
    "name": "my_tool",
    "description": "这个工具做什么",
    "input_schema": {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "输入参数"},
        },
        "required": ["input"],
    },
}

READONLY = True  # 只读工具可以并行执行

def handle(args: dict[str, Any], context: ToolUseContext) -> ToolResult:
    result = do_something(args["input"])
    return ToolResult(output=result, success=True)
```

**2. 完成。** 框架会自动发现并注册这个工具。

**3. 测试：**

```bash
python -c "from core.tools import registry; print([s['name'] for s in registry.schemas()])"
```

## 运行测试

```bash
python -m pytest tests/ -v
```

## 学习路线

如果你想理解代码，建议按以下顺序阅读：

1. **`core/config.py`** — 13 行，了解配置从哪来
2. **`core/interfaces.py`** — Protocol 定义，理解各组件的接口
3. **`core/tools/__init__.py`** — 工具注册表和执行上下文
4. **`core/llm_client.py`** — LLM 调用封装和响应结构
5. **`core/agent.py`** — 核心！AgentLoop 的编排逻辑
6. **`01_agent_loop.py`** — 入口，看依赖如何组装

`docs/` 目录下有详细的设计文档，记录了每个设计决策的原因和与 Claude Code 的对比。

## License

MIT
