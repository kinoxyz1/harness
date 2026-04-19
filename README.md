# Harness — 极简 AI Agent 框架

受 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 启发的终端 AI Agent 框架。
用最少的代码实现一个能读写文件、执行命令、调用 Skills、运行子代理的编码助手。

**适合谁：** 想理解 AI Agent 工作原理的开发者。核心代码约 4000 行，所有逻辑一目了然。

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
⚡ Read(test/test.txt)       → 读取文件内容
⚡ Bash(python solve.py)     → 编写并运行解题脚本
✅ 完成题目
✅ 清理临时文件
```

## 核心架构

```
用户输入 → SessionEngine
              ├── PromptAssembler  → 系统提示词 + Skills 目录
              ├── QueryLoop        → 思考-行动循环
              │     ├── ModelGateway      → LLM API 调用
              │     ├── ToolExecutorRuntime → 工具并行/串行调度
              │     ├── PolicyRunner      → 轮次限制 + 计划追踪
              │     └── RecoveryManager   → 空/截断响应恢复
              ├── SessionStore     → 对话历史
              └── SkillRegistry    → Skills 发现与加载
```

**工作流程：** 用户输入 → Prompt 组装 → LLM 思考 → 需要工具？→ 执行工具 → 结果送回 LLM → 继续思考或输出回答。

## 功能特性

### 内置工具

Agent 收到指令后，自主决定是否使用工具。简单问答直接回答，复杂任务通过工具逐步执行。

| 工具 | 作用 | 读写 |
|------|------|------|
| `read_file` | 读取文件内容（带行号，支持分段） | 只读 |
| `write_file` | 写入或追加文件 | 写 |
| `edit_file` | 精确替换文件中的文本 | 写 |
| `find` | 按 Glob 模式搜索文件 | 只读 |
| `bash` | 执行终端命令（带超时和安全检查） | 写 |
| `todo` | 管理多步骤任务的计划 | 写 |
| `skill` | 内联加载 Skill 知识包 | 写 |

**执行调度：** 只读工具并行执行（ThreadPoolExecutor），写工具串行执行。工具输出超过 30000 字符自动截断。

### Skills 系统

Skill 是模块化的知识包，放在 `.harness/skills/` 下，扩展 Agent 的专业能力。

- **使用方式：** 终端输入 `/skills list` 查看目录，`/skills use <id>` 加载，或 Agent 自主通过 `skill` 工具调用
- **创建 Skill：** 在 `.harness/skills/` 下新建目录，放入 `SKILL.md`（YAML 前置元数据 + Markdown 正文），可选加入 `references/` 参考文件
- **内联注入：** Skill 内容作为系统消息注入对话，24K 字符预算

### 子代理（Subagent）

三种预置子代理，各自在隔离会话中运行：

| 类型 | 工具权限 | 最大轮次 | 用途 |
|------|----------|----------|------|
| EXPLORE | find, read_file, todo | 10 | 只读探索代码库 |
| PLAN | find, read_file, todo | 12 | 规划实现方案 |
| GENERAL | 全部工具（不含子代理） | 20 | 通用任务执行 |

### 策略系统

- **MaxTurnsPolicy** — 工具调用轮次上限，防止无限循环
- **TodoPlanningPolicy** — 监控计划新鲜度，连续 4 轮未更新时提醒 Agent 刷新计划

## 项目结构

```
harness/
├── 01_agent_loop.py           # 入口：启动交互式 REPL
├── core/
│   ├── llm/                   # LLM 客户端
│   │   ├── anthropic_client.py  #   Anthropic API 封装（含 thinking 支持）
│   │   ├── client.py            #   ModelGateway 门面
│   │   ├── factory.py           #   客户端工厂
│   │   ├── protocol.py          #   消息格式规范化
│   │   └── response.py          #   响应数据类
│   ├── policy/                # 执行策略
│   │   ├── base.py              #   RunPolicy 协议
│   │   ├── max_turns.py         #   轮次限制
│   │   └── todo_tracking.py     #   计划追踪
│   ├── prompt/                # 提示词组装
│   │   ├── assembler.py         #   系统提示词构建 + 缓存
│   │   ├── cache.py             #   提示词缓存
│   │   ├── context.py           #   提示词上下文
│   │   └── system_context.py    #   三层提示词（框架/用户/环境）
│   ├── query/                 # 查询循环
│   │   ├── loop.py              #   核心：思考-行动循环
│   │   ├── recovery.py          #   空/截断响应恢复
│   │   ├── result.py            #   查询结果
│   │   └── state.py             #   运行状态
│   ├── session/               # 会话管理
│   │   ├── commands.py          #   /skills 命令处理
│   │   ├── engine.py            #   SessionEngine 编排器
│   │   ├── state.py             #   会话状态
│   │   ├── store.py             #   消息存储
│   │   ├── subagent.py          #   子代理运行时
│   │   └── view_builder.py      #   消息视图构建
│   ├── shared/                # 共享类型
│   │   ├── config.py            #   环境变量配置
│   │   ├── env_loader.py        #   .env 文件加载
│   │   ├── interfaces.py        #   Protocol 定义（LLM/Renderer/Context）
│   │   ├── protocol.py          #   消息协议
│   │   ├── run_options.py       #   运行显示选项
│   │   └── types.py             #   UsageDelta / MessageBatch
│   ├── skills/                # Skills 系统
│   │   ├── models.py            #   Skill 数据模型
│   │   ├── registry.py          #   Skill 发现与加载
│   │   └── runtime.py           #   Skill 内联注入
│   ├── tools/                 # 工具系统
│   │   ├── __init__.py          #   ToolRegistry + 自动发现
│   │   ├── context.py           #   ToolUseContext / ToolResult
│   │   ├── runtime.py           #   ToolExecutorRuntime 调度器
│   │   └── builtin/             #   内置工具（每个文件一个）
│   │       ├── bash.py
│   │       ├── read_file.py
│   │       ├── write_file.py
│   │       ├── edit_file.py
│   │       ├── find.py
│   │       ├── todo.py
│   │       └── skill.py
│   └── ui/                    # 终端渲染
│       └── renderer.py          #   RichRenderer + QuietRenderer
├── tests/                     # 测试
├── docs/                      # 设计文档
│   ├── agent_loop/              #   Agent Loop 设计笔记
│   ├── stage-4-agent-design-intent.md  #   阶段 4 教学型讲解
│   ├── specs/                   #   功能设计文档
│   └── tools/                   #   工具系统设计笔记
└── .harness/                  # 用户定制
    ├── context/                 #   AI 行为定制（identity.md, style.md, rules.md）
    └── skills/                  #   Skills 知识包
```

## 配置

所有配置通过环境变量（`.env` 文件）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | （必填） | API Key |
| `ANTHROPIC_MODEL` | `kimi-k2.5` | 模型 ID |
| `ANTHROPIC_BASE_URL` | （空） | 可选，用于兼容 Anthropic 协议的服务 |
| `LLM_MAX_TOKENS` | `8192` | 单次请求最大输出 token |
| `LLM_ENABLE_THINKING` | `true` | 启用推理模式 |
| `LLM_SHOW_THINKING` | `true` | 显示推理过程 |
| `BASH_TIMEOUT` | `120` | bash 命令超时（秒） |
| `AGENT_MAX_TURNS` | `300` | 工具调用轮次上限 |

### 支持的模型

只要 API 兼容 Anthropic Messages 协议就能用：

| 模型 | Provider | 说明 |
|------|----------|------|
| claude-sonnet-4-6 | Anthropic | 推荐模型 |
| claude-opus-4-6 | Anthropic | 高能力模型 |
| kimi-k2.5 | Moonshot | 默认模型，支持 extended thinking |

切换模型只需修改 `.env` 中的 `ANTHROPIC_MODEL` 和 `ANTHROPIC_BASE_URL`。

### 自定义 AI 行为

编辑 `.harness/context/` 下的文件来定制 Agent：

- `identity.md` — 定义 AI 身份（如"你是一个 Python 专家"）
- `style.md` — 定义沟通风格（如"回答简洁，用代码说话"）
- `rules.md` — 额外规则（如"不要修改配置文件"）

## 扩展指南

### 添加新工具

在 `core/tools/builtin/` 下新建一个 `.py` 文件，框架自动发现并注册。

```python
# core/tools/builtin/my_tool.py
from __future__ import annotations
from typing import Any
from ..context import ToolResult, ToolUseContext

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

就这么简单。框架启动时会自动扫描 `builtin/` 目录并注册所有工具。

### 创建 Skill

在 `.harness/skills/` 下新建目录：

```
.harness/skills/my-skill/
├── SKILL.md          # 必需：YAML 前置元数据 + Markdown 正文
└── references/       # 可选：参考文件
    └── guide.md
```

`SKILL.md` 格式：

```markdown
---
name: my-skill
description: 这个 Skill 做什么
when-to-use: 什么时候应该使用
---

# Skill 正文

这里写具体的指令和知识...
```

## 学习路线

建议按以下顺序阅读源码：

1. **`core/shared/config.py`** — 14 行，了解配置从哪来
2. **`core/shared/interfaces.py`** — Protocol 定义，理解各组件的接口契约
3. **`core/tools/__init__.py`** — ToolRegistry 和工具自动发现机制
4. **`core/tools/context.py`** — ToolResult、ToolUseContext 等核心类型
5. **`core/llm/anthropic_client.py`** — LLM 调用封装和 thinking 支持
6. **`core/query/loop.py`** — 核心！思考-行动循环的完整逻辑
7. **`core/session/engine.py`** — SessionEngine 编排器，看依赖如何组装
8. **`01_agent_loop.py`** — 入口，REPL 启动流程

`docs/` 目录下有详细的设计文档，记录了每个设计决策的原因。

推荐先读这两篇：

- [`docs/stage-4-agent-design-intent.md`](docs/stage-4-agent-design-intent.md) — 面向初学者理解阶段 4：control plane、policy、skills 如何协作
- [`docs/architecture-evolution-deep-dive.md`](docs/architecture-evolution-deep-dive.md) — 从整体演进视角看四个架构阶段如何一步步形成今天的结构

## 运行测试

```bash
python -m pytest tests/ -v
```

## License

MIT
