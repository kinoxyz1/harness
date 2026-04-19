from __future__ import annotations

from types import SimpleNamespace

from core.llm.client import ModelGateway


class FakeClient:
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


def test_model_gateway_raises_without_client():
    gateway = ModelGateway(None)
    try:
        gateway.call_once([{"role": "user", "content": "hi"}], tools=None)
        assert False, "Should have raised"
    except RuntimeError as e:
        assert "No LLM client" in str(e)
