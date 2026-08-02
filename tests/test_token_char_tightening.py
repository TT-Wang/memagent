"""Regression coverage for token-deficit to character-budget tightening."""

from types import SimpleNamespace

import sliceagent_core.loop as loop


class _CapacityPlan:
    system = "system"
    media_parts = ()
    blocks = ()
    last_request_copies = 0

    def __init__(self) -> None:
        self.last_selection = SimpleNamespace(used_chars=0)

    def project(self, capacity: int | None = None) -> list[dict]:
        assert capacity is not None
        self.last_selection = SimpleNamespace(used_chars=capacity)
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": "x" * capacity},
        ]

    def _fixed_user_chars(self, _request_copies: int) -> int:
        return 0


def _report_for(messages: list[dict], *, injected: bool = False) -> SimpleNamespace:
    user_chars = len(messages[1]["content"])
    has_injection = any(message.get("content") == "hook" for message in messages)
    overflowing = user_chars > 60 and (not injected or has_injection)
    return SimpleNamespace(required_tokens=110 if overflowing else 100, context_window=100)


def test_seed_projection_converts_token_deficit_to_character_budget(monkeypatch) -> None:
    plan = _CapacityPlan()
    monkeypatch.setattr(loop, "available_content_capacity", lambda *_args: 100)
    monkeypatch.setattr(loop, "estimate_model_call", lambda _llm, messages, _schemas: _report_for(messages))

    projected = loop._project_request_seed(plan, [], object(), [])

    assert len(projected[1]["content"]) <= 60


def test_post_hook_tightening_converts_token_deficit_to_character_budget(monkeypatch) -> None:
    plan = _CapacityPlan()
    monkeypatch.setattr(loop, "available_content_capacity", lambda *_args: 100)
    monkeypatch.setattr(
        loop,
        "estimate_model_call",
        lambda _llm, messages, _schemas: _report_for(messages, injected=True),
    )

    projected, prepared = loop._prepare_model_messages(
        seed_plan=plan,
        trajectory=[],
        messages=[],
        llm=object(),
        schemas=[],
        prepare=lambda messages: [*messages, {"role": "user", "content": "hook"}],
    )

    assert len(projected[1]["content"]) <= 60
    assert prepared[-1]["content"] == "hook"
