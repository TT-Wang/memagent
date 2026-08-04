from __future__ import annotations

from sliceagent_cli.promptlayer_observer import PromptLayerObserver, make_promptlayer_observer
from sliceagent_core.interfaces import AssistantMessage, ToolCall


def _record(*, response=None, error=None):
    return {
        "attempt": 2,
        "started_at": "2026-08-04T08:00:00+00:00",
        "ended_at": "2026-08-04T08:00:01+00:00",
        "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com/v1",
        "reasoning": "fast",
        "messages": [{"role": "user", "content": "private source secret-needle"}],
        "schemas": [{"type": "function", "function": {
            "name": "read_file", "description": "private schema", "parameters": {"type": "object"},
        }}],
        "response": response,
        "error": error,
    }


def test_metadata_mode_logs_tokens_cost_and_digests_without_content(monkeypatch):
    sent = []
    monkeypatch.setenv("AGENT_EXPERIMENTAL_RESULT_ALIAS", "1")
    response = AssistantMessage(
        content="private answer secret-output",
        tool_calls=[ToolCall(id="call-1", name="read_file", args={"path": "/private/a.py"})],
        usage={
            "prompt_tokens": 120, "completion_tokens": 10,
            "input_other": 20, "input_cache_read": 100, "input_cache_creation": 0, "output": 10,
        },
        finish_reason="tool_calls",
    )
    observer = PromptLayerObserver(
        api_key="pl_test_key", session_id="session-private", workspace_root=lambda: "/private/repo",
        content_mode="metadata", tags=("team:test",), sender=lambda payload: sent.append(payload) or {"id": 41},
    )

    observer(_record(response=response))
    stats = observer.close()

    assert stats["sent"] == 1 and stats["last_request_id"] == 41
    payload = sent[0]
    encoded = repr(payload)
    for secret in ("secret-needle", "secret-output", "/private/a.py", "/private/repo", "session-private"):
        assert secret not in encoded
    assert payload["provider"] == "openai" and payload["model"] == "deepseek-v4-flash"
    assert payload["metadata"]["sliceagent_provider_route"] == "deepseek"
    assert payload["input_tokens"] == 120 and payload["output_tokens"] == 10
    assert payload["price"] > 0
    assert "experiment:result_alias" in payload["tags"]
    assert payload["metadata"]["sliceagent_input_hmac_sha256"]
    assert payload["metadata"]["sliceagent_message_roles"] == "user"
    assert payload["metadata"]["sliceagent_tool_names"] == "read_file"


def test_full_mode_is_an_explicit_exact_content_path():
    sent = []
    response = AssistantMessage(content="exact output", tool_calls=[], usage={}, finish_reason="stop")
    observer = PromptLayerObserver(
        api_key="pl_test_key", session_id="s", workspace_root=lambda: "/repo",
        content_mode="full", sender=lambda payload: sent.append(payload) or {"request_id": 7},
    )

    observer(_record(response=response))
    stats = observer.close()

    assert stats["sent"] == 1 and stats["last_request_id"] == 7
    assert "private source secret-needle" in repr(sent[0]["input"])
    assert "private schema" in repr(sent[0]["input"])
    assert "exact output" in repr(sent[0]["output"])


def test_error_body_is_never_uploaded():
    sent = []
    observer = PromptLayerObserver(
        api_key="pl_test_key", session_id="s", workspace_root=lambda: "/repo",
        sender=lambda payload: sent.append(payload) or {"id": 9},
    )

    observer(_record(error=TimeoutError("secret provider echo")))
    observer.close()

    payload = sent[0]
    assert payload["status"] == "ERROR" and payload["error_type"] == "PROVIDER_TIMEOUT"
    assert payload["error_message"] == "TimeoutError"
    assert "secret provider echo" not in repr(payload)


def test_factory_requires_explicit_enable_and_key(monkeypatch):
    monkeypatch.delenv("AGENT_PROMPTLAYER", raising=False)
    monkeypatch.delenv("PROMPTLAYER_API_KEY", raising=False)
    assert make_promptlayer_observer(session_id="s", workspace_root=lambda: "/repo") is None

    monkeypatch.setenv("AGENT_PROMPTLAYER", "1")
    try:
        make_promptlayer_observer(session_id="s", workspace_root=lambda: "/repo")
        assert False, "enabled logging without a workspace key must be rejected"
    except ValueError as error:
        assert "PROMPTLAYER_API_KEY" in str(error)


def test_api_key_is_not_present_in_public_state():
    observer = PromptLayerObserver(
        api_key="pl_secret_value", session_id="s", workspace_root=lambda: "/repo",
        sender=lambda _payload: {"id": 1},
    )
    try:
        assert "pl_secret_value" not in repr(observer)
    finally:
        observer.close()
