# Streaming Query Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `docs/superpowers/specs/2026-05-13-streaming-query-runtime-design.md` 落成可执行实现，让主 agent 支持真实流式 thinking/reply，tool 与 subagent 支持统一事件协议和分层降级。

**Architecture:** 采用“统一 `StreamEvent` 协议 + `StreamAccumulator` 聚合 + `ModelGateway.stream_once()` + `QueryLoop` 流式主分支 + renderer 增量消费”的方案。现有 transcript、policy、governor、offloader 仍保持权威；event 只负责实时展示，tool/subagent 通过 `source_mode=live|replayed` 接到同一条 UI 管线。

**Tech Stack:** Python 3.10+, dataclasses, Rich, Anthropic-compatible SDK, pytest, 现有 `SessionEngine` / `QueryLoop` / `ToolExecutorRuntime` / `SubagentRuntime`

---

## Context

- 设计文档：`docs/superpowers/specs/2026-05-13-streaming-query-runtime-design.md`
- 关键现状：
  - `core/llm/anthropic_client.py` 目前只支持 `call()`，`stream=True` 直接抛 `NotImplementedError`
  - `core/llm/client.py` 目前只有 `ModelGateway.call_once()`
  - `core/query/loop.py` 只接受完整 `ModelResponse`
  - `core/ui/renderer.py` 目前只有 batch 风格 `show_*()` API
  - `core/tools/runtime.py` 并行工具已经存在，但展示层接近“先全部跑完再看结果”
  - `core/session/subagent.py` 已有 `emit` bridge，但传回父线程的仍是 batch 风格事件
- 本计划默认在当前 workspace 中编写 implementation spec；实际编码前应优先切到独立 worktree。

## Scope

本计划包含：

1. 新建统一 `StreamEvent` 协议和 `StreamAccumulator`
2. 为 `AnthropicClient` 和 `ModelGateway` 增加同步阻塞式 streaming API
3. 扩展 renderer 接口和 Rich 流式渲染实现
4. 给 `QueryLoop` 增加 `STREAMING_ENABLED` 主分支和 partial-output tradeoff
5. 让 `ToolExecutorRuntime` 发出实时事件，同时保持 transcript 顺序稳定
6. 让 `SubagentRuntime` bridge 同时支持 `consume_event()` 和 legacy `show_*()` 回放
7. 补齐 targeted tests 和回归命令

本计划不包含：

- 全局 async event bus
- 远端 transport / websocket 改造
- 新的 transcript 存储格式
- tool 输出逐字符/token 流式

## File Map

| 文件 | 操作 | 责任 |
| --- | --- | --- |
| `core/shared/stream_events.py` | Create | 统一事件协议、事件构造 helper、`StreamAccumulator` |
| `core/shared/config.py` | Modify | 新增 streaming 配置开关 |
| `core/shared/interfaces.py` | Modify | renderer / llm client streaming 协议扩展 |
| `core/llm/client.py` | Modify | `ModelGateway.stream_once()` 与 request options 兼容 |
| `core/llm/anthropic_client.py` | Modify | `AnthropicClient.stream()`，停止直接写 stdout |
| `core/query/state.py` | Modify | 流式状态字段 |
| `core/query/loop.py` | Modify | streaming 主分支、fallback 分支、partial persistence |
| `core/ui/renderer.py` | Modify | `begin_stream()` / `consume_event()` / `end_stream()` 与 80ms flush |
| `core/tools/runtime.py` | Modify | tool 事件发射、preview-safe result、顺序稳定 |
| `core/session/subagent.py` | Modify | bridge renderer 统一协议，subagent replay/live |
| `tests/test_stream_events.py` | Create | 事件协议与 accumulator 测试 |
| `tests/test_model_gateway.py` | Modify | gateway dual path 测试 |
| `tests/test_anthropic_client.py` | Modify | client streaming、cancel、fallback 测试 |
| `tests/test_query_logging.py` | Modify | thinking / status / error / streaming path 测试 |
| `tests/test_query_display.py` | Modify | tool turn / fallback / transcript 权威测试 |
| `tests/test_runtime_logging.py` | Modify | Rich renderer 流式展示和 flush 测试 |
| `tests/test_tool_runtime.py` | Modify | 实时 tool 事件顺序和 transcript 顺序分离测试 |
| `tests/session/test_subagent_runtime.py` | Modify | bridge live/replayed 事件测试 |

## Implementation Rules

1. 先测试，再实现，每个任务单独提交。
2. `QueryLoop.run()` 仍是唯一主循环；不要把控制流推回 `ModelGateway`。
3. `AnthropicClient` 不再直接写 stdout；所有用户可见输出统一交给 renderer 事件接口。
4. transcript 权威不能改：assistant/tool 最终 message 仍由 `store.append()/extend()` 驱动。
5. `sequence` 固定为 per-turn 局部单调递增。
6. flush 窗口固定 `80ms`，v1 不做动态调节。
7. `tool_call_result` 给 renderer 的内容必须是 preview-safe；大结果仍走当前 truncate + offloader 逻辑。
8. `PERSIST_PARTIAL_STREAM_OUTPUT` 默认 `false`；如果实现可选持久化，必须给 message 打 `incomplete=True`。

### Task 1: 建立 `StreamEvent` 协议和 `StreamAccumulator`

**Files:**
- Create: `core/shared/stream_events.py`
- Create: `tests/test_stream_events.py`

- [ ] **Step 1: 先写失败测试，锁定事件协议和 accumulator 行为**

在 `tests/test_stream_events.py` 创建以下测试：

```python
from core.llm.response import ModelResponse
from core.shared.stream_events import StreamAccumulator, StreamEvent, make_event


def test_make_event_defaults_timestamp_and_payload() -> None:
    event = make_event(
        type="response_start",
        turn_id="turn-1",
        sequence=1,
        origin="model",
        source_mode="live",
    )

    assert isinstance(event, StreamEvent)
    assert event.type == "response_start"
    assert event.turn_id == "turn-1"
    assert event.sequence == 1
    assert event.payload == {}
    assert event.timestamp > 0


def test_stream_accumulator_reconstructs_model_response() -> None:
    acc = StreamAccumulator()
    acc.consume(make_event("response_start", "turn-1", 1, "model", "live"))
    acc.consume(make_event("thinking_delta", "turn-1", 2, "model", "live", {"text": "先分析"}))
    acc.consume(make_event("content_delta", "turn-1", 3, "model", "live", {"text": "最终回答"}))
    acc.consume(
        make_event(
            "tool_call_ready",
            "turn-1",
            4,
            "model",
            "live",
            {"tool_call": {"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}}},
        )
    )
    acc.consume(
        make_event(
            "response_completed",
            "turn-1",
            5,
            "model",
            "live",
            {
                "finish_reason": "tool_use",
                "prompt_tokens": 123,
                "completion_tokens": 45,
                "reasoning_signature": "sig_1",
            },
        )
    )

    resp = acc.finalize()

    assert isinstance(resp, ModelResponse)
    assert resp.reasoning == "先分析"
    assert resp.content == "最终回答"
    assert resp.tool_calls == [{"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}}]
    assert resp.finish_reason == "tool_use"
    assert resp.prompt_tokens == 123
    assert resp.completion_tokens == 45
    assert resp.reasoning_signature == "sig_1"


def test_stream_accumulator_requires_completed_event_before_finalize() -> None:
    acc = StreamAccumulator()
    acc.consume(make_event("response_start", "turn-1", 1, "model", "live"))

    try:
        acc.finalize()
        assert False, "expected finalize() to reject incomplete streams"
    except RuntimeError as exc:
        assert "response_completed" in str(exc)


def test_stream_accumulator_captures_partial_state() -> None:
    acc = StreamAccumulator()
    acc.consume(make_event("response_start", "turn-1", 1, "model", "live"))
    acc.consume(make_event("thinking_delta", "turn-1", 2, "model", "live", {"text": "想法"}))
    acc.consume(make_event("content_delta", "turn-1", 3, "model", "live", {"text": "半截"}))

    snapshot = acc.snapshot()

    assert snapshot["reasoning"] == "想法"
    assert snapshot["content"] == "半截"
    assert snapshot["completed"] is False
```

- [ ] **Step 2: 运行测试，确认它们先失败**

Run: `pytest tests/test_stream_events.py -v`

Expected:
- `ModuleNotFoundError: No module named 'core.shared.stream_events'`

- [ ] **Step 3: 写最小实现，冻结事件协议面**

创建 `core/shared/stream_events.py`：

```python
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Literal


EventOrigin = Literal["model", "tool", "subagent", "system"]
SourceMode = Literal["live", "replayed"]


@dataclass(slots=True)
class StreamEvent:
    type: str
    turn_id: str
    sequence: int
    origin: EventOrigin
    source_mode: SourceMode
    timestamp: float
    payload: dict[str, Any] = field(default_factory=dict)


def make_event(
    type: str,
    turn_id: str,
    sequence: int,
    origin: EventOrigin,
    source_mode: SourceMode,
    payload: dict[str, Any] | None = None,
) -> StreamEvent:
    return StreamEvent(
        type=type,
        turn_id=turn_id,
        sequence=sequence,
        origin=origin,
        source_mode=source_mode,
        timestamp=time.time(),
        payload=dict(payload or {}),
    )


class StreamAccumulator:
    def __init__(self) -> None:
        self._reasoning_parts: list[str] = []
        self._content_parts: list[str] = []
        self._tool_calls: list[dict[str, Any]] = []
        self._finish_reason = "end_turn"
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._reasoning_signature = ""
        self._completed = False

    def consume(self, event: StreamEvent) -> None:
        if event.type == "thinking_delta":
            self._reasoning_parts.append(str(event.payload.get("text", "")))
        elif event.type == "content_delta":
            self._content_parts.append(str(event.payload.get("text", "")))
        elif event.type == "tool_call_ready":
            tool_call = event.payload.get("tool_call")
            if tool_call is not None:
                self._tool_calls.append(dict(tool_call))
        elif event.type == "response_completed":
            self._finish_reason = str(event.payload.get("finish_reason", "end_turn"))
            self._prompt_tokens = int(event.payload.get("prompt_tokens", 0))
            self._completion_tokens = int(event.payload.get("completion_tokens", 0))
            self._reasoning_signature = str(event.payload.get("reasoning_signature", ""))
            self._completed = True

    def snapshot(self) -> dict[str, Any]:
        return {
            "reasoning": "".join(self._reasoning_parts),
            "content": "".join(self._content_parts),
            "tool_calls": list(self._tool_calls),
            "completed": self._completed,
        }

    def finalize(self):
        from core.llm.response import ModelResponse

        if not self._completed:
            raise RuntimeError("response_completed event is required before finalize()")
        return ModelResponse(
            content="".join(self._content_parts),
            tool_calls=list(self._tool_calls),
            finish_reason=self._finish_reason,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            reasoning="".join(self._reasoning_parts),
            reasoning_signature=self._reasoning_signature,
        )
```

- [ ] **Step 4: 重新运行协议测试，确认通过**

Run: `pytest tests/test_stream_events.py -v`

Expected: `4 passed`

- [ ] **Step 5: 提交事件协议层**

```bash
git add core/shared/stream_events.py tests/test_stream_events.py
git commit -m "feat: add shared stream event protocol"
```

### Task 2: 为 `AnthropicClient` 和 `ModelGateway` 增加 streaming API

**Files:**
- Modify: `core/llm/client.py`
- Modify: `core/llm/anthropic_client.py`
- Modify: `tests/test_model_gateway.py`
- Modify: `tests/test_anthropic_client.py`

- [ ] **Step 1: 先写 gateway/client 的失败测试**

在 `tests/test_model_gateway.py` 追加：

```python
from core.shared.stream_events import StreamEvent


class StreamingFakeClient:
    def __init__(self):
        self.last_call = None

    def stream(self, messages, *, system="", tools=None, request_options=None, turn_id="turn-1"):
        self.last_call = {
            "messages": messages,
            "system": system,
            "tools": tools,
            "request_options": request_options,
            "turn_id": turn_id,
        }
        yield StreamEvent(
            type="response_start",
            turn_id=turn_id,
            sequence=1,
            origin="model",
            source_mode="live",
            timestamp=1.0,
            payload={},
        )


def test_model_gateway_stream_once_forwards_turn_id_and_request_options():
    client = StreamingFakeClient()
    gateway = ModelGateway(client)

    events = list(
        gateway.stream_once(
            [{"role": "user", "content": "hi"}],
            system="SYSTEM",
            tools=None,
            request_options=ModelRequestOptions(),
            turn_id="turn-42",
        )
    )

    assert len(events) == 1
    assert client.last_call["turn_id"] == "turn-42"
    assert client.last_call["system"] == "SYSTEM"
```

在 `tests/test_anthropic_client.py` 追加：

```python
from core.shared.stream_events import StreamEvent


def test_anthropic_stream_uses_messages_stream_context_manager():
    stream_ctx = MagicMock()
    stream_ctx.__enter__.return_value = iter(
        [
            MagicMock(type="message_start", message=MagicMock(usage=MagicMock(input_tokens=10, output_tokens=0))),
            MagicMock(type="content_block_delta", delta=MagicMock(type="thinking_delta", thinking="想")),
            MagicMock(type="content_block_delta", delta=MagicMock(type="text_delta", text="答")),
            MagicMock(type="message_delta", delta=MagicMock(stop_reason="end_turn"), usage=MagicMock(output_tokens=5)),
            MagicMock(type="message_stop"),
        ]
    )
    stream_ctx.__exit__.return_value = False

    client = AnthropicClient.__new__(AnthropicClient)
    client._client = MagicMock()
    client._adaptive_supported = True
    client._client.messages.stream.return_value = stream_ctx

    with patch("core.llm.anthropic_client.normalize_messages", return_value=("", [{"role": "user", "content": "hi"}])):
        events = list(client.stream([{"role": "user", "content": "hi"}], turn_id="turn-1"))

    assert [event.type for event in events] == [
        "response_start",
        "thinking_delta",
        "content_delta",
        "response_completed",
    ]
    assert events[-1].payload["finish_reason"] == "end_turn"


def test_anthropic_stream_raises_cancelled_when_cancel_check_trips():
    stream_ctx = MagicMock()
    stream_ctx.__enter__.return_value = iter([MagicMock(type="message_start", message=MagicMock(usage=MagicMock(input_tokens=1, output_tokens=0)))])
    stream_ctx.__exit__.return_value = False

    client = AnthropicClient.__new__(AnthropicClient)
    client._client = MagicMock()
    client._adaptive_supported = True
    client._client.messages.stream.return_value = stream_ctx
    client._reset_client = MagicMock()

    with patch("core.llm.anthropic_client.normalize_messages", return_value=("", [{"role": "user", "content": "hi"}])):
        try:
            list(
                client.stream(
                    [{"role": "user", "content": "hi"}],
                    turn_id="turn-1",
                    request_options=ModelRequestOptions(cancel_check=lambda: True),
                )
            )
            assert False, "expected RequestCancelledError"
        except RequestCancelledError:
            pass

    client._reset_client.assert_called_once()
```

- [ ] **Step 2: 运行测试，确认先失败**

Run:

```bash
pytest tests/test_model_gateway.py::test_model_gateway_stream_once_forwards_turn_id_and_request_options -v
pytest tests/test_anthropic_client.py::test_anthropic_stream_uses_messages_stream_context_manager -v
pytest tests/test_anthropic_client.py::test_anthropic_stream_raises_cancelled_when_cancel_check_trips -v
```

Expected:
- `AttributeError: 'ModelGateway' object has no attribute 'stream_once'`
- `AttributeError: 'AnthropicClient' object has no attribute 'stream'`

- [ ] **Step 3: 最小实现 dual-path LLM API**

在 `core/llm/client.py` 增加：

```python
from core.shared.stream_events import StreamEvent


class ModelGateway:
    ...

    def stream_once(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None,
        request_options: ModelRequestOptions | None = None,
        turn_id: str,
    ):
        if self._client is None:
            raise RuntimeError("No LLM client configured")
        if hasattr(self._client, "stream"):
            yield from self._client.stream(
                messages,
                system=system,
                tools=tools,
                request_options=request_options,
                turn_id=turn_id,
            )
            return
        raise RuntimeError("Configured client does not support stream()")
```

在 `core/llm/anthropic_client.py` 增加 `stream()`，实现骨架如下：

```python
from uuid import uuid4

from core.shared.stream_events import make_event


class AnthropicClient:
    ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        request_options: ModelRequestOptions | None = None,
        turn_id: str | None = None,
    ):
        request_options = request_options or ModelRequestOptions()
        turn_id = turn_id or uuid4().hex

        normalized_system, api_messages = normalize_messages(messages)
        full_system = "\n\n".join(part for part in [system, normalized_system] if part)
        api_messages = _sanitize_surrogates(api_messages)
        full_system = _sanitize_surrogates(full_system)

        params = {
            "model": MODEL,
            "system": full_system,
            "messages": api_messages,
            "max_tokens": request_options.max_output_tokens or MAX_TOKENS,
        }
        if tools:
            params["tools"] = tools
        if request_options.thinking_mode != "disabled":
            self._apply_thinking(params)

        sequence = 0

        def emit(type: str, payload: dict[str, Any] | None = None):
            nonlocal sequence
            sequence += 1
            return make_event(type, turn_id, sequence, "model", "live", payload)

        try:
            with self._client.messages.stream(**params) as stream:
                input_tokens = 0
                output_tokens = 0
                finish_reason = "end_turn"
                reasoning_signature = ""
                yield emit("response_start")
                for sdk_event in stream:
                    if request_options.cancel_check and request_options.cancel_check():
                        self._reset_client()
                        raise RequestCancelledError("request cancelled by user")
                    if sdk_event.type == "message_start":
                        input_tokens = int(getattr(sdk_event.message.usage, "input_tokens", 0))
                    elif sdk_event.type == "content_block_delta":
                        delta = sdk_event.delta
                        if getattr(delta, "type", "") == "thinking_delta":
                            yield emit("thinking_delta", {"text": getattr(delta, "thinking", "")})
                        elif getattr(delta, "type", "") == "text_delta":
                            yield emit("content_delta", {"text": getattr(delta, "text", "")})
                    elif sdk_event.type == "message_delta":
                        finish_reason = getattr(sdk_event.delta, "stop_reason", finish_reason) or finish_reason
                        output_tokens = int(getattr(sdk_event.usage, "output_tokens", output_tokens))
                    elif sdk_event.type == "message_stop":
                        yield emit(
                            "response_completed",
                            {
                                "finish_reason": finish_reason,
                                "prompt_tokens": input_tokens,
                                "completion_tokens": output_tokens,
                                "reasoning_signature": reasoning_signature,
                            },
                        )
        except Exception as err:
            self._raise_context_window_exceeded_if_needed(err)
            raise
```

- [ ] **Step 4: 重新运行 LLM 相关测试，确认通过**

Run:

```bash
pytest tests/test_model_gateway.py -v
pytest tests/test_anthropic_client.py -k "stream or call_" -v
```

Expected:
- `tests/test_model_gateway.py`: all pass
- `tests/test_anthropic_client.py`: streaming tests pass and existing `call()` tests remain green

- [ ] **Step 5: 提交 streaming LLM API**

```bash
git add core/llm/client.py core/llm/anthropic_client.py tests/test_model_gateway.py tests/test_anthropic_client.py
git commit -m "feat: add streaming llm gateway path"
```

### Task 3: 扩展配置、接口和 Rich renderer 的流式消费能力

**Files:**
- Modify: `core/shared/config.py`
- Modify: `core/shared/interfaces.py`
- Modify: `core/ui/renderer.py`
- Modify: `tests/test_runtime_logging.py`

- [ ] **Step 1: 先写 renderer 和配置的失败测试**

在 `tests/test_runtime_logging.py` 追加：

```python
from rich.console import Console

from core.shared.config import STREAMING_RENDER_FLUSH_MS
from core.shared.stream_events import make_event
from core.ui.renderer import RichRenderer


def test_streaming_render_flush_default_is_80ms() -> None:
    assert STREAMING_RENDER_FLUSH_MS == 80


def test_renderer_consumes_thinking_and_reply_events() -> None:
    console = Console(record=True, force_terminal=False, width=120)
    renderer = RichRenderer(console=console)

    renderer.begin_stream("turn-1", {})
    renderer.consume_event(make_event("thinking_delta", "turn-1", 1, "model", "live", {"text": "先想"}))
    renderer.consume_event(make_event("content_delta", "turn-1", 2, "model", "live", {"text": "再答"}))
    renderer.end_stream("turn-1", {})

    output = console.export_text()
    assert "思考 先想" in output
    assert "回复 再答" in output


def test_renderer_flushes_text_before_tool_event() -> None:
    console = Console(record=True, force_terminal=False, width=120)
    renderer = RichRenderer(console=console)

    renderer.begin_stream("turn-1", {})
    renderer.consume_event(make_event("content_delta", "turn-1", 1, "model", "live", {"text": "正在回答"}))
    renderer.consume_event(
        make_event(
            "tool_call_start",
            "turn-1",
            2,
            "tool",
            "live",
            {"tool_name": "read_file", "tool_args": {"path": "README.md"}},
        )
    )
    renderer.end_stream("turn-1", {})

    output = console.export_text()
    assert output.index("回复 正在回答") < output.index("$ Read(")
```

- [ ] **Step 2: 运行测试，确认先失败**

Run: `pytest tests/test_runtime_logging.py -k "streaming_render_flush_default_is_80ms or renderer_consumes_thinking_and_reply_events or renderer_flushes_text_before_tool_event" -v`

Expected:
- `ImportError` for `STREAMING_RENDER_FLUSH_MS`
- `AttributeError` for missing `begin_stream` / `consume_event` / `end_stream`

- [ ] **Step 3: 实现配置与 renderer 增量 API**

在 `core/shared/config.py` 增加：

```python
STREAMING_ENABLED: bool = os.environ.get("STREAMING_ENABLED", "true").lower() in ("true", "1", "yes")
STREAMING_THINKING_ENABLED: bool = os.environ.get("STREAMING_THINKING_ENABLED", "true").lower() in ("true", "1", "yes")
STREAMING_RENDER_FLUSH_MS: int = int(os.environ.get("STREAMING_RENDER_FLUSH_MS", "80"))
SUBAGENT_STREAMING_MODE: str = os.environ.get("SUBAGENT_STREAMING_MODE", "replayed")
PERSIST_PARTIAL_STREAM_OUTPUT: bool = os.environ.get("PERSIST_PARTIAL_STREAM_OUTPUT", "false").lower() in ("true", "1", "yes")
```

在 `core/shared/interfaces.py` 扩展 renderer 协议：

```python
from core.llm.client import ModelRequestOptions
from core.shared.stream_events import StreamEvent


class LLMClient(Protocol):
    ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        request_options: ModelRequestOptions | None = None,
        turn_id: str | None = None,
    ) -> Any:
        ...


class Renderer(Protocol):
    ...

    def begin_stream(self, turn_id: str, meta: dict[str, Any]) -> None:
        ...

    def consume_event(self, event: StreamEvent) -> None:
        ...

    def end_stream(self, turn_id: str, result_meta: dict[str, Any]) -> None:
        ...
```

在 `core/ui/renderer.py` 为 `RichRenderer` 增加最小流式状态：

```python
import time

from core.shared.config import STREAMING_RENDER_FLUSH_MS, STREAMING_THINKING_ENABLED
from core.shared.stream_events import StreamEvent


class RichRenderer:
    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._stream_turn_id: str | None = None
        self._thinking_buffer: list[str] = []
        self._content_buffer: list[str] = []
        self._last_flush_at = time.monotonic()

    def begin_stream(self, turn_id: str, meta: dict[str, Any]) -> None:
        self._stream_turn_id = turn_id
        self._thinking_buffer = []
        self._content_buffer = []
        self._last_flush_at = time.monotonic()

    def _flush_stream_buffers(self, *, force: bool = False) -> None:
        elapsed_ms = (time.monotonic() - self._last_flush_at) * 1000
        if not force and elapsed_ms < STREAMING_RENDER_FLUSH_MS:
            return
        if self._thinking_buffer and STREAMING_THINKING_ENABLED:
            self._console.print(f"[dim]思考 {''.join(self._thinking_buffer)}[/dim]")
            self._thinking_buffer = []
        if self._content_buffer:
            self._console.print(f"回复 {''.join(self._content_buffer)}")
            self._content_buffer = []
        self._last_flush_at = time.monotonic()

    def consume_event(self, event: StreamEvent) -> None:
        if event.type == "thinking_delta":
            self._thinking_buffer.append(str(event.payload.get("text", "")))
            self._flush_stream_buffers()
            return
        if event.type == "content_delta":
            self._content_buffer.append(str(event.payload.get("text", "")))
            self._flush_stream_buffers()
            return
        self._flush_stream_buffers(force=True)
        if event.type == "tool_call_start":
            self.show_tool_call(event.payload["tool_name"], event.payload.get("tool_args", {}))
        elif event.type == "tool_call_result":
            self.show_tool_result(event.payload["tool_name"], event.payload.get("content", ""))
        elif event.type == "status":
            self.show_status(str(event.payload.get("message", "")))

    def end_stream(self, turn_id: str, result_meta: dict[str, Any]) -> None:
        self._flush_stream_buffers(force=True)
        self._stream_turn_id = None
```

同时让 `QuietRenderer` 补空实现：

```python
def begin_stream(self, turn_id: str, meta: dict[str, Any]) -> None:
    pass

def consume_event(self, event) -> None:
    pass

def end_stream(self, turn_id: str, result_meta: dict[str, Any]) -> None:
    pass
```

- [ ] **Step 4: 重新运行 renderer 测试，确认通过**

Run: `pytest tests/test_runtime_logging.py -k "streaming_render_flush_default_is_80ms or renderer_consumes_thinking_and_reply_events or renderer_flushes_text_before_tool_event" -v`

Expected: `3 passed`

- [ ] **Step 5: 提交流式 renderer 基础设施**

```bash
git add core/shared/config.py core/shared/interfaces.py core/ui/renderer.py tests/test_runtime_logging.py
git commit -m "feat: add streaming renderer interface"
```

### Task 4: 给 `QueryLoop` 增加 streaming 主分支和 partial-output 策略

**Files:**
- Modify: `core/query/state.py`
- Modify: `core/query/loop.py`
- Modify: `tests/test_query_logging.py`
- Modify: `tests/test_query_display.py`

- [ ] **Step 1: 先写 QueryLoop streaming 路径的失败测试**

在 `tests/test_query_logging.py` 追加：

```python
from core.shared.stream_events import make_event


class FakeStreamingRenderer(FakeRenderer):
    def __init__(self) -> None:
        super().__init__()
        self.begin_calls: list[str] = []
        self.event_types: list[str] = []
        self.end_calls: list[str] = []

    def begin_stream(self, turn_id: str, meta: dict[str, object]) -> None:
        self.begin_calls.append(turn_id)

    def consume_event(self, event) -> None:
        self.event_types.append(event.type)

    def end_stream(self, turn_id: str, result_meta: dict[str, object]) -> None:
        self.end_calls.append(turn_id)


class FakeStreamingGateway:
    def stream_once(self, messages, *, system="", tools=None, request_options=None, turn_id: str):
        yield make_event("response_start", turn_id, 1, "model", "live")
        yield make_event("thinking_delta", turn_id, 2, "model", "live", {"text": "先分析"})
        yield make_event("content_delta", turn_id, 3, "model", "live", {"text": "最终回答"})
        yield make_event(
            "response_completed",
            turn_id,
            4,
            "model",
            "live",
            {
                "finish_reason": "end_turn",
                "prompt_tokens": 123,
                "completion_tokens": 45,
                "reasoning_signature": "sig_1",
            },
        )


def test_query_loop_streams_main_agent_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr("core.query.loop.STREAMING_ENABLED", True)
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)
    renderer = FakeStreamingRenderer()

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeStreamingGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert renderer.begin_calls and renderer.end_calls
    assert renderer.event_types == ["response_start", "thinking_delta", "content_delta", "response_completed"]
    assert session_state.conversation_messages[-1]["role"] == "assistant"
    assert session_state.conversation_messages[-1]["content"] == "最终回答"


def test_query_loop_uses_batch_path_when_streaming_disabled(monkeypatch) -> None:
    monkeypatch.setattr("core.query.loop.STREAMING_ENABLED", False)
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=FakePromptAssembler(),
        model_gateway=FakeModelGateway(),
        tool_runtime=object(),
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
        renderer=FakeRenderer(),
    )

    assert result.stop_reason == StopReason.COMPLETED
```

在 `tests/test_query_display.py` 追加：

```python
def test_streaming_tool_turn_keeps_fallback_text_out_of_transcript(monkeypatch) -> None:
    from core.shared.stream_events import make_event

    monkeypatch.setattr("core.query.loop.STREAMING_ENABLED", True)
    session_state = SessionState(conversation_messages=[])
    store = SessionStore(session_state)

    class Gateway:
        def stream_once(self, messages, *, system="", tools=None, request_options=None, turn_id: str):
            yield make_event("response_start", turn_id, 1, "model", "live")
            yield make_event(
                "tool_call_ready",
                turn_id,
                2,
                "model",
                "live",
                {"tool_call": {"id": "toolu_1", "name": "read_file", "args": {"path": "README.md"}}},
            )
            yield make_event(
                "response_completed",
                turn_id,
                3,
                "model",
                "live",
                {"finish_reason": "tool_use", "prompt_tokens": 10, "completion_tokens": 5},
            )
            yield make_event("response_start", turn_id, 4, "model", "live")
            yield make_event("content_delta", turn_id, 5, "model", "live", {"text": "完成"})
            yield make_event(
                "response_completed",
                turn_id,
                6,
                "model",
                "live",
                {"finish_reason": "end_turn", "prompt_tokens": 10, "completion_tokens": 5},
            )

    renderer = FakeRenderer()
    runtime = FakeToolRuntime([_success_batch("read_file")])

    result = QueryLoop().run(
        session_state=session_state,
        store=store,
        view_builder=FakeViewBuilder(),
        prompt_assembler=FakePromptAssembler(),
        model_gateway=Gateway(),
        tool_runtime=runtime,
        tool_context=object(),
        policy_runner=FakePolicyRunner(),
        recovery=FakeRecovery(),
        governor=FakeGovernor(),
        offloader=FakeOffloader(),
        renderer=renderer,
    )

    assert result.stop_reason == StopReason.COMPLETED
    assert all(msg.get("content") != "先读取 README.md。" for msg in session_state.conversation_messages if msg.get("role") == "assistant")
```

- [ ] **Step 2: 运行 streaming QueryLoop 测试，确认先失败**

Run:

```bash
pytest tests/test_query_logging.py::test_query_loop_streams_main_agent_when_enabled -v
pytest tests/test_query_logging.py::test_query_loop_uses_batch_path_when_streaming_disabled -v
pytest tests/test_query_display.py::test_streaming_tool_turn_keeps_fallback_text_out_of_transcript -v
```

Expected:
- `AttributeError` or `TypeError` because `QueryLoop` 还不会调用 `stream_once()`

- [ ] **Step 3: 实现 streaming 主分支与 partial tradeoff**

在 `core/query/state.py` 增加流式字段：

```python
    current_turn_id: str | None = None
    current_response_streaming: bool = False
    current_thinking_visible: bool = False
    current_content_visible: bool = False
    last_stream_sequence: int = 0
    stream_source_mode: str | None = None
```

在 `core/query/loop.py`：

```python
from uuid import uuid4

from core.shared.config import PERSIST_PARTIAL_STREAM_OUTPUT, STREAMING_ENABLED
from core.shared.stream_events import StreamAccumulator
```

在模型调用部分替换为显式分支：

```python
if STREAMING_ENABLED and hasattr(model_gateway, "stream_once"):
    turn_id = uuid4().hex
    accumulator = StreamAccumulator()
    state.current_turn_id = turn_id
    state.current_response_streaming = True
    if renderer is not None and hasattr(renderer, "begin_stream"):
        renderer.begin_stream(turn_id, {"source": "main_loop"})
    try:
        for event in model_gateway.stream_once(
            view.messages,
            system=view.system,
            tools=active_tools,
            request_options=request_options,
            turn_id=turn_id,
        ):
            state.last_stream_sequence = event.sequence
            state.stream_source_mode = event.source_mode
            if event.type == "thinking_delta":
                state.current_thinking_visible = True
            elif event.type == "content_delta":
                state.current_content_visible = True
            accumulator.consume(event)
            if renderer is not None and hasattr(renderer, "consume_event"):
                renderer.consume_event(event)
        model_resp = accumulator.finalize()
    except RequestCancelledError:
        if renderer is not None and hasattr(renderer, "show_status"):
            renderer.show_status("已取消当前运行。")
        return QueryResult(
            final_output="已取消当前运行。",
            stop_reason=StopReason.ABORTED,
            success=False,
            turns_used=state.turn_count,
            tool_calls_executed=state.tool_calls_executed,
            files_modified=state.files_modified,
        )
    except Exception:
        snapshot = accumulator.snapshot()
        if PERSIST_PARTIAL_STREAM_OUTPUT and snapshot["content"]:
            store.append({"role": "assistant", "content": snapshot["content"], "incomplete": True})
        raise
    finally:
        state.current_response_streaming = False
        if renderer is not None and hasattr(renderer, "end_stream"):
            renderer.end_stream(turn_id, {"source_mode": state.stream_source_mode})
else:
    model_resp = model_gateway.call_once(
        view.messages,
        system=view.system,
        tools=active_tools,
        request_options=request_options,
    )
```

保留以下逻辑不变：

```python
store.append(model_resp.to_message())
prompt_tokens = getattr(model_resp, "prompt_tokens", None)
if isinstance(prompt_tokens, int):
    session_state.compact_state["last_prompt_tokens"] = prompt_tokens
```

- [ ] **Step 4: 重新运行 QueryLoop 测试，确认通过**

Run:

```bash
pytest tests/test_query_logging.py -k "streams_main_agent_when_enabled or uses_batch_path_when_streaming_disabled" -v
pytest tests/test_query_display.py -k "streaming_tool_turn_keeps_fallback_text_out_of_transcript or shows_assistant_update_for_tool_turn" -v
```

Expected:
- streaming path tests pass
- existing non-stream display tests remain green

- [ ] **Step 5: 提交 QueryLoop streaming 主分支**

```bash
git add core/query/state.py core/query/loop.py tests/test_query_logging.py tests/test_query_display.py
git commit -m "feat: stream main query loop responses"
```

### Task 5: 让 `ToolExecutorRuntime` 发出实时事件并保持 transcript 顺序稳定

**Files:**
- Modify: `core/tools/runtime.py`
- Modify: `tests/test_tool_runtime.py`

- [ ] **Step 1: 先写 tool runtime 事件顺序测试**

在 `tests/test_tool_runtime.py` 追加：

```python
from core.shared.stream_events import StreamEvent


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def __call__(self, event: StreamEvent) -> None:
        tool_name = str(event.payload.get("tool_name", ""))
        content = str(event.payload.get("content", ""))
        self.events.append((event.type, tool_name, content))


def test_parallel_tools_emit_start_in_schedule_order_and_result_in_completion_order(tmp_path) -> None:
    registry = ToolRegistry()

    class SlowA:
        SCHEMA = {"name": "find_a", "input_schema": {"type": "object", "properties": {}}}
        READONLY = True
        ANNOTATIONS = {"readonly": True, "destructive": False, "idempotent": True, "concurrency_safe": True}

        @staticmethod
        def handle(args, context):
            import time
            time.sleep(0.02)
            return ToolInvocationOutcome(messages=[make_tool_message(context, "A")])

    class FastB:
        SCHEMA = {"name": "find_b", "input_schema": {"type": "object", "properties": {}}}
        READONLY = True
        ANNOTATIONS = {"readonly": True, "destructive": False, "idempotent": True, "concurrency_safe": True}

        @staticmethod
        def handle(args, context):
            return ToolInvocationOutcome(messages=[make_tool_message(context, "B")])

    registry.register(SlowA)
    registry.register(FastB)

    context = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    state = SessionState(conversation_messages=[])
    context.bind_runtime(session_state=state, skill_registry=None)
    recorder = EventRecorder()
    runtime = ToolExecutorRuntime(registry, context, event_sink=recorder)

    batch = runtime.execute_batch(
        [
            ToolCall(idx=0, name="find_a", call_id="call_0", args={}),
            ToolCall(idx=1, name="find_b", call_id="call_1", args={}),
        ],
        run_state=RunState(),
        apply_session_update=lambda update: apply_session_update(state, update),
        apply_run_update=apply_run_update,
    )

    assert recorder.events[0][:2] == ("tool_call_start", "find_a")
    assert recorder.events[1][:2] == ("tool_call_start", "find_b")
    assert recorder.events[2][:2] == ("tool_call_result", "find_b")
    assert recorder.events[3][:2] == ("tool_call_result", "find_a")
    assert [msg["tool_call_id"] for msg in batch.messages] == ["call_0", "call_1"]
```

- [ ] **Step 2: 运行测试，确认先失败**

Run: `pytest tests/test_tool_runtime.py::test_parallel_tools_emit_start_in_schedule_order_and_result_in_completion_order -v`

Expected:
- `TypeError: ToolExecutorRuntime.__init__() got an unexpected keyword argument 'event_sink'`

- [ ] **Step 3: 实现 runtime 事件发射，不改变 transcript 权威**

在 `core/tools/runtime.py` 修改构造函数和 helper：

```python
from core.shared.stream_events import make_event


class ToolExecutorRuntime:
    def __init__(self, registry, context, display: RunDisplayOptions | None = None, renderer=None, event_sink=None):
        self._registry = registry
        self._context = context
        self._display = display or RunDisplayOptions()
        self._renderer = renderer
        self._event_sink = event_sink
        self._active_run_state = None

    def _emit_tool_event(self, type: str, *, tool_name: str, tool_args=None, content: str = "", source_mode: str = "live") -> None:
        if self._event_sink is None:
            return
        turn_id = getattr(self._active_run_state, "current_turn_id", None) or "tool-turn"
        sequence = getattr(self._active_run_state, "last_stream_sequence", 0) + 1
        if self._active_run_state is not None:
            self._active_run_state.last_stream_sequence = sequence
        self._event_sink(
            make_event(
                type=type,
                turn_id=turn_id,
                sequence=sequence,
                origin="tool",
                source_mode=source_mode,
                payload={
                    "tool_name": tool_name,
                    "tool_args": dict(tool_args or {}),
                    "content": content,
                },
            )
        )
```

让 start/result 同时走 event sink：

```python
    def execute_batch(...):
        self._active_run_state = run_state
        try:
            ...
        finally:
            self._active_run_state = None

    def _render_tool_call(self, call: ToolCall) -> None:
        self._emit_tool_event("tool_call_start", tool_name=call.name, tool_args=call.args)
        if self._renderer is None or self._display.quiet:
            return
        ...

    def _render_tool_result(self, call: ToolCall, outcome: ToolInvocationOutcome) -> None:
        preview = self._first_content(self._truncate_first_message(outcome))
        self._emit_tool_event("tool_call_result", tool_name=call.name, tool_args=call.args, content=preview)
        if self._renderer is None or self._display.quiet:
            return
        ...
```

注意只在主线程 emit：

```python
for future in as_completed(futures):
    call = futures[future]
    ...
    results[call.idx] = result
    self._render_tool_result(call, result)
```

这样 event sink 不会从 worker thread 直接被调用。

- [ ] **Step 4: 重新运行 tool runtime 测试，确认通过**

Run: `pytest tests/test_tool_runtime.py -v`

Expected:
- 新增并发事件测试通过
- 既有 `show_tool_call/show_tool_result` 顺序测试仍通过

- [ ] **Step 5: 提交 tool runtime 事件化**

```bash
git add core/tools/runtime.py tests/test_tool_runtime.py
git commit -m "feat: emit live tool runtime events"
```

### Task 6: 升级 `SubagentRuntime` bridge，支持 live/replayed 统一协议

**Files:**
- Modify: `core/session/subagent.py`
- Modify: `tests/session/test_subagent_runtime.py`

- [ ] **Step 1: 先写 subagent bridge 的失败测试**

在 `tests/session/test_subagent_runtime.py` 追加：

```python
from core.shared.stream_events import make_event


def test_subagent_bridge_forwards_consume_event_with_subagent_prefix() -> None:
    events = []
    bridge = SubagentBridgeRenderer(task_id="task-1", agent_type="general", emit=events.append)

    bridge.consume_event(
        make_event(
            "content_delta",
            "turn-child",
            1,
            "model",
            "live",
            {"text": "子代理输出"},
        )
    )

    assert events == [
        {
            "event": "subagent_content_delta",
            "task_id": "task-1",
            "agent_type": "general",
            "source_mode": "live",
            "content": "子代理输出",
        }
    ]


def test_subagent_bridge_legacy_show_assistant_becomes_replayed_event() -> None:
    events = []
    bridge = SubagentBridgeRenderer(task_id="task-1", agent_type="general", emit=events.append)

    bridge.show_assistant("完整输出")

    assert events == [
        {
            "event": "subagent_content_delta",
            "task_id": "task-1",
            "agent_type": "general",
            "source_mode": "replayed",
            "content": "完整输出",
        }
    ]


def test_existing_subagent_runtime_tool_event_name_uses_start_suffix(tmp_path) -> None:
    parent = ToolUseContext(working_dir=str(tmp_path), max_turns=10)
    parent.bind_runtime(session_state=SessionState(conversation_messages=[]), skill_registry=None)
    runtime = SubagentRuntime(parent_context=parent)
    packet = TaskPacket(task_id="task-1", title="Inspect runtime", directive="Fix runtime", agent_type="general")
    request = SubagentRequest(task_packet=packet, agent_type=SubagentType.GENERAL)
    events = []

    class FakeEngine:
        def __init__(self, **kwargs):
            self.state = SessionState(conversation_messages=[])
            self._renderer = kwargs["renderer"]

        def submit_user_message(self, prompt):
            self._renderer.show_tool_call("find", {"pattern": "*.py"})
            self._renderer.show_tool_result("find", "core/tasks/models.py")
            return QueryResult(final_output="done", stop_reason=StopReason.COMPLETED, success=True, turns_used=1)

    with patch("core.session.subagent.SessionEngine", FakeEngine):
        runtime.run(request, emit=events.append)

    assert events[1]["event"] == "subagent_tool_call_start"
```

- [ ] **Step 2: 运行测试，确认先失败**

Run: `pytest tests/session/test_subagent_runtime.py -k "forwards_consume_event_with_subagent_prefix or legacy_show_assistant_becomes_replayed_event" -v`

Expected:
- `AttributeError: 'SubagentBridgeRenderer' object has no attribute 'consume_event'`

- [ ] **Step 3: 实现 bridge 的双通道适配**

在 `core/session/subagent.py` 中重写 `SubagentBridgeRenderer`：

```python
from core.shared.stream_events import StreamEvent


class SubagentBridgeRenderer:
    ...

    def _emit_replayed(self, event: str, *, content: str = "", tool_name: str = "", tool_args=None) -> None:
        payload = {
            "event": event,
            "task_id": self._task_id,
            "agent_type": self._agent_type,
            "source_mode": "replayed",
        }
        if content:
            payload["content"] = content
        if tool_name:
            payload["tool_name"] = tool_name
        if tool_args:
            payload["tool_args"] = dict(tool_args)
        self._emit(payload)

    def consume_event(self, event: StreamEvent) -> None:
        mapping = {
            "thinking_delta": "subagent_thinking_delta",
            "content_delta": "subagent_content_delta",
            "tool_call_start": "subagent_tool_call_start",
            "tool_call_result": "subagent_tool_call_result",
            "status": "subagent_status",
        }
        mapped = mapping.get(event.type)
        if mapped is None:
            return
        payload = {
            "event": mapped,
            "task_id": self._task_id,
            "agent_type": self._agent_type,
            "source_mode": event.source_mode,
        }
        if "text" in event.payload:
            payload["content"] = event.payload["text"]
        if "content" in event.payload:
            payload["content"] = event.payload["content"]
        if "tool_name" in event.payload:
            payload["tool_name"] = event.payload["tool_name"]
        if "tool_args" in event.payload:
            payload["tool_args"] = dict(event.payload["tool_args"])
        self._emit(payload)

    def show_assistant(self, content: str | None) -> None:
        if content:
            self._emit_replayed("subagent_content_delta", content=content)

    def show_thinking(self, title: str, reasoning: str) -> None:
        self._emit_replayed("subagent_thinking_delta", content=reasoning)

    def show_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._emit_replayed("subagent_tool_call_start", tool_name=name, tool_args=args)

    def show_tool_result(self, name: str, output: str) -> None:
        self._emit_replayed("subagent_tool_call_result", tool_name=name, content=output)
```

同时在 `SubagentRuntime.run()` 中保留：

```python
child_display = RunDisplayOptions(quiet=emit is None)
...
renderer=bridge,
```

这样 child engine streaming path 会优先走 `consume_event()`，legacy path 会回退到 `show_*()`。

- [ ] **Step 4: 重新运行 subagent 测试，确认通过**

Run: `pytest tests/session/test_subagent_runtime.py -v`

Expected:
- bridge live/replayed tests pass
- 既有 `subagent_start` / `subagent_done` / tools passed 测试仍通过

- [ ] **Step 5: 提交 subagent bridge 升级**

```bash
git add core/session/subagent.py tests/session/test_subagent_runtime.py
git commit -m "feat: unify subagent bridge streaming events"
```

### Task 7: 全链路回归与 implementation signoff

**Files:**
- Modify: `docs/superpowers/plans/2026-05-13-streaming-query-runtime-implementation.md`

- [ ] **Step 1: 跑最小回归矩阵**

Run:

```bash
pytest tests/test_stream_events.py -v
pytest tests/test_model_gateway.py -v
pytest tests/test_anthropic_client.py -k "stream or call_" -v
pytest tests/test_query_logging.py -v
pytest tests/test_query_display.py -v
pytest tests/test_runtime_logging.py -v
pytest tests/test_tool_runtime.py -v
pytest tests/session/test_subagent_runtime.py -v
```

Expected:
- 全部 PASS
- 无新增 flaky failure

- [ ] **Step 2: 做一次手工 smoke checklist**

按以下清单手工验证：

```text
1. 启动 REPL，输入一个会触发较长 thinking 的问题
2. 确认终端先出现浅色“思考 ...”，再出现“回复 ...”
3. 确认包含工具调用的问题会在正文之间插入 `$ Tool(...)`
4. 确认 subagent 任务会显示 `[子代理 ...]` 事件，而不是完全静默
5. 确认 STREAMING_ENABLED=false 时仍能正常批响应
6. 确认中断请求不会把半截 assistant message 写入 transcript（默认配置）
```

- [ ] **Step 3: 在 plan 文档中勾选已完成项并记录实际回归命令**

把本文档顶部追加一段执行记录：

```markdown
## Execution Notes

- Implemented in workspace: `/Users/kino/works/kino/harness`
- Final regression command set:
  - `pytest tests/test_stream_events.py -v`
  - `pytest tests/test_model_gateway.py -v`
  - `pytest tests/test_query_logging.py -v`
  - `pytest tests/test_tool_runtime.py -v`
```

- [ ] **Step 4: 提交 implementation signoff**

```bash
git add docs/superpowers/plans/2026-05-13-streaming-query-runtime-implementation.md
git commit -m "docs: record streaming runtime implementation plan"
```
