from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from sliceagent_core.errors import ImmediateRetryError
from sliceagent_core.model_runner import complete_model_call


class _LLM:
    model = "observer-model"
    reasoning = "fast"
    _base_url = "https://provider.invalid/v1"

    def __init__(self, outcome):
        self.outcome = outcome
        self.records = []
        self._model_call_observer = self.records.append

    def complete(self, _messages, _schemas):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_success_observer_receives_the_exact_physical_outcome():
    response = NS(content="done", tool_calls=[], usage={"prompt_tokens": 7}, finish_reason="stop")
    llm = _LLM(response)
    messages = [{"role": "user", "content": "observe"}]
    schemas = [{"type": "function", "function": {"name": "read_file"}}]

    assert complete_model_call(llm, messages, schemas, retry=False) is response
    assert len(llm.records) == 1
    record = llm.records[0]
    assert record["attempt"] == 1
    assert record["messages"] is messages and record["schemas"] is schemas
    assert record["response"] is response and record["error"] is None
    assert record["model"] == "observer-model" and record["reasoning"] == "fast"
    assert record["started_at"].endswith("+00:00") and record["ended_at"].endswith("+00:00")


def test_failure_observer_runs_once_and_the_original_error_propagates():
    failure = TimeoutError("provider timeout")
    llm = _LLM(failure)

    with pytest.raises(TimeoutError) as caught:
        complete_model_call(llm, [{"role": "user", "content": "observe"}], [], retry=False)

    assert caught.value is failure
    assert len(llm.records) == 1
    assert llm.records[0]["response"] is None and llm.records[0]["error"] is failure


def test_broken_observer_is_fail_open_and_never_replays_the_provider_call():
    response = NS(content="done", tool_calls=[], usage={}, finish_reason="stop")
    llm = _LLM(response)
    calls = 0

    def complete(_messages, _schemas):
        nonlocal calls
        calls += 1
        return response

    llm.complete = complete
    llm._model_call_observer = lambda _record: (_ for _ in ()).throw(RuntimeError("observer down"))

    assert complete_model_call(llm, [{"role": "user", "content": "x"}], [], retry=False) is response
    assert calls == 1


def test_observer_records_each_physical_retry_attempt():
    response = NS(content="done", tool_calls=[], usage={}, finish_reason="stop")
    llm = _LLM(response)
    calls = 0

    def complete(_messages, _schemas):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ImmediateRetryError("negotiate once")
        return response

    llm.complete = complete
    llm.is_retryable = lambda error: isinstance(error, ImmediateRetryError)

    assert complete_model_call(llm, [{"role": "user", "content": "x"}], []) is response
    assert [record["attempt"] for record in llm.records] == [1, 2]
    assert isinstance(llm.records[0]["error"], ImmediateRetryError)
    assert llm.records[1]["response"] is response
