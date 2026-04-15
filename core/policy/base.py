from __future__ import annotations

from typing import Protocol


class RunPolicy(Protocol):
    def before_model_call(self, context, state) -> list[dict[str, str]]:
        raise NotImplementedError

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        raise NotImplementedError

    def should_stop(self, context, state) -> str | None:
        raise NotImplementedError


class PolicyRunner:
    def __init__(self, policies: list[RunPolicy]):
        self._policies = policies

    def before_model_call(self, context, state) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for policy in self._policies:
            messages.extend(policy.before_model_call(context, state))
        return messages

    def after_tool_batch(self, context, state, batch_result) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for policy in self._policies:
            messages.extend(policy.after_tool_batch(context, state, batch_result))
        return messages

    def should_stop(self, context, state) -> str | None:
        for policy in self._policies:
            decision = policy.should_stop(context, state)
            if decision is not None:
                return decision
        return None
