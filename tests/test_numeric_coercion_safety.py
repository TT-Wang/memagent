"""Non-finite numeric inputs fail safe at provider, config, and tool boundaries."""

import json
from types import SimpleNamespace

import pytest

from sliceagent.code_grep import _norm_int
from sliceagent.config import Config
from sliceagent.event_ledger import EventLedger, LedgerCorruptError
from sliceagent.execution import estimate_model_call
from sliceagent.llm import _usage_dict
from sliceagent.receipts import compact_receipt_projection
from sliceagent.tools import _coerce_int


def test_nonfinite_model_tool_and_receipt_numbers_do_not_raise() -> None:
    assert _coerce_int(float("inf")) is None
    assert _norm_int(float("inf"), 7) == 7
    assert _usage_dict(SimpleNamespace(
        prompt_tokens=float("inf"),
        completion_tokens=float("-inf"),
        cached_tokens=0,
    ))["prompt_tokens"] == 0

    report = estimate_model_call(
        SimpleNamespace(context_window=float("inf"), max_tokens=float("inf")),
        [{"role": "user", "content": "hello"}],
        [],
    )
    assert report.context_window == 0
    assert report.output_reserve == 0

    compact = compact_receipt_projection({
        "counts": {"requested": float("inf")},
        "operations": [],
    })
    assert compact["counts"]["requested"] == 0


def test_nonfinite_toml_budgets_fall_back_to_safe_defaults() -> None:
    config = Config({
        "agent": {"subagent_depth": float("inf")},
        "budget": {"max_tokens": float("inf"), "max_steps": float("inf")},
    })

    assert config.subagent_depth == 1
    assert config.max_tokens is None
    assert config.max_steps == 120


def test_nonfinite_persisted_integer_is_reported_as_typed_corruption(tmp_path) -> None:
    ledger = EventLedger("numeric-corrupt", root=str(tmp_path))
    record = {
        "v": 1,
        "id": "event-a",
        "kind": "request_admitted",
        "session_id": "numeric-corrupt",
        "logical_turn_id": "turn-a",
        "task_id": "task-a",
        "segment_id": "",
        "workspace_epoch": float("inf"),
        "workspace_id": "",
        "timestamp": 1,
        "payload": {},
    }
    with open(ledger.path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")

    with pytest.raises(LedgerCorruptError):
        EventLedger("numeric-corrupt", root=str(tmp_path))
