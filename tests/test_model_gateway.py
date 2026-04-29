from __future__ import annotations

from types import SimpleNamespace

from core.llm.client import ModelGateway, ModelRequestOptions


class FakeClient:
    def __init__(self):
        self.last_call = None

    def call(self, messages, *, system="", tools=None, request_options=None):
        self.last_call = {
            "messages": messages,
            "system": system,
            "tools": tools,
            "request_options": request_options,
        }
        return SimpleNamespace(
            content="answer",
            tool_calls=[],
            finish_reason="end_turn",
            prompt_tokens=10,
            completion_tokens=20,
            reasoning="step by step",
            reasoning_signature="sig_123",
        )


class LegacyFakeClient:
    def __init__(self):
        self.last_call = None

    def call(self, messages, *, system="", tools=None):
        self.last_call = {"messages": messages, "system": system, "tools": tools}
        return SimpleNamespace(
            content="answer",
            tool_calls=[],
            finish_reason="end_turn",
            prompt_tokens=10,
            completion_tokens=20,
            reasoning="step by step",
            reasoning_signature="sig_123",
        )


def test_model_gateway_forwards_system_parameter():
    client = FakeClient()
    gateway = ModelGateway(client)
    response = gateway.call_once(
        [{"role": "user", "content": "hi"}],
        system="SYSTEM",
        tools=None,
    )
    assert client.last_call["system"] == "SYSTEM"
    assert response.content == "answer"


def test_model_gateway_defaults_system_to_empty():
    client = FakeClient()
    gateway = ModelGateway(client)
    gateway.call_once(
        [{"role": "user", "content": "hi"}],
        tools=None,
    )
    assert client.last_call["system"] == ""


def test_model_gateway_forwards_tools():
    client = FakeClient()
    gateway = ModelGateway(client)
    tools = [{"name": "read_file"}]
    gateway.call_once(
        [{"role": "user", "content": "hi"}],
        system="S",
        tools=tools,
    )
    assert client.last_call["tools"] == tools


def test_model_gateway_forwards_request_options():
    client = FakeClient()
    gateway = ModelGateway(client)
    request_options = ModelRequestOptions(
        query_source="summary",
        max_output_tokens=123,
        thinking_mode="disabled",
    )
    gateway.call_once(
        [{"role": "user", "content": "hi"}],
        system="S",
        tools=None,
        request_options=request_options,
    )
    assert client.last_call["request_options"] is request_options


def test_model_gateway_preserves_legacy_client_signature_by_default():
    client = LegacyFakeClient()
    gateway = ModelGateway(client)
    response = gateway.call_once(
        [{"role": "user", "content": "hi"}],
        system="SYSTEM",
        tools=None,
    )
    assert client.last_call == {
        "messages": [{"role": "user", "content": "hi"}],
        "system": "SYSTEM",
        "tools": None,
    }
    assert response.content == "answer"


def test_model_gateway_raises_without_client():
    gateway = ModelGateway(None)
    try:
        gateway.call_once([{"role": "user", "content": "hi"}], tools=None)
        assert False, "Should have raised"
    except RuntimeError as e:
        assert "No LLM client" in str(e)
