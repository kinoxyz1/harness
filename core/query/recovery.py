from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class RecoveryDecision:
    should_continue: bool
    follow_up_messages: list[dict[str, str]] = field(default_factory=list)


class RecoveryManager:
    def handle(self, model_resp, state) -> RecoveryDecision:
        if model_resp.finish_reason == "length":
            return RecoveryDecision(
                should_continue=True,
                follow_up_messages=[{"role": "user", "content": "请继续输出。"}],
            )
        if not model_resp.has_final_text:
            return RecoveryDecision(
                should_continue=True,
                follow_up_messages=[{"role": "user", "content": "请直接给出最终答复。"}],
            )
        return RecoveryDecision(should_continue=False)
