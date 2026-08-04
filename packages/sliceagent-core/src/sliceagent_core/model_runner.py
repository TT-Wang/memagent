"""Single provider-call seam for capacity preflight and retry policy."""
from __future__ import annotations

from datetime import datetime, timezone

from .errors import with_retry
from .execution import preflight_model_call


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _observe_physical_call(llm, record: dict) -> None:
    """Publish one best-effort, post-I/O observation without perturbing the call.

    The host may install ``_model_call_observer`` on any LLM adapter.  Keeping this seam
    duck-typed preserves the public two-argument LLM protocol and the core's zero-dependency
    boundary.  Observers receive the exact prepared request and final provider outcome, but
    their failures never become provider retries or failed agent turns.
    """
    observer = getattr(llm, "_model_call_observer", None)
    if not callable(observer):
        return
    try:
        observer(record)
    except Exception:  # noqa: BLE001 - telemetry must never alter provider-call semantics
        pass


def complete_model_call(
    llm,
    messages: list[dict],
    schemas: list[dict],
    *,
    dispatch=None,
    retry: bool = True,
    allow_unknown: bool | None = None,
    on_attempt=None,
    should_cancel=None,
    transport_activity=None,
):
    """Preflight and execute one model call through the shared retry boundary.

    Usage/budget ownership stays with the calling lifecycle because routing, onboarding, background
    consolidation, and an active turn have different accounting scopes. None bypasses physical validation.
    ``on_attempt`` is the required pre-request publication seam: if it cannot durably publish the prepared
    call, its failure propagates and the provider request is not opened.
    """
    if allow_unknown is None:
        allow_unknown = not bool(getattr(llm, "require_known_context", False))

    physical_attempt = 0

    def invoke():
        nonlocal physical_attempt
        report = preflight_model_call(llm, messages, schemas, allow_unknown=allow_unknown)
        physical_attempt += 1
        if on_attempt is not None:
            # This is lifecycle publication, not optional diagnostics. Production uses it to dispatch
            # ModelCallPrepared through the required journal/reducer sinks before any provider I/O.
            on_attempt(physical_attempt, messages, report)
        # Production adapters may expose the richer per-request control seam.  Feature detection keeps the
        # public two-argument LLMClient protocol (and every test/third-party adapter implementing it) intact;
        # cancellation and transport activity are never smuggled into arbitrary ``complete`` callables.
        started_at = _utc_now()
        effort = getattr(llm, "_effort", None)
        try:
            api_type = "responses" if callable(effort) and effort() else "chat-completions"
        except Exception:  # noqa: BLE001 - wire-type metadata must never block the call
            api_type = "chat-completions"
        common = {
            "attempt": physical_attempt,
            "started_at": started_at,
            "model": str(getattr(llm, "model", "") or ""),
            "base_url": str(getattr(llm, "_base_url", "") or ""),
            "reasoning": str(getattr(llm, "reasoning", "") or ""),
            "api_type": api_type,
            "messages": messages,
            "schemas": schemas,
        }
        try:
            controlled = getattr(llm, "complete_with_control", None)
            if callable(controlled):
                response = controlled(
                    messages,
                    schemas,
                    should_cancel=should_cancel,
                    transport_activity=transport_activity,
                )
            else:
                response = llm.complete(messages, schemas)
        except Exception as error:
            _observe_physical_call(llm, {
                **common, "ended_at": _utc_now(), "response": None, "error": error,
            })
            raise
        _observe_physical_call(llm, {
            **common, "ended_at": _utc_now(), "response": response, "error": None,
        })
        return response

    if not retry:
        return invoke()
    return with_retry(
        invoke, is_retryable=getattr(llm, "is_retryable", None), dispatch=dispatch,
        should_cancel=should_cancel,
    )


__all__ = ["complete_model_call"]
