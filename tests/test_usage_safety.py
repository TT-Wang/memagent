"""Provider telemetry cannot refund budget or poison cost aggregation."""

from sliceagent.execution import Usage


def test_negative_and_nonfinite_usage_is_normalized_fail_safe() -> None:
    hostile = Usage.from_value({
        "prompt_tokens": -100,
        "completion_tokens": "-5",
        "input_cache_read": float("-inf"),
        "cost_usd": float("inf"),
    })

    assert hostile.prompt_tokens == 0
    assert hostile.completion_tokens == 0
    assert hostile.input_cache_read == 0
    assert hostile.cost_usd is None


def test_hostile_usage_cannot_reduce_already_counted_tokens() -> None:
    counted = Usage(prompt_tokens=50, completion_tokens=10, cost_usd=0.2)
    hostile = Usage(prompt_tokens=-500, completion_tokens=-100, cost_usd=-4)

    total = counted + hostile

    assert total.prompt_tokens == 50
    assert total.completion_tokens == 10
    assert total.cost_usd == 0.2
