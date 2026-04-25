# Harness

一个用于学习 Agent 运行时内部机制的终端框架。

代码本身就是教材——每个模块职责收窄到一件事，文件顶部都有模块级注释说明"你在数据流中的位置"。目标是读完这个仓库后，你能回答：

- 一个 Agent 的 think-act 循环到底在循环什么？退出条件有哪些？
- 模型看到的"输入"是怎么从内部状态组装出来的？system 和 messages 为什么分开？
- 工具调用的结果怎么回到模型？为什么不让工具直接改状态？
- 上下文窗口不够时怎么办？transcript 截取和 thinking 清理的策略是什么？

## 核心设计

主流 Agent 把"完整对话历史"直接喂给模型。Harness 的做法不同——维护显式状态，每轮重组视图：

1. **显式状态**（SessionState + RunState）保存运行时真相，不是散落在对话历史里的文本
2. **视图组装器**（MessageViewBuilder）每轮从状态重建模型输入，从状态而非历史拼装
3. **reducer 是唯一状态写入口**——工具只返回结构化 updates，不直接改 QueryLoop 状态

这三个约束把系统从"基于历史消息拼运气"变成"基于状态重组视图"的 Agent runtime。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入 API Key

python 01_agent_loop.py
```

```text
Agent Loop 已启动。输入 exit 或 quit 退出。

>> /skills list
>> 阅读 README.md 并总结
```

## 数据流

```text
用户输入
  → SessionEngine.submit_user_message()
    → QueryLoop.run()
      ┌──────────────────────────────────┐
      │ while True:                       │
      │   maintenance（文件缓存失效检查）  │
      │   policy before_model_call        │
      │   MessageViewBuilder.build()      │
      │   ModelGateway.call_once()        │
      │   有 tool_calls → 执行 → reducer  │
      │   有最终文本   → return            │
      │   空响应       → recovery 重试     │
      └──────────────────────────────────┘
    ← QueryResult
```

退出循环：正常完成 | max_turns 强制收尾 | recovery 失败。

## 项目结构

```text
harness/
├── 01_agent_loop.py          # 入口：REPL + 依赖装配
├── core/
│   ├── llm/                  # API 客户端、协议归一化、响应模型
│   ├── policy/               # 策略框架（max_turns、todo stale）
│   ├── prompt/               # system prompt 组装与缓存
│   ├── query/                # QueryLoop、RunState、reducers、recovery
│   ├── session/              # SessionEngine、SessionState、ViewBuilder
│   ├── skills/               # Skill 注册与加载
│   ├── tools/                # ToolExecutorRuntime + builtin 工具
│   └── ui/                   # RichRenderer
├── docs/
├── tests/
└── .harness/
    ├── context/              # identity.md, style.md, rules.md
    └── skills/               # 用户自定义 skill
```

## 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 空 | API Key |
| `ANTHROPIC_MODEL` | `kimi-k2.5` | 模型名 |
| `ANTHROPIC_BASE_URL` | `https://api.kimi.com/coding/` | 兼容 Anthropic Messages API 的地址 |
| `LLM_MAX_TOKENS` | `8192` | 单次输出 token 上限 |
| `LLM_THINKING_MODE` | `auto` | `auto / enabled / disabled` |
| `AGENT_MAX_TURNS` | `300` | 单次 query 最大工具轮次 |

其余配置见 `core/shared/config.py`。

## 怎么读这个仓库

建议先分两层来读：

- **第一层：核心代码路径** — 直接对照代码，理解一条请求从入口到退出
- **第二层：伴随文档路径** — 补齐“从 0 到 1 做一个 Agent”时最容易缺的桥梁知识

如果你是第一次接触这个仓库，建议先读 [docs/features/00-learning-path.md](docs/features/00-learning-path.md)。

### 第一层：核心代码路径

建议按以下顺序，每个文件都在 200 行以内：

**第一组：主路径** — 理解一条用户输入从头到尾走了什么

1. [`01_agent_loop.py`](01_agent_loop.py) — REPL + 依赖装配（~110 行）
2. [`core/session/engine.py`](core/session/engine.py) — 会话协调者（~155 行）
3. [`core/query/loop.py`](core/query/loop.py) — think-act 主循环（~350 行，核心）

**第二组：模型输入** — 理解模型"看到"的到底是什么

4. [`core/session/view_builder.py`](core/session/view_builder.py) — transcript 截取 + thinking 清理
5. [`core/prompt/assembler.py`](core/prompt/assembler.py) — system prompt 三层组装

**第三组：协议与工具** — 理解数据怎么进出模型

6. [`core/llm/protocol.py`](core/llm/protocol.py) — 内部格式 → Anthropic 格式转换
7. [`core/query/reducers.py`](core/query/reducers.py) — 唯一状态写入口
8. [`core/tools/runtime.py`](core/tools/runtime.py) — 工具分批执行（并行/串行）

### 第二层：伴随文档路径

当你已经能沿着代码路径走一遍以后，建议继续读这 5 篇：

1. [docs/features/08-request-lifecycle-walkthrough.md](docs/features/08-request-lifecycle-walkthrough.md) — 一次真实请求如何跑完整个运行时
2. [docs/features/09-state-assembled-runtime.md](docs/features/09-state-assembled-runtime.md) — 为什么状态才是运行时真相
3. [docs/features/10-anthropic-protocol-boundary.md](docs/features/10-anthropic-protocol-boundary.md) — 内部消息结构和 Anthropic 协议边界
4. [docs/features/11-extension-playbook.md](docs/features/11-extension-playbook.md) — 项目组同事要扩展能力时从哪里动手
5. [docs/features/12-runtime-invariants.md](docs/features/12-runtime-invariants.md) — 哪些运行时约束不能破坏

`docs/features` 现在既包含按组件拆开的功能说明，也包含这组面向入门和扩展的伴随文档。

完整架构文档：[docs/architecture-current-runtime.md](docs/architecture-current-runtime.md)

## License

MIT
