"""Opt-in PromptLayer request logging for SliceAgent's custom model transport.

SliceAgent keeps provider execution local and continues to use the operator's existing
DeepSeek/OpenAI-compatible credential.  PromptLayer receives a post-call request log only;
it is never placed in the provider request path and cannot create a retry or failed turn.

The default ``metadata`` content mode uploads token/cost/timing data plus keyed digests,
roles, and tool names.  Exact prompts, source code, tool arguments, and model output are sent
only when the operator explicitly chooses ``AGENT_PROMPTLAYER_CONTENT=full``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit


_TRUE = {"1", "on", "true", "yes"}
_SENTINEL = object()


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUE


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _keyed_digest(secret: str, value: object) -> str:
    return hmac.new(secret.encode("utf-8"), _json_bytes(value), hashlib.sha256).hexdigest()


def _keyed_id(secret: str, value: object) -> str:
    return _keyed_digest(secret, str(value))[:20]


def _content_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, Mapping) and part.get("type") in {"text", "input_text"}:
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, Mapping) and part.get("type") in {"image_url", "input_image"}:
                parts.append("[image]")
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(value)


def _text_block(text: str) -> list[dict]:
    return [{"type": "text", "text": text}] if text else []


def _redacted_text(value: object, *, secret: str) -> str:
    text = _content_text(value)
    return f"[redacted chars={len(text)} hmac_sha256={_keyed_digest(secret, text)}]"


def _tool_call(call: object, *, full: bool, secret: str) -> dict:
    if isinstance(call, Mapping):
        call_id = str(call.get("id") or "")
        name = str(call.get("name") or (call.get("function") or {}).get("name") or "")
        args = call.get("args")
        if args is None and isinstance(call.get("function"), Mapping):
            args = call["function"].get("arguments")
    else:
        call_id = str(getattr(call, "id", "") or "")
        name = str(getattr(call, "name", "") or "")
        args = getattr(call, "args", {})
    if full:
        arguments = args if isinstance(args, str) else json.dumps(args or {}, sort_keys=True, default=str)
    else:
        arguments = json.dumps({"redacted_hmac_sha256": _keyed_digest(secret, args or {})}, sort_keys=True)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _input_blueprint(messages: list, schemas: list, *, full: bool, secret: str) -> dict:
    normalized = []
    for message in messages or []:
        if not isinstance(message, Mapping):
            message = {"role": "user", "content": str(message)}
        body = (
            _content_text(message.get("content"))
            if full else _redacted_text(message.get("content"), secret=secret)
        )
        item = {"role": str(message.get("role") or "user"), "content": _text_block(body)}
        calls = message.get("tool_calls") or []
        if calls:
            item["tool_calls"] = [_tool_call(call, full=full, secret=secret) for call in calls]
        normalized.append(item)
    blueprint = {"type": "chat", "messages": normalized}
    if full and schemas:
        blueprint["tools"] = schemas
        blueprint["tool_choice"] = "auto"
    return blueprint


def _output_blueprint(response: object, *, full: bool, secret: str) -> dict:
    if response is None:
        return {"type": "chat", "messages": []}
    content = getattr(response, "content", None)
    text = _content_text(content) if full else _redacted_text(content, secret=secret)
    message = {"role": "assistant", "content": _text_block(text)}
    calls = list(getattr(response, "tool_calls", None) or [])
    if calls:
        message["tool_calls"] = [_tool_call(call, full=full, secret=secret) for call in calls]
    return {"type": "chat", "messages": [message]}


def _provider_route(base_url: str) -> str:
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        host = ""
    if not host or "openai.com" in host:
        return "openai"
    if "deepseek" in host:
        return "deepseek"
    if "openrouter" in host:
        return "openrouter"
    if "moonshot" in host:
        return "moonshot"
    if "anthropic" in host:
        return "anthropic"
    return "openai-compatible"


def _error_type(error: BaseException) -> str:
    text = f"{type(error).__name__} {error}".lower()
    if "timeout" in text:
        return "PROVIDER_TIMEOUT"
    if "rate" in text and "limit" in text:
        return "PROVIDER_RATE_LIMIT"
    if "quota" in text or "balance" in text or "insufficient" in text:
        return "PROVIDER_QUOTA_LIMIT"
    if "auth" in text or "api key" in text or "unauthorized" in text:
        return "PROVIDER_AUTH_ERROR"
    return "PROVIDER_ERROR"


def _request_price(model: str, base_url: str, usage: Mapping) -> float:
    supplied = usage.get("cost_usd")
    if isinstance(supplied, (int, float)) and supplied >= 0:
        return float(supplied)
    try:
        from sliceagent_core.model_catalog import pricing

        prices = pricing(model, base_url)
    except Exception:  # noqa: BLE001 - pricing metadata is optional
        prices = None
    if not prices:
        return 0.0
    fresh_price, cached_price, output_price = prices
    fresh = int(usage.get("input_other") or 0) + int(usage.get("input_cache_creation") or 0)
    cached = int(usage.get("input_cache_read") or 0)
    output = int(usage.get("output", usage.get("completion_tokens", 0)) or 0)
    return (fresh * fresh_price + cached * cached_price + output * output_price) / 1_000_000


def _experiment_flags(env: Mapping[str, str]) -> list[str]:
    flags = []
    for name, value in env.items():
        if name.startswith("AGENT_EXPERIMENTAL_") and _truthy(value):
            flags.append(name.removeprefix("AGENT_EXPERIMENTAL_").lower())
    return sorted(flags)


class PromptLayerObserver:
    """Bounded, single-worker PromptLayer logger.

    ``__call__`` only normalizes and enqueues. Network I/O happens off the model/turn thread,
    and the queue is bounded so an unavailable SaaS cannot grow SliceAgent's memory without limit.
    """

    def __init__(
        self,
        *,
        api_key: str,
        session_id: str,
        workspace_root: Callable[[], str],
        content_mode: str = "metadata",
        tags: tuple[str, ...] = (),
        environment: str = "development",
        base_url: str = "https://api.promptlayer.com",
        queue_size: int = 32,
        timeout: float = 5.0,
        sender: Callable[[dict], Mapping | None] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("PROMPTLAYER_API_KEY is required when PromptLayer logging is enabled")
        self._api_key = api_key
        # Content digests are correlation keys, NOT confidentiality protection — but they must not be
        # brute-forceable by the log service itself, which receives the API key in the same request's
        # X-API-KEY header. Derive them from a per-process random pepper instead of the API key.
        self._pepper = secrets.token_hex(16)
        self._session_id = session_id
        self._workspace_root = workspace_root
        self.content_mode = "full" if content_mode == "full" else "metadata"
        self._tags = tuple(dict.fromkeys(tag for tag in tags if tag))
        self._environment = environment.strip() or "development"
        self._base_url = base_url.rstrip("/")
        self._timeout = max(0.1, float(timeout))
        self._sender = sender
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self._lock = threading.Lock()
        self._closed = False
        self._stats = {"queued": 0, "sent": 0, "failed": 0, "dropped": 0, "last_request_id": None}
        self._client = None
        self._worker = threading.Thread(target=self._run, name="sliceagent-promptlayer", daemon=True)
        self._worker.start()

    def _payload(self, record: Mapping) -> dict:
        messages = list(record.get("messages") or [])
        schemas = list(record.get("schemas") or [])
        response = record.get("response")
        error = record.get("error")
        usage = dict(getattr(response, "usage", None) or {})
        model = str(record.get("model") or "unknown")
        base_url = str(record.get("base_url") or "")
        full = self.content_mode == "full"
        prompt_tokens = int(usage.get("prompt_tokens") or (
            int(usage.get("input_other") or 0)
            + int(usage.get("input_cache_read") or 0)
            + int(usage.get("input_cache_creation") or 0)
        ))
        output_tokens = int(usage.get("completion_tokens", usage.get("output", 0)) or 0)
        flags = _experiment_flags(os.environ)
        roles = [
            str(message.get("role") or "user")
            for message in messages
            if isinstance(message, Mapping)
        ]
        tool_names = [
            str(schema.get("function", {}).get("name") or "")
            for schema in schemas
            if isinstance(schema, Mapping) and isinstance(schema.get("function"), Mapping)
        ]
        tags = list(dict.fromkeys((
            "sliceagent", f"environment:{self._environment}", f"content:{self.content_mode}",
            *self._tags, *(f"experiment:{flag}" for flag in flags),
        )))
        output_identity = {
            "content": getattr(response, "content", None) if response is not None else None,
            "tool_calls": [
                {
                    "name": getattr(call, "name", ""),
                    "args": getattr(call, "args", {}),
                }
                for call in (getattr(response, "tool_calls", None) or [])
            ],
            "finish_reason": getattr(response, "finish_reason", "") if response is not None else "",
        }
        metadata = {
            "sliceagent_session": _keyed_id(self._pepper, self._session_id),
            "sliceagent_workspace": _keyed_id(self._pepper, self._workspace_root()),
            "sliceagent_provider_route": _provider_route(base_url),
            "sliceagent_reasoning": str(record.get("reasoning") or ""),
            "sliceagent_attempt": str(int(record.get("attempt") or 1)),
            "sliceagent_content_mode": self.content_mode,
            "sliceagent_message_count": str(len(messages)),
            "sliceagent_tool_count": str(len(schemas)),
            "sliceagent_message_roles": ",".join(roles),
            "sliceagent_tool_names": ",".join(name for name in tool_names if name),
            "sliceagent_input_hmac_sha256": _keyed_digest(
                self._pepper, {"messages": messages, "schemas": schemas}
            ),
            "sliceagent_output_hmac_sha256": _keyed_digest(self._pepper, output_identity),
            "sliceagent_input_fresh_tokens": str(
                int(usage.get("input_other") or 0) + int(usage.get("input_cache_creation") or 0)
            ),
            "sliceagent_input_cache_read_tokens": str(int(usage.get("input_cache_read") or 0)),
            "sliceagent_input_cache_creation_tokens": str(int(usage.get("input_cache_creation") or 0)),
            "sliceagent_experiment_flags": ",".join(flags),
        }
        payload = {
            # SliceAgent's provider clients all speak the OpenAI-compatible wire. The actual route
            # (DeepSeek/OpenRouter/etc.) remains separately filterable in metadata.
            "provider": "openai",
            "model": model,
            "input": _input_blueprint(messages, schemas, full=full, secret=self._pepper),
            "output": _output_blueprint(response, full=full, secret=self._pepper),
            "request_start_time": str(record.get("started_at") or ""),
            "request_end_time": str(record.get("ended_at") or ""),
            "parameters": {"reasoning": str(record.get("reasoning") or "")},
            "tags": tags,
            "metadata": metadata,
            "input_tokens": max(0, prompt_tokens),
            "output_tokens": max(0, output_tokens),
            "price": max(0.0, _request_price(model, base_url, usage)),
            "function_name": "sliceagent.model_call",
            "api_type": str(record.get("api_type") or "chat-completions"),
            "status": "ERROR" if error is not None else "SUCCESS",
        }
        if error is not None:
            payload["error_type"] = _error_type(error)
            # Never upload a provider error body: some providers echo request content.
            payload["error_message"] = type(error).__name__
        return payload

    def __call__(self, record: Mapping) -> None:
        try:
            payload = self._payload(record)
        except Exception:  # noqa: BLE001 - malformed observations are dropped, never load-bearing
            with self._lock:
                self._stats["dropped"] += 1
            return
        # Keep the close check and non-blocking enqueue atomic with respect to sentinel insertion:
        # no late observation can land behind the worker's shutdown marker.
        with self._lock:
            if self._closed:
                self._stats["dropped"] += 1
                return
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                self._stats["dropped"] += 1
            else:
                self._stats["queued"] += 1

    def _post(self, payload: dict) -> Mapping | None:
        if self._sender is not None:
            return self._sender(payload)
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self._timeout, trust_env=False)
        response = self._client.post(
            f"{self._base_url}/log-request",
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SENTINEL:
                    return
                try:
                    result = self._post(item) or {}
                except Exception:  # noqa: BLE001 - SaaS availability cannot affect SliceAgent
                    with self._lock:
                        self._stats["failed"] += 1
                else:
                    request_id = result.get("request_id") or result.get("id")
                    with self._lock:
                        self._stats["sent"] += 1
                        if request_id is not None:
                            self._stats["last_request_id"] = request_id
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + max(0.1, float(timeout))
        with self._lock:
            if not self._closed:
                self._closed = True
                should_signal = True
            else:
                should_signal = False
        if should_signal:
            try:
                self._queue.put(_SENTINEL, timeout=max(0.1, deadline - time.monotonic()))
            except queue.Full:
                pass
        self._worker.join(timeout=max(0.1, deadline - time.monotonic()))
        if self._client is not None and not self._worker.is_alive():
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - shutdown observer only
                pass
        with self._lock:
            return {**self._stats, "pending": self._queue.qsize(), "worker_alive": self._worker.is_alive()}


def make_promptlayer_observer(*, session_id: str, workspace_root: Callable[[], str]):
    """Build the observer from documented environment configuration, or return ``None``."""
    if not _truthy(os.environ.get("AGENT_PROMPTLAYER")):
        return None
    tags = tuple(tag.strip() for tag in os.environ.get("AGENT_PROMPTLAYER_TAGS", "").split(",") if tag.strip())
    return PromptLayerObserver(
        api_key=os.environ.get("PROMPTLAYER_API_KEY", "").strip(),
        session_id=session_id,
        workspace_root=workspace_root,
        content_mode=os.environ.get("AGENT_PROMPTLAYER_CONTENT", "metadata").strip().lower(),
        tags=tags,
        environment=os.environ.get("AGENT_PROMPTLAYER_ENVIRONMENT", "development"),
        base_url=os.environ.get("PROMPTLAYER_BASE_URL", "https://api.promptlayer.com"),
    )


__all__ = ["PromptLayerObserver", "make_promptlayer_observer"]
