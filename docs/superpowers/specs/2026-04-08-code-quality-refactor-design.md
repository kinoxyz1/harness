# Agent Loop 代码质量重构设计

## 目标

将当前的 Agent Loop 脚本从单文件、硬编码、缺乏错误处理的状态，重构为结构清晰、类型安全、配置外置的实用工具。

## 约束

- 保持核心 Agent Loop 行为不变
- 允许引入新依赖（rich）
- 允许拆分多个文件
- 定位：个人实用工具，不追求框架级别的抽象

## 文件结构

```
harness/
├── config.py       # 配置常量与环境变量
├── llm.py          # LLM 客户端工厂
├── tools.py        # 工具 schema 定义 + 执行逻辑
├── agent.py        # Agent Loop 核心逻辑
├── main.py         # REPL 入口
├── requirements.txt
└── .env.example    # 环境变量示例
```

## 模块职责

### config.py

集中管理所有配置，通过环境变量读取敏感信息：

```python
import os

API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "deepseek-v3.2")
BASE_URL = os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8000"))
BASH_TIMEOUT = int(os.environ.get("BASH_TIMEOUT", "120"))
```

### llm.py

保留客户端工厂，从 config 读取配置，不再硬编码 API Key：

```python
from openai import OpenAI
from core.config import API_KEY, BASE_URL


def create_llm_client() -> OpenAI:
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)
```

### tools.py

- `TOOLS` schema 定义（当前仅 bash）
- `execute_tool(name: str, arguments: dict) -> str` — 统一工具执行入口
- `run_bash(command: str, timeout: int) -> str` — bash 执行，带改进的安全检查

### agent.py

- `agent_loop(messages: list[dict]) -> None` — 核心循环逻辑
- `print_response(msg)` — 统一消息打印（思考过程 + 最终回复）
- 修复消息历史中 dict/object 混用问题
- JSON 解析异常处理
- 消除重复的思考过程打印逻辑

### main.py

REPL 入口：
- 系统提示初始化
- 用户输入循环
- `exit`/`quit` 退出支持
- 移除死代码（`response_content` list 检查）

## 安全性改进

### API Key

- 从环境变量 `DASHSCOPE_API_KEY` 读取
- 提供 `.env.example` 模板
- 删除硬编码的 Key

### Bash 命令安全

当前 `d in command` 子串匹配的问题：
- `sudo` 会误拦截 `sudoers`、`mkdir sudo_backup` 等合法命令
- `\rm`、`/sbin/shutdown` 等变体可以绕过

改进方案：
1. 用 `shlex.split()` 解析命令，提取首命令名
2. 维护禁止命令黑名单（命令名级别）：`rm -rf /`, `mkfs`, `dd`, `format`
3. 维护需确认命令列表：`rm`, `sudo`, `shutdown`, `reboot`
4. 确认类命令提示用户输入 y/N，而非直接拦截
5. 超时不可配置时使用 `config.BASH_TIMEOUT`

### JSON 解析

`tool_call.function.arguments` 的 JSON 解析加 `try/except JSONDecodeError`，返回错误消息给 LLM 让其重试。

## 代码质量改进

### 类型标注

所有函数添加完整类型标注。引入 `from __future__ import annotations`。

### 消息历史 bug

OpenAI SDK 返回的 `ChatCompletionMessage` 对象不应直接 append 到 `messages` 列表（该列表是 `list[dict]`）。改为使用 `.model_dump()` 转为 dict 后再 append。

### 消除重复代码

`print_response` 函数和 `agent_loop` 内部循环中都打印思考过程，统一由 `print_response` 处理，循环中不再单独打印。

### 死代码移除

`main.py` 中第 113-118 行检查 `isinstance(response_content, list)` 的代码是死代码（`agent_loop` 内部已处理最终输出），移除。

## 用户体验改进

### Rich 格式化

引入 `rich` 库：
- 思考过程：`dim` 灰色 + panel 标题"思考过程"
- Bash 命令：黄色高亮
- 工具输出：`code` 样式
- LLM 调用期间：`rich.status` spinner 显示"正在思考..."
- 最终回复：正常 markdown 渲染

### 流式输出

对最终回复（`finish_reason != "tool_calls"`）使用 `stream=True`，实时输出 token。工具调用阶段保持非流式。

### 退出指令

支持 `exit`、`quit`、`Ctrl+C` 优雅退出，打印告别语。

## 依赖

```
openai
rich
```

## 杂项

- `.gitignore` 添加 `.env`、`__pycache__/`、`.idea/`

## 不做的事

- 不引入框架（smolagents、langchain）
- 不做 Tool 抽象基类（当前只有 bash 一个工具，YAGNI）
- 不做持久化对话历史
- 不做多模型切换
- 不做单元测试（个人工具，后续按需添加）
