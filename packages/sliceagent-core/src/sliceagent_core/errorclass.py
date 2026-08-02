"""Provider-error classification tables, ported from the Pi harness
(packages/ai/src/utils/retry.ts) — a battle-tested vocabulary maintained against real
provider quirks (each pattern cites the failure it was added for).

THE ORDER IS THE CONTRACT: non-retryable is checked FIRST. A 429 whose message says
"insufficient_quota" is a dead balance, not a throttle — retrying it is how rate-limit
storms happen. Only after the non-retryable table passes do type checks and the
retryable message table apply.
"""
from __future__ import annotations

import re


def _build(patterns: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(patterns), re.IGNORECASE)


# Subscription/account/quota exhaustion — never transient. (Pi retry.ts:7-24)
# STRONG markers only: a weak one ("billing", "available balance") matches the GUIDANCE text of a
# transient free-tier 429 ("check your plan and billing details", a billing URL) and makes a
# retryable RateLimitError permanent — 1 attempt instead of 3 (the review's P2; a regression the
# port introduced). "insufficient_quota" is OpenAI's billing code; "insufficient balance" is
# DeepSeek's 402 — both are dead balances, and retrying a dead balance is how rate-limit storms
# happen. Guidance about billing is NOT a dead balance.
NON_RETRYABLE_PROVIDER_ERROR_PATTERN = _build((
    # OpenCode Go/free-tier limits returned as 429 JSON error types by OpenCode's Zen API.
    "GoUsageLimitError",
    "FreeUsageLimitError",
    # OpenCode Go subscription-limit text (rolling/weekly/monthly caps).
    "Monthly usage limit reached",
    # Generic quota/budget/billing exhaustion, as CODES or decisive phrases — never the mere
    # mention of billing.
    "insufficient_quota",
    "insufficient balance",
    "out of budget",
    "quota exceeded",
))

# Transient provider load, HTTP status, transport failures, premature stream endings,
# provider-requested retries. (Pi retry.ts:26-89, with per-provider issue references there.)
# Bare status digits are ANCHORED: "500" must not match the "500" inside a token count like
# 130500 — an overflow message carrying that count would be re-sent 3x at full size, directly
# contradicting the rule that overflow tightens the slice, never re-sends (the review's P2).
def _status(code: int) -> str:
    return rf"(?<!\d){code}(?!\d)"


RETRYABLE_PROVIDER_ERROR_PATTERN = _build((
    "overloaded",
    "rate.?limit",
    "too many requests",
    _status(429),
    _status(500),
    _status(502),
    _status(503),
    _status(504),
    _status(524),
    "service.?unavailable",
    "server.?error",
    "internal.?error",
    "provider.?returned.?error",       # OpenRouter "Provider returned error" (pi #2264)
    "network.?error",
    "connection.?error",
    "connection.?refused",
    "connection.?lost",
    "other side closed",
    "fetch failed",
    "getaddrinfo",
    "ENOTFOUND",
    "EAI_AGAIN",
    "upstream.?connect",               # OpenAI Codex raw-fetch failures (pi #733)
    "reset before headers",
    "socket hang up",
    "socket connection was closed",
    "timed? out",
    "timeout",
    "terminated",
    "websocket.?closed",
    "websocket.?error",
    "ended without",                   # Anthropic "stream ended without …" (pi #4433)
    "stream ended before message_stop",
    "stream ended before a terminal response event",
    "http2 request did not get a response",   # Bedrock/Smithy (pi #3594)
    "retry delay",
    "you can retry your request",      # explicit provider retry guidance (pi #6019)
    "try your request again",
    "please retry your request",
    "ResourceExhausted",               # gRPC providers (e.g. NVIDIA NIM)
))


def is_non_retryable_message(text: str) -> bool:
    """Quota/billing/subscription exhaustion in the error text — retrying can never help."""
    return bool(NON_RETRYABLE_PROVIDER_ERROR_PATTERN.search(str(text or "")))


def is_retryable_message(text: str) -> bool:
    """A transient provider/transport failure named in the error text (non-retryable wins ties)."""
    return bool(RETRYABLE_PROVIDER_ERROR_PATTERN.search(str(text or "")))


__all__ = [
    "NON_RETRYABLE_PROVIDER_ERROR_PATTERN",
    "RETRYABLE_PROVIDER_ERROR_PATTERN",
    "is_non_retryable_message",
    "is_retryable_message",
]
