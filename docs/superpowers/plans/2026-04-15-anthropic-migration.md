# Anthropic 协议迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 harness 框架从 OpenAI/DashScope 协议完全切换到 Anthropic `messages` 协议，保持上层架构不变。

**Architecture:** 采用方案 A——内部 store 继续使用当前近似通用格式（assistant 带 `tool_calls`、tool role 回写），Anthropic 原生格式只在 `protocol.py` 和 client 层的 API 边界做转换。上层 `QueryLoop`、`SessionEngine`、`ModelGateway` 接口不变。

**Tech Stack:** Python 3.10+, `anthropic` SDK, `rich`, `pytest`

**Spec:** `docs/superpowers/specs/2026-04-15-anthropic-migration-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `core/shared/config.py` | Modify | 环境变量从 DashScope 切到 Anthropic |
| `core/llm/factory.py` | Modify | 从 `openai.OpenAI` 切到 `anthropic.Anthropic` |
| `core/llm/openai_client.py` | **Delete** | 被 `anthropic_client.py` 替代 |
| `core/llm/anthropic_client.py` | **Create** | Anthropic SDK 封装 + LLMResponse |
| `core/llm/protocol.py` | Rewrite | OpenAI 规范化 → Anthropic 规范化 |
| `core/llm/response.py` | No change | `ModelResponse` 保持不变 |
| `core/llm/client.py` | No change | `ModelGateway` 保持不变 |
| `core/query/loop.py` | Modify | 简化 `_parse_tool_calls()` |
| `core/tools/__init__.py` | Modify | `register()`/`filtered()` schema 路径 |
| `core/tools/builtin/bash.py` | Modify | SCHEMA 格式 |
| `core/tools/builtin/read_file.py` | Modify | SCHEMA 格式 |
| `core/tools/builtin/write_file.py` | Modify | SCHEMA 格式 |
| `core/tools/builtin/edit_file.py` | Modify | SCHEMA 格式 |
| `core/tools/builtin/find.py` | Modify | SCHEMA 格式 |
| `core/tools/builtin/todo.py` | Modify | SCHEMA 格式 |
| `core/tools/builtin/subagent.py` | Modify | SCHEMA 格式 |
| `core/session/subagent.py` | Modify | import + schema name 路径 |
| `01_agent_loop.py` | Modify | import 切换 |
| `requirements.txt` | Modify | `openai` → `anthropic` |
| `.env.example` | Modify | 新环境变量 |
| `README.md` | Modify | 配置说明、SCHEMA 示例 |
| `tests/__init__.py` | Create | 测试包 |
| `tests/test_config.py` | Create | 配置读取测试 |
| `tests/test_factory.py` | Create | Anthropic SDK timeout 回归测试 |
| `tests/test_tool_registry.py` | Create | Registry + SCHEMA 测试 |
| `tests/test_protocol.py` | Create | 消息规范化测试 |
| `tests/test_anthropic_client.py` | Create | 响应归一化测试 |
| `tests/test_loop.py` | Create | `_parse_tool_calls()` 测试 |

---

## Task 1: Config & Dependencies

**Files:**
- Modify: `core/shared/config.py`
- Modify: `core/llm/factory.py`
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`
- Create: `tests/test_factory.py`

- [ ] **Step 1: Write config test**

Create `tests/__init__.py` (empty), `tests/test_config.py`, and `tests/test_factory.py`:

```python
"""测试配置层迁移：新环境变量读取、旧变量已移除。"""
import os
import importlib
import pytest


@pytest.fixture(autouse=True)
def _reload_config(monkeypatch):
    """每个测试前重置 config 模块，确保读到最新环境变量。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_ID", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("LLM_SHOW_THINKING", raising=False)
    import core.shared.config as cfg
    importlib.reload(cfg)
    yield
    importlib.reload(cfg)


def test_reads_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.API_KEY == "sk-test-123"


def test_reads_model_id(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "claude-sonnet-4-6-20250514")
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.MODEL == "claude-sonnet-4-6-20250514"


def test_default_model_id():
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.MODEL == "claude-sonnet-4-6-20250514"


def test_reads_base_url(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://kimi.example.com/v1")
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.BASE_URL == "https://kimi.example.com/v1"


def test_base_url_empty_by_default():
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.BASE_URL == ""


def test_preserves_existing_env_vars(monkeypatch):
    monkeypatch.setenv("LLM_MAX_TOKENS", "4096")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    monkeypatch.setenv("BASH_TIMEOUT", "60")
    import core.shared.config as cfg
    importlib.reload(cfg)
    assert cfg.MAX_TOKENS == 4096
    assert cfg.ENABLE_THINKING is False
    assert cfg.BASH_TIMEOUT == 60
```

```python
"""测试 Anthropic client factory：显式 timeout 避免 SDK 默认的非流式长请求拦截。"""
from __future__ import annotations

import pytest

import core.llm.factory as factory


def test_create_llm_client_sets_explicit_timeout_for_long_nonstreaming_requests(monkeypatch):
    monkeypatch.delenv("SSLKEYLOGFILE", raising=False)
    monkeypatch.setattr(factory, "API_KEY", "test-key")
    monkeypatch.setattr(factory, "BASE_URL", "")

    client = factory.create_llm_client()

    def fail_after_timeout_check(*args, **kwargs):
        raise RuntimeError("post called")

    monkeypatch.setattr(client.messages, "_post", fail_after_timeout_check)

    with pytest.raises(RuntimeError, match="post called"):
        client.messages.create(
            model="claude-sonnet-4-6-20250514",
            system="You are helpful.",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=50_000,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kino/works/kino/harness && env -u SSLKEYLOGFILE python -m pytest tests/test_config.py tests/test_factory.py -v`
Expected: FAIL
- `tests/test_config.py` fails because config still reads `DASHSCOPE_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`
- `tests/test_factory.py` fails with `ValueError: Streaming is required for operations that may take longer than 10 minutes...` because factory has not set an explicit Anthropic client timeout

- [ ] **Step 3: Update config.py**

Replace entire `core/shared/config.py`:

```python
from __future__ import annotations

import os

API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL: str = os.environ.get("MODEL_ID", "claude-sonnet-4-6-20250514")
BASE_URL: str = os.environ.get("ANTHROPIC_BASE_URL", "")
MAX_TOKENS: int = int(os.environ.get("LLM_MAX_TOKENS", "8192"))
ENABLE_THINKING: bool = os.environ.get("LLM_ENABLE_THINKING", "true").lower() in ("true", "1", "yes")
SHOW_THINKING: bool = os.environ.get("LLM_SHOW_THINKING", "true").lower() in ("true", "1", "yes")
BASH_TIMEOUT: int = int(os.environ.get("BASH_TIMEOUT", "120"))
MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", "300"))
MAX_OUTPUT_CHARS: int = int(os.environ.get("MAX_OUTPUT_CHARS", "30000"))
```

- [ ] **Step 4: Update requirements.txt**

Replace entire `requirements.txt`:

```
anthropic
rich
pytest
```

- [ ] **Step 5: Install new dependency**

Run: `cd /Users/kino/works/kino/harness && pip install anthropic`

- [ ] **Step 6: Update factory.py**

Replace entire `core/llm/factory.py`:

```python
from __future__ import annotations

import anthropic

from ..shared.config import API_KEY, BASE_URL


def create_llm_client() -> anthropic.Anthropic:
    # Anthropic SDK rejects some large non-streaming requests when using its
    # default 10-minute timeout heuristic. Use an explicit timeout so request
    # validation does not fail before the API call is sent.
    kwargs: dict = {"api_key": API_KEY, "timeout": 3600.0}
    if BASE_URL:
        kwargs["base_url"] = BASE_URL
    return anthropic.Anthropic(**kwargs)
```

- [ ] **Step 7: Run tests to verify**

Run: `cd /Users/kino/works/kino/harness && env -u SSLKEYLOGFILE python -m pytest tests/test_config.py tests/test_factory.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
cd /Users/kino/works/kino/harness
git add core/shared/config.py core/llm/factory.py requirements.txt tests/__init__.py tests/test_config.py tests/test_factory.py
git commit -m "feat: switch config and dependencies from OpenAI/DashScope to Anthropic"
```

---

## Task 2: Tool Schema Migration

将所有 builtin tool 的 SCHEMA 从 OpenAI 格式切到 Anthropic 格式，同时修改 ToolRegistry 和 subagent 中读取 schema 名称的代码。

**Files:**
- Modify: `core/tools/builtin/bash.py`
- Modify: `core/tools/builtin/read_file.py`
- Modify: `core/tools/builtin/write_file.py`
- Modify: `core/tools/builtin/edit_file.py`
- Modify: `core/tools/builtin/find.py`
- Modify: `core/tools/builtin/todo.py`
- Modify: `core/tools/builtin/subagent.py`
- Modify: `core/tools/__init__.py`
- Modify: `core/session/subagent.py`
- Create: `tests/test_tool_registry.py`

- [ ] **Step 1: Write tool registry test**

Create `tests/test_tool_registry.py`:

```python
"""测试工具注册表迁移：Anthropic schema 形状、名称提取、filtered。"""
import pytest
from core.tools import registry


class TestSchemaShape:
    """所有注册工具的 schema 必须是 Anthropic 格式。"""

    def test_schemas_are_anthropic_format(self):
        for schema in registry.schemas():
            assert "name" in schema, f"Missing 'name' in schema: {schema}"
            assert "description" in schema, f"Missing 'description' in schema: {schema}"
            assert "input_schema" in schema, f"Missing 'input_schema' in schema: {schema}"
            assert "type" not in schema, f"Legacy 'type' key still present in: {schema}"
            assert "function" not in schema, f"Legacy 'function' key still present in: {schema}"

    def test_expected_tools_registered(self):
        names = {schema["name"] for schema in registry.schemas()}
        expected = {"bash", "read_file", "write_file", "edit_file", "find", "todo", "subagent"}
        assert names == expected

    def test_input_schema_has_type_object(self):
        for schema in registry.schemas():
            assert schema["input_schema"]["type"] == "object"

    def test_required_params_extracted(self):
        """registry 能从 input_schema.required 正确提取必填参数。"""
        assert "command" in registry._required_params.get("bash", [])
        assert "path" in registry._required_params.get("read_file", [])
        assert "path" in registry._required_params.get("write_file", [])
        assert "content" in registry._required_params.get("write_file", [])


class TestFiltered:
    def test_filtered_returns_subset(self):
        sub = registry.filtered({"bash", "find"})
        names = {s["name"] for s in sub.schemas()}
        assert names == {"bash", "find"}

    def test_filtered_preserves_handlers(self):
        sub = registry.filtered({"bash"})
        assert sub.has("bash")
        assert not sub.has("read_file")

    def test_filtered_empty(self):
        sub = registry.filtered(set())
        assert sub.schemas() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kino/works/kino/harness && python -m pytest tests/test_tool_registry.py -v`
Expected: FAIL — schemas still have `"type": "function"` and `"function"` key

- [ ] **Step 3: Migrate bash.py SCHEMA**

Replace the SCHEMA dict in `core/tools/builtin/bash.py` (lines 12-50 area). The new shape:

```python
SCHEMA: dict[str, Any] = {
    "name": "bash",
    "description": (
        f"在终端执行一条 Shell 命令。命令在子进程中执行，不保留环境变量变更。"
        f"超时设置为 {BASH_TIMEOUT} 秒，长时间运行的命令会被自动终止。"
        "\n\n安全机制："
        "\n- 黑名单命令（mkfs, dd）会被直接拒绝。"
        "\n- 危险命令（rm, sudo, shutdown 等）需要用户确认后才会执行。"
        "\n\n使用场景："
        "\n- 运行测试、构建、git 等需要 Shell 环境的操作"
        "\n- 安装依赖、启动服务等"
        "\n\n不要用 bash 执行以下操作（有专用工具更安全）："
        "\n- cat/head/tail 读取文件 → 用 read_file"
        "\n- find/ls 搜索文件 → 用 find"
        "\n- sed/awk 修改文件 → 用 edit_file"
        "\n- echo > 创建文件 → 用 write_file"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 Shell 命令",
            },
        },
        "required": ["command"],
    },
}
```

- [ ] **Step 4: Migrate read_file.py SCHEMA**

Replace the SCHEMA dict in `core/tools/builtin/read_file.py` (lines 10-47 area):

```python
SCHEMA: dict[str, Any] = {
    "name": "read_file",
    "description": (
        "读取本地文件的文本内容。只读工具，不会修改文件。"
        "\n\n行为要点："
        "\n- 输出带行号，格式与 cat -n 一致。"
        "\n- 单次最多读取 2000 行，大文件请用 offset 和 limit 分段读取。"
        "\n- 检测到二进制文件时会拒绝读取。"
        "\n- 路径支持相对路径（基于工作目录）和绝对路径。"
        "\n\n重要：edit_file 强制要求先完整读取文件后才能编辑。"
        "如果只读了部分内容（使用了 offset/limit），edit_file 也会拒绝执行。"
        "\n\n使用场景："
        "\n- 查看文件内容（不要用 bash cat/head/tail，用本工具更安全）"
        "\n- 编辑前必须先读取文件"
        "\n- 搜索文件路径不确定时，先用 find 工具定位"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径，支持相对路径（基于工作目录）或绝对路径",
            },
            "offset": {
                "type": "integer",
                "description": "从第几行开始读取（从 1 开始），默认为 1",
            },
            "limit": {
                "type": "integer",
                "description": "最多读取的行数，默认读取全部（最大 2000 行）",
            },
        },
        "required": ["path"],
    },
}
```

- [ ] **Step 5: Migrate write_file.py SCHEMA**

Replace the SCHEMA dict in `core/tools/builtin/write_file.py` (lines 10-50 area):

```python
SCHEMA: dict[str, Any] = {
    "name": "write_file",
    "description": (
        "将内容写入文件。支持创建、覆盖和追加三种模式。"
        "\n\n行为要点："
        "\n- mode='write'（默认）：完全覆盖文件，如不存在则自动创建（包括父目录）。"
        "\n- mode='append'：在文件末尾追加内容，如不存在则自动创建。适合分块写入大文件。"
        "\n- 写入后系统自动记录文件认知，后续可用 edit_file 继续编辑。"
        "\n\n使用场景："
        "\n- 创建新文件（不要用 bash echo > file，用本工具更可靠）"
        "\n- 需要完全重写文件内容（如生成配置文件、脚本等）"
        "\n- 分块写入大文件：先 write 创建，再多次 append 追加"
        "\n- 如果只需对文件进行局部修改，优先使用 edit_file，避免意外覆盖未修改的部分"
        "\n\n大文件策略："
        "\n- 如果文件内容很长（超过 200 行），建议分块写入："
        "\n  1. 第一次调用 write_file(path, 第一块内容, mode='write')"
        "\n  2. 后续调用 write_file(path, 下一块内容, mode='append')"
        "\n  3. 直到所有内容写完"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径，支持相对路径（基于工作目录）或绝对路径",
            },
            "content": {
                "type": "string",
                "description": "要写入的文件内容（当前块）",
            },
            "mode": {
                "type": "string",
                "enum": ["write", "append"],
                "description": "写入模式：'write' 覆盖写入（默认），'append' 追加到文件末尾",
            },
        },
        "required": ["path", "content"],
    },
}
```

- [ ] **Step 6: Migrate edit_file.py SCHEMA**

Replace the SCHEMA dict in `core/tools/builtin/edit_file.py` (lines 10-50 area):

```python
SCHEMA: dict[str, Any] = {
    "name": "edit_file",
    "description": (
        "基于字符串替换编辑文件。在文件中查找 old_string 并替换为 new_string。"
        "\n\n前置条件（系统强制）："
        "\n- 必须先用 read_file 完整读取目标文件，否则会被拒绝执行（error: not_read）。"
        "\n- 如果文件在读取后被外部修改，也会被拒绝执行（error: stale），需要重新读取。"
        "\n- 只读了部分内容（使用了 offset/limit）也不行，必须是完整读取。"
        "\n\n匹配规则："
        "\n- old_string 必须与文件内容精确匹配（包括缩进和空行）。"
        "\n- 默认只替换第一个匹配项。设置 replace_all=true 可替换所有匹配项。"
        "\n- 如果 old_string 在文件中存在多处匹配且未设置 replace_all，会报错（error: ambiguous_match）。"
        "\n\n使用场景："
        "\n- 对文件进行精确、局部的修改（不要用 bash sed/awk，用本工具更安全）"
        "\n- 如需完全重写文件，请使用 write_file 工具"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径，支持相对路径（基于工作目录）或绝对路径",
            },
            "old_string": {
                "type": "string",
                "description": "要查找的文本（必须与文件中的内容精确匹配，包括缩进）",
            },
            "new_string": {
                "type": "string",
                "description": "替换后的文本",
            },
            "replace_all": {
                "type": "boolean",
                "description": "是否替换所有匹配项，默认 false（仅替换第一个）",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
}
```

- [ ] **Step 7: Migrate find.py SCHEMA**

Replace the SCHEMA dict in `core/tools/builtin/find.py` (lines 10-46 area):

```python
SCHEMA: dict[str, Any] = {
    "name": "find",
    "description": (
        "按 glob 模式搜索文件路径。只读工具，可安全并行执行。"
        "\n\n行为要点："
        "\n- 返回匹配的文件列表，按修改时间排序（最近修改的在前）。"
        "\n- 最多返回 200 个结果，超出部分截断。"
        "\n- 输出相对路径（相对于搜索目录）。"
        "\n\n模式语法（Python pathlib glob）："
        "\n- `*.py` — 当前目录下的 Python 文件"
        "\n- `**/*.py` — 递归搜索所有 Python 文件"
        "\n- `src/**/*.ts` — src 目录下递归搜索 TypeScript 文件"
        "\n- `path` 参数可指定搜索根目录，默认为当前工作目录"
        "\n\n使用场景："
        "\n- 查找特定类型的文件（不要用 bash find/ls，用本工具更高效）"
        "\n- 确认文件路径是否存在"
        "\n- 浏览项目结构"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "find 匹配模式，如 '**/*.py' 匹配所有 Python 文件",
            },
            "path": {
                "type": "string",
                "description": "搜索的根目录，默认为当前工作目录",
            },
        },
        "required": ["pattern"],
    },
}
```

- [ ] **Step 8: Migrate todo.py SCHEMA**

Replace the SCHEMA dict in `core/tools/builtin/todo.py` (lines 20-50 area):

```python
SCHEMA: dict[str, Any] = {
    "name": "todo",
    "description": "Rewrite the current session plan for multi-step work.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "完整的任务列表（替换旧列表）",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "任务描述",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "failed"],
                            "description": "任务状态",
                        },
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["items"],
    },
}
```

- [ ] **Step 9: Migrate subagent.py SCHEMA**

Replace the SCHEMA dict in `core/tools/builtin/subagent.py` (lines 13-49 area):

```python
SCHEMA: dict[str, Any] = {
    "name": "subagent",
    "description": (
        "Delegate a substantial subtask to an isolated sub-agent. "
        "Use for codebase exploration, implementation planning, "
        "or isolated multi-step work that would otherwise bloat the main context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "A self-contained task prompt for the sub-agent. "
                    "Include all necessary context, constraints, and expected output format."
                ),
            },
            "agent_type": {
                "type": "string",
                "enum": ["explore", "plan", "general"],
                "description": "Sub-agent type. Default is general.",
            },
            "description": {
                "type": "string",
                "description": "A short label for status display, ideally 3-8 words.",
            },
            "max_turns": {
                "type": "integer",
                "description": "Optional per-subagent turn limit.",
            },
        },
        "required": ["task"],
    },
}
```

- [ ] **Step 10: Update ToolRegistry.register() and filtered()**

In `core/tools/__init__.py`, update two methods:

`register()` (line 34-44) — change schema name extraction and required extraction:

```python
    def register(self, module: Any) -> None:
        """从一个工具模块中注册。模块需包含 SCHEMA 和 handle。"""
        name: str = module.SCHEMA["name"]
        self._handlers[name] = module.handle
        self._schemas.append(module.SCHEMA)
        self._readonly[name] = getattr(module, "READONLY", False)
        annotations = getattr(module, "ANNOTATIONS", {})
        if annotations:
            self._annotations[name] = annotations
        required = module.SCHEMA.get("input_schema", {}).get("required", [])
        self._required_params[name] = required
```

`filtered()` (line 64-77) — change schema name reading:

```python
    def filtered(self, allowed_names: set[str]) -> ToolRegistry:
        """返回只包含允许工具的新注册表。"""
        new_reg = ToolRegistry()
        for schema in self._schemas:
            name = schema["name"]
            if name in allowed_names:
                new_reg._handlers[name] = self._handlers[name]
                new_reg._schemas.append(schema)
                new_reg._readonly[name] = self._readonly.get(name, False)
                if name in self._annotations:
                    new_reg._annotations[name] = self._annotations[name]
                if name in self._required_params:
                    new_reg._required_params[name] = self._required_params[name]
        return new_reg
```

- [ ] **Step 11: Update _compute_allowed_names() in subagent.py**

In `core/session/subagent.py`, change line 135 from `schema["function"]["name"]` to `schema["name"]`:

```python
def _compute_allowed_names(definition: SubagentDefinition) -> set[str]:
    """根据子代理定义计算允许的工具名集合。"""
    if definition.allowed_tools is None:
        allowed_names = {schema["name"] for schema in registry.schemas()}
    else:
        allowed_names = set(definition.allowed_tools)
    allowed_names -= set(definition.disallowed_tools)
    return allowed_names
```

- [ ] **Step 12: Run tests to verify**

Run: `cd /Users/kino/works/kino/harness && python -m pytest tests/test_tool_registry.py -v`
Expected: All PASS

- [ ] **Step 13: Commit**

```bash
cd /Users/kino/works/kino/harness
git add core/tools/ core/session/subagent.py tests/test_tool_registry.py
git commit -m "feat: migrate tool schemas and registry from OpenAI to Anthropic format"
```

---

## Task 3: Protocol Rewrite

完全重写 `core/llm/protocol.py`，从 OpenAI 规范化器改为 Anthropic 规范化器。

**Files:**
- Rewrite: `core/llm/protocol.py`
- Create: `tests/test_protocol.py`

- [ ] **Step 1: Write protocol tests**

Create `tests/test_protocol.py`:

```python
"""测试消息规范化层：Anthropic 协议转换。"""
import pytest
from core.llm.protocol import normalize_messages


class TestSystemExtraction:
    def test_extracts_system_to_separate_return(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = normalize_messages(messages)
        assert system == "You are helpful."
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_merges_multiple_system_messages(self):
        messages = [
            {"role": "system", "content": "Part 1"},
            {"role": "system", "content": "Part 2"},
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = normalize_messages(messages)
        assert system == "Part 1\n\nPart 2"
        assert len(msgs) == 1

    def test_no_system_returns_empty_string(self):
        messages = [
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = normalize_messages(messages)
        assert system == ""

    def test_empty_system_message_skipped(self):
        messages = [
            {"role": "system", "content": ""},
            {"role": "user", "content": "Hi"},
        ]
        system, msgs = normalize_messages(messages)
        assert system == ""


class TestAssistantToolCalls:
    def test_converts_tool_calls_to_tool_use_blocks(self):
        messages = [
            {"role": "user", "content": "Run pwd"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
                ],
            },
        ]
        system, msgs = normalize_messages(messages)
        assert len(msgs) == 2
        assistant_msg = msgs[1]
        assert assistant_msg["role"] == "assistant"
        assert isinstance(assistant_msg["content"], list)
        assert assistant_msg["content"][0] == {
            "type": "tool_use",
            "id": "tu_1",
            "name": "bash",
            "input": {"command": "pwd"},
        }

    def test_assistant_with_text_and_tool_calls(self):
        messages = [
            {"role": "user", "content": "Do it"},
            {
                "role": "assistant",
                "content": "Let me check",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "ls"}},
                ],
            },
        ]
        system, msgs = normalize_messages(messages)
        content = msgs[1]["content"]
        assert len(content) == 2
        assert content[0] == {"type": "text", "text": "Let me check"}
        assert content[1]["type"] == "tool_use"


class TestToolResultConversion:
    def test_converts_tool_messages_to_user_tool_result(self):
        messages = [
            {"role": "user", "content": "Run pwd"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tu_1", "name": "bash", "args": {"command": "pwd"}}],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "/home/user"},
        ]
        system, msgs = normalize_messages(messages)
        # Last message should be user with tool_result block
        last = msgs[-1]
        assert last["role"] == "user"
        assert isinstance(last["content"], list)
        assert last["content"][0] == {
            "type": "tool_result",
            "tool_use_id": "tu_1",
            "content": "/home/user",
        }

    def test_merges_consecutive_tool_messages(self):
        messages = [
            {"role": "user", "content": "Check"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
                    {"id": "tu_2", "name": "bash", "args": {"command": "ls"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "/home"},
            {"role": "tool", "tool_call_id": "tu_2", "content": "file1.py"},
        ]
        system, msgs = normalize_messages(messages)
        # Two tool messages should merge into one user message
        last = msgs[-1]
        assert last["role"] == "user"
        assert len(last["content"]) == 2
        assert last["content"][0]["tool_use_id"] == "tu_1"
        assert last["content"][1]["tool_use_id"] == "tu_2"


class TestUnclosedToolCalls:
    def test_inserts_cancelled_placeholder_for_unclosed(self):
        messages = [
            {"role": "user", "content": "Do stuff"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
                    {"id": "tu_2", "name": "bash", "args": {"command": "ls"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "/home"},
        ]
        system, msgs = normalize_messages(messages)
        # tu_2 has no tool result — should get (cancelled) placeholder
        last = msgs[-1]
        assert last["role"] == "user"
        tool_results = [b for b in last["content"] if b["type"] == "tool_result"]
        ids = {tr["tool_use_id"] for tr in tool_results}
        assert "tu_1" in ids
        assert "tu_2" in ids
        tu2_result = next(tr for tr in tool_results if tr["tool_use_id"] == "tu_2")
        assert tu2_result["content"] == "(cancelled)"


class TestConsecutiveRoleMerge:
    def test_merges_consecutive_user_strings(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "World"},
        ]
        system, msgs = normalize_messages(messages)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello\nWorld"

    def test_no_reasoning_content_leaked(self):
        messages = [
            {"role": "assistant", "content": "Hi", "reasoning_content": "thinking..."},
        ]
        system, msgs = normalize_messages(messages)
        assistant = msgs[0]
        # Anthropic format: content is list of blocks
        assert isinstance(assistant["content"], list)
        text_blocks = [b for b in assistant["content"] if b["type"] == "text"]
        assert len(text_blocks) == 1
        assert text_blocks[0]["text"] == "Hi"
        # No reasoning_content key leaked
        assert "reasoning_content" not in assistant

    def test_does_not_modify_original(self):
        original = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello", "tool_calls": [{"id": "tu_1", "name": "bash", "args": {"command": "pwd"}}]},
            {"role": "tool", "tool_call_id": "tu_1", "content": "/home"},
        ]
        import copy
        snapshot = copy.deepcopy(original)
        normalize_messages(original)
        assert original == snapshot
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kino/works/kino/harness && python -m pytest tests/test_protocol.py -v`
Expected: FAIL — `normalize_messages` still has old signature `list[dict] -> list[dict]`

- [ ] **Step 3: Rewrite protocol.py**

Replace entire `core/llm/protocol.py`:

```python
"""消息规范化层：将内部消息列表转换为 Anthropic messages 协议格式。

解决三类问题：
1. system 独立抽离为顶层参数
2. 内部 tool_calls / tool role 转换为 Anthropic tool_use / tool_result block
3. 角色交替、未闭合工具调用的修正

内部消息格式（方案 A）：
- assistant: {"role": "assistant", "content": "...", "tool_calls": [...]}
- tool: {"role": "tool", "tool_call_id": "...", "content": "..."}

Anthropic API 格式：
- system: 顶层参数
- messages: 只有 user/assistant，tool_result 嵌入 user.content[]
"""
from __future__ import annotations

from typing import Any


def normalize_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """将内部消息列表规范化为 Anthropic API 格式。

    Returns:
        (system, messages) 二元组。
        system: 合并后的系统提示词（可能为空字符串）。
        messages: 仅包含 user/assistant 角色的 Anthropic 格式消息列表。
    """
    # 1. 分离 system
    system_parts: list[str] = []
    non_system: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if content:
                system_parts.append(content)
        else:
            non_system.append(msg)

    system = "\n\n".join(system_parts)

    # 2. 转换内部消息为 Anthropic 格式
    converted: list[dict[str, Any]] = []
    for msg in non_system:
        role = msg.get("role")
        if role == "user":
            converted.append(_convert_user(msg))
        elif role == "assistant":
            converted.append(_convert_assistant(msg))
        elif role == "tool":
            converted.append(msg)  # 保留原始，后续处理

    # 3. 补齐未闭合的 tool_use
    converted = _pair_tool_results(converted)

    # 4. 把连续 tool 消息合并为 user + tool_result blocks
    converted = _merge_tool_results(converted)

    # 5. 合并连续同角色消息
    converted = _merge_consecutive_roles(converted)

    return system, converted


def _convert_user(msg: dict[str, Any]) -> dict[str, Any]:
    """转换内部 user 消息。"""
    return {"role": "user", "content": msg.get("content", "")}


def _convert_assistant(msg: dict[str, Any]) -> dict[str, Any]:
    """转换内部 assistant 消息，把 tool_calls 转为 tool_use blocks。"""
    content_blocks: list[dict[str, Any]] = []

    text = msg.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    tool_calls = msg.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc.get("args", {}),
            })

    return {
        "role": "assistant",
        "content": content_blocks if content_blocks else "",
    }


def _pair_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确保每个 tool_use block 都有匹配的 tool_result。

    为缺失的 tool_use 插入 (cancelled) 占位 tool 消息。
    """
    paired_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id:
                paired_ids.add(tc_id)

    insertions: list[tuple[int, dict[str, Any]]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tc_id = block.get("id")
                if tc_id and tc_id not in paired_ids:
                    insertions.append((
                        i + 1 + len(insertions),
                        {"role": "tool", "tool_call_id": tc_id, "content": "(cancelled)"},
                    ))

    for idx, placeholder in insertions:
        messages.insert(idx, placeholder)

    return messages


def _merge_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把连续的内部 tool 消息聚合为一条 user 消息中的多个 tool_result block。"""
    result: list[dict[str, Any]] = []
    tool_buffer: list[dict[str, Any]] = []

    def flush_buffer():
        if tool_buffer:
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg.get("content", ""),
                }
                for msg in tool_buffer
            ]
            result.append({"role": "user", "content": blocks})
            tool_buffer.clear()

    for msg in messages:
        if msg.get("role") == "tool":
            tool_buffer.append(msg)
        else:
            flush_buffer()
            result.append(msg)

    flush_buffer()
    return result


def _merge_consecutive_roles(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并连续同角色消息（Anthropic 要求 user/assistant 交替）。"""
    if not messages:
        return messages

    merged: list[dict[str, Any]] = [messages[0]]

    for msg in messages[1:]:
        prev = merged[-1]
        if msg["role"] == prev["role"] == "user":
            prev_content = prev.get("content", "")
            msg_content = msg.get("content", "")
            if isinstance(prev_content, str) and isinstance(msg_content, str):
                prev["content"] = prev_content + "\n" + msg_content
            else:
                prev["content"] = _to_blocks(prev_content) + _to_blocks(msg_content)
        elif msg["role"] == prev["role"] == "assistant":
            prev_content = prev.get("content", "")
            msg_content = msg.get("content", "")
            if isinstance(prev_content, list) and isinstance(msg_content, list):
                prev["content"] = prev_content + msg_content
            elif isinstance(prev_content, list):
                prev["content"] = prev_content + [{"type": "text", "text": msg_content or ""}]
            elif isinstance(msg_content, list):
                prev["content"] = [{"type": "text", "text": prev_content or ""}] + msg_content
            else:
                prev["content"] = (prev_content or "") + (msg_content or "")
        else:
            merged.append(msg)

    return merged


def _to_blocks(content: Any) -> list[dict[str, Any]]:
    """将 content 转为 block 列表。"""
    if isinstance(content, list):
        return content
    if isinstance(content, str) and content:
        return [{"type": "text", "text": content}]
    return []

- [ ] **Step 4: Run tests to verify**

Run: `cd /Users/kino/works/kino/harness && python -m pytest tests/test_protocol.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/kino/works/kino/harness
git add core/llm/protocol.py tests/test_protocol.py
git commit -m "feat: rewrite protocol.py for Anthropic message normalization"
```

---

## Task 4: Anthropic Client

创建新的 Anthropic client，替换 `openai_client.py`。

**Files:**
- Create: `core/llm/anthropic_client.py`
- Create: `tests/test_anthropic_client.py`
- Delete: `core/llm/openai_client.py`

- [ ] **Step 1: Write client tests**

Create `tests/test_anthropic_client.py`:

```python
"""测试 Anthropic client：响应归一化、LLMResponse 属性。"""
import pytest
from unittest.mock import MagicMock, patch
from core.llm.anthropic_client import _parse_response, LLMResponse


def _mock_anthropic_response(
    content_blocks=None,
    stop_reason="end_turn",
    input_tokens=100,
    output_tokens=50,
):
    """构建模拟 Anthropic API response 对象。"""
    if content_blocks is None:
        content_blocks = [MagicMock(type="text", text="Hello!")]

    response = MagicMock()
    response.content = content_blocks
    response.stop_reason = stop_reason
    response.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return response


class TestParseResponse:
    def test_text_only_response(self):
        resp = _mock_anthropic_response([
            MagicMock(type="text", text="Hello world"),
        ])
        llm = _parse_response(resp)
        assert llm.content == "Hello world"
        assert llm.tool_calls == []
        assert llm.finish_reason == "end_turn"
        assert llm.prompt_tokens == 100
        assert llm.completion_tokens == 50

    def test_multiple_text_blocks_joined(self):
        resp = _mock_anthropic_response([
            MagicMock(type="text", text="Part 1"),
            MagicMock(type="text", text="Part 2"),
        ])
        llm = _parse_response(resp)
        assert llm.content == "Part 1\nPart 2"

    def test_tool_use_blocks_normalized(self):
        tool_block = MagicMock(type="tool_use", id="tu_1", name="bash", input={"command": "pwd"})
        resp = _mock_anthropic_response([tool_block], stop_reason="tool_use")
        llm = _parse_response(resp)
        assert llm.content is None
        assert len(llm.tool_calls) == 1
        tc = llm.tool_calls[0]
        assert tc == {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}}

    def test_thinking_block_extracted(self):
        thinking_block = MagicMock(type="thinking", thinking="Let me reason...")
        text_block = MagicMock(type="text", text="Answer")
        resp = _mock_anthropic_response([thinking_block, text_block])
        llm = _parse_response(resp)
        assert llm.reasoning == "Let me reason..."
        assert llm.content == "Answer"

    def test_no_thinking_block_gives_none(self):
        resp = _mock_anthropic_response([MagicMock(type="text", text="Hi")])
        llm = _parse_response(resp)
        assert llm.reasoning is None


class TestLLMResponseProperties:
    def test_has_content_true(self):
        llm = LLMResponse(content="Hello")
        assert llm.has_content is True

    def test_has_content_false_when_empty(self):
        llm = LLMResponse(content="")
        assert llm.has_content is False

    def test_is_tool_call_by_finish_reason(self):
        llm = LLMResponse(finish_reason="tool_use", tool_calls=[{"id": "1", "name": "bash", "args": {}}])
        assert llm.is_tool_call is True

    def test_is_truncated(self):
        llm = LLMResponse(finish_reason="max_tokens")
        assert llm.is_truncated is True


class TestClientCall:
    def test_stream_raises_error(self):
        from core.llm.anthropic_client import AnthropicClient
        client = AnthropicClient.__new__(AnthropicClient)
        with pytest.raises(NotImplementedError, match="Streaming"):
            client.call([], stream=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kino/works/kino/harness && python -m pytest tests/test_anthropic_client.py -v`
Expected: FAIL — `anthropic_client` module doesn't exist yet

- [ ] **Step 3: Create anthropic_client.py**

Create `core/llm/anthropic_client.py`:

```python
"""LLM 调用层：封装与 Anthropic messages API 的所有交互。

职责：
- LLMResponse：对 API response 的结构化封装（协议无关的内部对象）
- AnthropicClient：管理 client 生命周期，封装调用逻辑
- _parse_response：Anthropic block → 内部 LLMResponse 的归一化
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any

from rich.console import Console

from ..shared.config import ENABLE_THINKING, MAX_TOKENS, MODEL
from .factory import create_llm_client
from .protocol import normalize_messages
from ..shared.run_options import RunDisplayOptions

_console = Console()


class LLMResponse:
    """对 API response 的结构化封装。

    所有调用者通过这个类访问响应，不需要关心底层 SDK 的差异。
    """

    def __init__(
        self,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        finish_reason: str = "end_turn",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        reasoning: str | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning = reasoning
        self.finish_reason = finish_reason
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    @property
    def has_content(self) -> bool:
        return bool(self.content and self.content.strip())

    @property
    def is_tool_call(self) -> bool:
        if self.finish_reason == "tool_use":
            return True
        if self.tool_calls and not self.has_content:
            return True
        return False

    @property
    def is_truncated(self) -> bool:
        return self.finish_reason == "max_tokens"

    @property
    def raw_response(self) -> Any:
        return self._raw if hasattr(self, "_raw") else None


class AnthropicClient:
    """封装 Anthropic messages API 的调用逻辑。"""

    def __init__(self) -> None:
        self._client = create_llm_client()

    def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        display: RunDisplayOptions | None = None,
    ) -> LLMResponse:
        if stream:
            raise NotImplementedError("Streaming is not supported in this migration")

        display = display or RunDisplayOptions()

        system, api_messages = normalize_messages(messages)

        params: dict[str, Any] = {
            "model": MODEL,
            "system": system,
            "messages": api_messages,
            "max_tokens": MAX_TOKENS,
        }
        if tools:
            params["tools"] = tools
        if ENABLE_THINKING:
            params["thinking"] = {"type": "enabled", "budget_tokens": min(MAX_TOKENS, 10000)}

        result: dict[str, Any] = {}
        error: dict[str, Any] = {}

        def do_call() -> None:
            try:
                result["data"] = self._client.messages.create(**params)
            except Exception as e:
                error["data"] = e

        start = time.time()
        thread = threading.Thread(target=do_call)
        thread.start()

        while thread.is_alive():
            elapsed = int(time.time() - start)
            if not display.quiet:
                sys.stdout.write(f"\r\033[K\033[32m正在思考... {elapsed}s\033[0m")
                sys.stdout.flush()
            thread.join(timeout=1.0)

        if not display.quiet:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        if error.get("data"):
            raise error["data"]

        response = result["data"]
        elapsed = time.time() - start

        llm_resp = _parse_response(response)
        llm_resp._raw = response

        if not display.quiet:
            _console.print(
                f"[dim]{elapsed:.1f}s │ token {llm_resp.prompt_tokens}↓ {llm_resp.completion_tokens}↑"
                f" │ finish={llm_resp.finish_reason}[/dim]"
            )

        return llm_resp


def _parse_response(response: Any) -> LLMResponse:
    """将 Anthropic API response 归一化为内部 LLMResponse。"""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning: str | None = None

    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "name": block.name,
                "args": block.input if isinstance(block.input, dict) else {},
            })
        elif block.type == "thinking":
            reasoning = block.thinking

    content = "\n".join(text_parts) if text_parts else None

    prompt_tokens = response.usage.input_tokens if response.usage else 0
    completion_tokens = response.usage.output_tokens if response.usage else 0

    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=response.stop_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning=reasoning,
    )
```

- [ ] **Step 4: Run tests to verify**

Run: `cd /Users/kino/works/kino/harness && python -m pytest tests/test_anthropic_client.py -v`
Expected: All PASS

- [ ] **Step 5: Delete old openai_client.py**

Run: `rm core/llm/openai_client.py`

- [ ] **Step 6: Commit**

```bash
cd /Users/kino/works/kino/harness
git add core/llm/anthropic_client.py tests/test_anthropic_client.py
git rm core/llm/openai_client.py
git commit -m "feat: replace OpenAI client with Anthropic client"
```

---

## Task 5: Query Loop & Entrypoint Integration

简化 `_parse_tool_calls()`，更新入口文件的 import。

**Files:**
- Modify: `core/query/loop.py`
- Modify: `01_agent_loop.py`
- Modify: `core/session/subagent.py`
- Create: `tests/test_loop.py`

- [ ] **Step 1: Write loop tests**

Create `tests/test_loop.py`:

```python
"""测试 _parse_tool_calls 简化：只接受 ToolCall 实例和归一 dict。"""
import pytest
from core.query.loop import _parse_tool_calls
from core.tools.runtime import ToolCall


class TestParseNormalizedDict:
    def test_parses_normalized_tool_call_dict(self):
        raw = [
            {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
        ]
        calls = _parse_tool_calls(raw)
        assert len(calls) == 1
        assert calls[0].name == "bash"
        assert calls[0].call_id == "tu_1"
        assert calls[0].args == {"command": "pwd"}
        assert calls[0].idx == 0

    def test_parses_multiple_dicts(self):
        raw = [
            {"id": "tu_1", "name": "bash", "args": {"command": "pwd"}},
            {"id": "tu_2", "name": "read_file", "args": {"path": "test.py"}},
        ]
        calls = _parse_tool_calls(raw)
        assert len(calls) == 2
        assert calls[1].name == "read_file"
        assert calls[1].idx == 1

    def test_handles_missing_args_gracefully(self):
        raw = [{"id": "tu_1", "name": "bash"}]
        calls = _parse_tool_calls(raw)
        assert calls[0].args == {}

    def test_handles_non_dict_args(self):
        raw = [{"id": "tu_1", "name": "bash", "args": "not a dict"}]
        calls = _parse_tool_calls(raw)
        assert calls[0].args == {}


class TestParseToolCallInstance:
    def test_passes_through_toolcall_instances(self):
        tc = ToolCall(idx=0, name="bash", call_id="tu_1", args={"command": "pwd"})
        calls = _parse_tool_calls([tc])
        assert len(calls) == 1
        assert calls[0] is tc

    def test_generates_fallback_call_id(self):
        raw = [{"name": "bash", "args": {}}]
        calls = _parse_tool_calls(raw)
        assert calls[0].call_id.startswith("call_")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kino/works/kino/harness && python -m pytest tests/test_loop.py -v`
Expected: Some tests may fail because `_parse_tool_calls` still has OpenAI-specific branches

- [ ] **Step 3: Simplify _parse_tool_calls()**

Replace `_parse_tool_calls` function in `core/query/loop.py` (lines 10-45):

```python
def _parse_tool_calls(raw_calls: list) -> list[ToolCall]:
    """将归一 tool_call dict 或 ToolCall 实例转为 ToolCall 列表。

    输入格式：
    - ToolCall 实例：直接透传
    - dict: {"id": "...", "name": "...", "args": {...}}
    """
    calls: list[ToolCall] = []
    for i, tc in enumerate(raw_calls):
        if isinstance(tc, ToolCall):
            calls.append(tc)
        elif isinstance(tc, dict):
            args = tc.get("args", {})
            if not isinstance(args, dict):
                args = {}
            calls.append(ToolCall(
                idx=i,
                name=tc.get("name", "unknown"),
                call_id=tc.get("id", f"call_{i}"),
                args=args,
            ))
    return calls
```

Also remove the `import json` at line 3 (no longer needed).

- [ ] **Step 4: Update 01_agent_loop.py imports**

In `01_agent_loop.py`, change line 7:

```python
from core.llm.anthropic_client import AnthropicClient
```

And change line 24:

```python
        model_gateway=ModelGateway(AnthropicClient()),
```

- [ ] **Step 5: Update subagent.py imports**

In `core/session/subagent.py`, change line 11:

```python
from ..llm.anthropic_client import AnthropicClient
```

And change line 179:

```python
        self._llm_factory = llm_factory or AnthropicClient
```

- [ ] **Step 6: Run all tests to verify**

Run: `cd /Users/kino/works/kino/harness && python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
cd /Users/kino/works/kino/harness
git add core/query/loop.py 01_agent_loop.py core/session/subagent.py tests/test_loop.py
git commit -m "feat: simplify loop parsing, update imports to Anthropic client"
```

---

## Task 6: Documentation

更新 `.env.example` 和 `README.md`。

**Files:**
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Update .env.example**

Replace entire `.env.example`:

```
ANTHROPIC_API_KEY=your-api-key-here
MODEL_ID=claude-sonnet-4-6-20250514
# ANTHROPIC_BASE_URL=  # Optional: for Anthropic-compatible services (Kimi, GLM, etc.)
LLM_MAX_TOKENS=8192
LLM_ENABLE_THINKING=true
LLM_SHOW_THINKING=true
BASH_TIMEOUT=120
AGENT_MAX_TURNS=300
```

- [ ] **Step 2: Update README.md**

Update the configuration table section (around lines 100-112). Replace:

```markdown
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | （必填） | Anthropic API Key |
| `MODEL_ID` | `claude-sonnet-4-6-20250514` | 模型 ID |
| `ANTHROPIC_BASE_URL` | （空） | 可选，用于兼容 Anthropic 协议的服务 |
| `LLM_MAX_TOKENS` | `8192` | 单次请求最大输出 token |
| `LLM_ENABLE_THINKING` | `true` | 启用推理模式 |
| `LLM_SHOW_THINKING` | `true` | 显示推理过程 |
| `BASH_TIMEOUT` | `120` | bash 命令超时（秒） |
| `AGENT_MAX_TURNS` | `300` | 工具调用轮次上限 |
```

Update the supported models section (around lines 113-124). Replace:

```markdown
### 支持的模型

只要 API 兼容 Anthropic messages 协议就能用。已测试：

| 模型 | Provider | 说明 |
|------|----------|------|
| claude-sonnet-4-6 | Anthropic 官方 | 推荐模型 |
| claude-opus-4-6 | Anthropic 官方 | 高能力模型 |

切换模型只需修改 `.env` 中的 `MODEL_ID` 和 `ANTHROPIC_BASE_URL`。
```

Update the "添加新工具" section SCHEMA example (around lines 141-157). Replace the SCHEMA dict:

```python
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
```

Update the schema verification command (around line 172):

```python
python -c "from core.tools import registry; print([s['name'] for s in registry.schemas()])"
```

- [ ] **Step 3: Commit**

```bash
cd /Users/kino/works/kino/harness
git add .env.example README.md
git commit -m "docs: update README and .env.example for Anthropic protocol"
```

---

## Task 7: Smoke Test

端到端验证：确认整个应用能正常启动并完成一次调用。

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/kino/works/kino/harness && python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 2: Verify module imports**

Run: `cd /Users/kino/works/kino/harness && python -c "from core.llm.anthropic_client import AnthropicClient; from core.tools import registry; print('Imports OK'); print([s['name'] for s in registry.schemas()])"`
Expected: `Imports OK` followed by list of tool names

- [ ] **Step 3: Verify protocol normalization**

Run: `cd /Users/kino/works/kino/harness && python -c "
from core.llm.protocol import normalize_messages
msgs = [
    {'role': 'system', 'content': 'You are helpful.'},
    {'role': 'user', 'content': 'Hello'},
    {'role': 'assistant', 'content': 'Let me check', 'tool_calls': [{'id': 'tu_1', 'name': 'bash', 'args': {'command': 'pwd'}}]},
    {'role': 'tool', 'tool_call_id': 'tu_1', 'content': '/home/user'},
    {'role': 'assistant', 'content': 'You are at /home/user'},
]
system, normalized = normalize_messages(msgs)
print('system:', repr(system))
for m in normalized:
    print(m['role'], ':', m.get('content', '')[:80])
"`
Expected: system extracted, tool_calls → tool_use blocks, tool → user+tool_result blocks

- [ ] **Step 4: Final commit (if any fixes)**

If any fixes were needed during smoke test, commit them:

```bash
cd /Users/kino/works/kino/harness
git add -A
git commit -m "fix: address smoke test issues"
```
