# Anthropic 协议迁移设计

> 日期: 2026-04-15
> 状态: 待审阅
> 参考:
> - [`/Users/kino/works/opensource/learn-claude-code/agents/s01_agent_loop.py`](/Users/kino/works/opensource/learn-claude-code/agents/s01_agent_loop.py)

## 背景

当前项目的模型接入层仍以“百炼 + OpenAI 兼容 chat completions”为中心：

- [`core/shared/config.py`](/Users/kino/works/kino/harness/core/shared/config.py) 使用 `DASHSCOPE_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`
- [`core/llm/factory.py`](/Users/kino/works/kino/harness/core/llm/factory.py) 直接构造 `openai.OpenAI`
- [`core/llm/openai_client.py`](/Users/kino/works/kino/harness/core/llm/openai_client.py) 假设响应结构为 OpenAI `chat.completions`
- [`core/llm/protocol.py`](/Users/kino/works/kino/harness/core/llm/protocol.py) 只规范化 OpenAI 风格的 `tool_calls` / `tool` 消息

这与目标不一致。目标是直接切换到 Anthropic `messages` 协议，并允许通过 `ANTHROPIC_BASE_URL` 访问兼容 Anthropic 协议的服务，例如 Anthropic 官方、Kimi、GLM 的 coding 模型接口。

## 目标

- 直接切换到 Anthropic `messages` 协议，不保留双栈切换逻辑。
- 只要求后端兼容 Anthropic `messages` + `tool_use` / `tool_result` 协议即可运行。
- 配置层切换为：
  - `ANTHROPIC_API_KEY`
  - `ANTHROPIC_BASE_URL`
  - `MODEL_ID`
  - 以及保留现有运行参数，如 `LLM_MAX_TOKENS`、`LLM_ENABLE_THINKING`、`LLM_SHOW_THINKING`
- 上层 `QueryLoop` / `SessionEngine` / `ModelGateway` 继续消费统一的内部响应结构，不直接依赖 Anthropic SDK 细节。
- README、依赖、示例环境变量同步更新。

## 非目标

- 不保留百炼 / OpenAI 协议兼容分支。
- 不在本次迁移中引入 streaming。
- 不借本次迁移顺手重写重试、限流或错误恢复策略。
- 不重构工具运行时本身，只做协议适配所需的最小边界调整。

## 核心原则

1. 协议差异只在 `core/llm/` 和消息规范化边界吸收。
2. 上层循环继续使用统一的 `content`、`tool_calls`、`finish_reason`、token usage。
3. 工具调用和工具回写必须使用 Anthropic 原生语义，而不是“看起来像 Anthropic”的 OpenAI 包装。
4. `LLM_ENABLE_THINKING` 与 `LLM_SHOW_THINKING` 保留，但不把是否支持 thinking 当成主流程前提。

## 配置与依赖

## 新环境变量

- `ANTHROPIC_API_KEY`: 必填
- `MODEL_ID`: 必填
- `ANTHROPIC_BASE_URL`: 可选；为空时走 Anthropic 官方默认地址，非空时访问兼容 Anthropic 协议的服务
- `LLM_MAX_TOKENS`: 保留
- `LLM_ENABLE_THINKING`: 保留
- `LLM_SHOW_THINKING`: 保留
- `BASH_TIMEOUT`、`AGENT_MAX_TURNS`、`MAX_OUTPUT_CHARS`: 保留

## 代码变更

- [`core/shared/config.py`](/Users/kino/works/kino/harness/core/shared/config.py)
  - 删除 `DASHSCOPE_API_KEY`、`LLM_MODEL`、默认 DashScope URL
  - 改为读取 `ANTHROPIC_API_KEY`、`MODEL_ID`、`ANTHROPIC_BASE_URL`
- [`core/llm/factory.py`](/Users/kino/works/kino/harness/core/llm/factory.py)
  - 从构造 `openai.OpenAI` 改为构造 `anthropic.Anthropic`
  - `base_url` 仅在配置存在时传入
- [`requirements.txt`](/Users/kino/works/kino/harness/requirements.txt)
  - 移除 `openai`
  - 增加 `anthropic`
- [`README.md`](/Users/kino/works/kino/harness/README.md)、[`/Users/kino/works/kino/harness/.env.example`](/Users/kino/works/kino/harness/.env.example)
  - 统一改写为 Anthropic 协议语义

## 消息与协议模型

## 输入消息

Anthropic `messages` 协议下：

- `system` 独立作为顶层参数，不放入普通消息列表
- `messages` 仅包含 `user` / `assistant`
- `content` 既可能是字符串，也可能是 block 列表

因此当前“内部消息列表直接清洗后送 API”的做法需要调整为两步：

1. 内部消息仍允许保持现有近似通用结构，方便 `QueryLoop`、`Store`、`Policy` 继续工作
2. 在 [`core/llm/protocol.py`](/Users/kino/works/kino/harness/core/llm/protocol.py) 中新增 Anthropic 专用转换，把内部消息拆成：
   - `system: str`
   - `messages: list[dict[str, Any]]`

对应地，[`core/llm/client.py`](/Users/kino/works/kino/harness/core/llm/client.py) 的 `ModelGateway.call_once()` 不应再保留未生效的 `prompt` 参数，而应显式消费：

- `system`
- `messages`
- `tools`

这样“稳定系统提示词”和“普通对话消息”在接口层有清晰边界，不会再次出现 prompt 构建了但调用层静默忽略的问题。

## 工具声明

当前工具 schema 形状为：

```python
{
    "type": "function",
    "function": {
        "name": "...",
        "description": "...",
        "parameters": {...},
    },
}
```

Anthropic 需要：

```python
{
    "name": "...",
    "description": "...",
    "input_schema": {...},
}
```

推荐直接把 [`core/tools/__init__.py`](/Users/kino/works/kino/harness/core/tools/__init__.py) 的注册表输出切到 Anthropic 形状，而不是在 client 层做二次转换。原因：

- 工具协议是当前唯一目标协议，不需要保留旧形状
- `registry.schemas()` 是 API 出口，直接返回目标协议更清晰
- 可以尽早发现依赖 `schema["function"]["name"]` 的位置并一并修正

对应影响点包括：

- 工具注册时的名称提取
- `filtered()` 中的 schema 名称读取
- subagent 代码中基于 schema 名称构建 allowed set 的位置

## 模型输出归一化

Anthropic 响应中的 block 需要在 [`core/llm/openai_client.py`](/Users/kino/works/kino/harness/core/llm/openai_client.py) 对应的 Anthropic client 中统一归一为内部 `LLMResponse` 语义：

- `content`
  - 收集所有 text block，按顺序拼接
- `tool_calls`
  - 收集所有 `tool_use` block
  - 归一为现有 runtime 易消费的结构，至少包含 `id`、`name`、`input`
- `finish_reason`
  - `tool_use` 映射为需要继续执行工具的完成态
  - 正常结束映射为 final text
  - token 截断映射为 `length`
- token usage
  - 从 Anthropic usage 映射到 `prompt_tokens` / `completion_tokens`
- thinking
  - 如果存在独立 thinking / reasoning block，提取并保存到统一字段

这里的关键不是把 Anthropic block 伪装成 OpenAI SDK 对象，而是让 `LLMResponse` 或其等价封装变成“真正的内部协议对象”。

## 工具调用与回写

## 工具调用解析

[`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py) 当前 `_parse_tool_calls()` 主要兼容两类输入：

- OpenAI 风格 dict
- SDK object，字段为 `tc.function.name` / `tc.function.arguments`

迁移后应扩展为直接兼容 Anthropic `tool_use` block，例如：

```python
{
    "type": "tool_use",
    "id": "...",
    "name": "bash",
    "input": {"command": "pwd"},
}
```

解析目标仍是内部 `ToolCall`：

- `name` <- `tool_use.name`
- `call_id` <- `tool_use.id`
- `args` <- `tool_use.input`

## 工具结果回写

Anthropic 不使用独立 `tool` 角色消息，而是把工具结果作为下一条 `user` 消息中的 `tool_result` block 回写。迁移后 runtime 输出给 store 的 message-ready 结构应为：

```python
{
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": "...",
            "content": "...",
        }
    ],
}
```

如果同一批执行了多个工具，推荐合并为同一条 `user` 消息中的多个 `tool_result` block，而不是多条独立 `user` 消息。原因：

- 更贴近 Anthropic 官方循环示例
- 减少消息列表膨胀
- 避免额外的连续 user 消息合并逻辑

这意味着 [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py) 或其结果适配层需要从“产出 OpenAI 风格 tool message”切换为“产出 Anthropic 风格 tool_result user message”。

## 消息规范化

[`core/llm/protocol.py`](/Users/kino/works/kino/harness/core/llm/protocol.py) 需要从 OpenAI 规范化器改造成 Anthropic 规范化器，主要职责变为：

- 从内部消息中抽出唯一的 `system`
- 丢弃 Anthropic 不接受的无关字段
- 把 assistant 的内部 `tool_calls` 转为 `tool_use` block
- 把内部工具结果消息转为 `tool_result` block
- 在 `max_turns` 或中断场景下为未闭合的 `tool_use` 补 `(cancelled)` 占位 `tool_result`
- 合并连续 `user` / `assistant` 消息时，正确合并字符串与 block 列表

其中一个关键变化是：旧实现默认允许 `tool` 角色存在；新实现应把它视为内部过渡形态，并在真正发给 Anthropic API 前消解掉。

## Thinking 兼容策略

- `LLM_ENABLE_THINKING=true`
  - 如果目标服务支持 Anthropic 风格 thinking 参数，则透传
  - 如果不支持，则自动降级为普通调用，不因为 thinking 不可用而中断主流程
- `LLM_SHOW_THINKING=true`
  - 如果响应中能提取到 thinking / reasoning 内容，则继续显示
  - 如果响应没有该内容，则静默忽略

这两个变量保留的原因是迁移应尽量不破坏现有使用习惯；但 thinking 必须是“可选增强”，而不是协议迁移的前置条件。

## 错误处理

本次只做 SDK 和协议切换，不重写策略：

- 认证失败、网络错误、4xx/5xx、限流错误继续向上抛出
- 上层现有恢复或失败展示逻辑保持不变
- 不新增“兼容服务特判”或 provider 白名单

唯一新增要求是：当 `ANTHROPIC_BASE_URL` 为空时，factory 必须走 Anthropic SDK 默认行为，而不是拼接一个伪默认 URL。

## 主要文件范围

本次设计预计涉及以下文件：

- [`core/shared/config.py`](/Users/kino/works/kino/harness/core/shared/config.py)
- [`core/llm/factory.py`](/Users/kino/works/kino/harness/core/llm/factory.py)
- [`core/llm/openai_client.py`](/Users/kino/works/kino/harness/core/llm/openai_client.py)
- [`core/llm/protocol.py`](/Users/kino/works/kino/harness/core/llm/protocol.py)
- [`core/llm/client.py`](/Users/kino/works/kino/harness/core/llm/client.py)
- [`core/query/loop.py`](/Users/kino/works/kino/harness/core/query/loop.py)
- [`core/tools/__init__.py`](/Users/kino/works/kino/harness/core/tools/__init__.py)
- [`core/tools/runtime.py`](/Users/kino/works/kino/harness/core/tools/runtime.py)
- [`core/session/subagent.py`](/Users/kino/works/kino/harness/core/session/subagent.py)
- [`README.md`](/Users/kino/works/kino/harness/README.md)
- [`/Users/kino/works/kino/harness/.env.example`](/Users/kino/works/kino/harness/.env.example)
- [`requirements.txt`](/Users/kino/works/kino/harness/requirements.txt)

说明：

- `core/llm/openai_client.py` 可以保留文件名后只替换实现，也可以重命名为更中性的文件名；这属于实现决策，不影响本设计
- `core/tools/runtime.py` 只修改输出消息形状，不改变并发/串行执行策略

## 测试策略

本次迁移优先补协议级单测，不打真实网络：

1. 配置测试
   - 新环境变量能正确读取
   - `ANTHROPIC_BASE_URL` 为空时不会强制注入 URL
2. 工具 schema 测试
   - `registry.schemas()` 输出 Anthropic 形状
3. 响应归一化测试
   - text block 正确聚合
   - `tool_use` 正确提取为内部 `tool_calls`
   - usage 正确映射
   - thinking 有值时可提取，无值时可忽略
4. 消息规范化测试
   - system 从内部消息中正确抽离
   - assistant tool call 正确转为 `tool_use`
   - tool result 正确转为 `user.content[]`
   - 未闭合工具调用能补位 `(cancelled)`
5. Query loop 测试
   - Anthropic 风格 tool call 能进入 runtime
   - runtime 结果能以 `tool_result` 形式回写

## 推荐实施顺序

1. 先改配置、依赖和 factory，让 Anthropic client 可以构造出来
2. 再改 response/client 封装，拿到统一的内部 `LLMResponse`
3. 然后改 tool schema 与消息规范化，打通 `tool_use` / `tool_result`
4. 最后修正 query loop 解析与 README / `.env.example`

这个顺序的目的是优先稳定“单次消息调用”边界，再处理工具回路，避免同时在多层追协议差异。
