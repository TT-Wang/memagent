"""Tests for taskdag.stats."""

import json

from taskdag.config import set_key
from taskdag.graph import add_task
from taskdag.scheduler import RETRY_LIMIT, run
from taskdag.stats import failures, results_to_json, summarize


def build(deps):
    """Build a plain-dict registry from a {name: depends_on} mapping."""
    reg = {}
    for name, depends_on in deps.items():
        add_task(reg, name, depends_on=depends_on)
    return reg


def test_summarize_success_run_counts():
    result = run(build({"a": (), "b": ("a",)}), lambda name: name)
    assert summarize(result) == {
        "total": 2,
        "done": 2,
        "failed": 0,
        "skipped": 0,
        "summary": "2 done, 0 failed, 0 skipped",
    }


def test_summarize_failure_run_counts():
    reg = build({"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")})

    def fn(name):
        if name == "b":
            raise RuntimeError("boom")
        return name

    summary = summarize(run(reg, fn))
    assert summary["total"] == 4
    assert summary["done"] == 2
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert summary["summary"] == "2 done, 1 failed, 1 skipped"


def test_summarize_cancel_run_counts():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})
    summary = summarize(run(reg, lambda name: name, cancel={"b"}))
    assert summary == {
        "total": 3,
        "done": 1,
        "failed": 0,
        "skipped": 2,
        "summary": "1 done, 0 failed, 2 skipped",
    }


def test_summarize_empty_run():
    assert summarize(run({}, lambda name: name)) == {
        "total": 0,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "summary": "0 done, 0 failed, 0 skipped",
    }


def test_summarize_counts_match_run_lists():
    reg = build({"a": (), "b": ("a",), "x": (), "y": ()})
    add_task(reg, "b", depends_on=("a",), tags=("core",))
    result = run(reg, lambda name: name, only_tag="core")
    summary = summarize(result)
    assert summary["done"] == len(result["done"])
    assert summary["failed"] == len(result["failed"])
    assert summary["skipped"] == len(result["skipped"])
    assert summary["total"] == (
        len(result["done"]) + len(result["failed"]) + len(result["skipped"])
    )


def test_summarize_summary_is_one_line():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})

    def fn(name):
        if name == "a":
            raise RuntimeError("boom")
        return name

    summary = summarize(run(reg, fn))
    assert "\n" not in summary["summary"]
    assert summary["summary"].count(", ") == 2



def test_results_to_json_round_trips_success_run(tmp_path):
    result = run(build({"a": (), "b": ("a",)}), lambda name: name)
    path = tmp_path / "result.json"
    results_to_json(result, path)
    assert json.loads(path.read_text()) == result


def test_results_to_json_empty_run(tmp_path):
    result = run({}, lambda name: name)
    path = tmp_path / "result.json"
    results_to_json(result, path)
    assert json.loads(path.read_text()) == result


def test_results_to_json_failure_run(tmp_path):
    reg = build({"a": (), "b": ("a",), "c": ("b",)})

    def fn(name):
        if name == "a":
            raise RuntimeError("boom")
        return name

    path = tmp_path / "result.json"
    results_to_json(run(reg, fn), path)
    data = json.loads(path.read_text())
    assert data["failed"] == ["a"]
    assert data["skipped"] == ["b", "c"]
    assert data["done"] == []


def test_results_to_json_overwrites_existing_file(tmp_path):
    path = tmp_path / "result.json"
    path.write_text("not json at all")
    result = run(build({"a": ()}), lambda name: name)
    results_to_json(result, path)
    assert json.loads(path.read_text()) == result


def test_failures_empty_when_nothing_failed():
    result = run(build({"a": (), "b": ("a",)}), lambda name: name)
    assert failures(result) == []


def test_failures_lists_failed_task_with_attempts():
    reg = build({"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")})

    def fn(name):
        if name == "b":
            raise RuntimeError("boom")
        return name

    result = run(reg, fn)
    assert result["retries"] == {"b": 4}
    assert failures(result) == ["b: 4"]


def test_failures_attempts_equal_retry_limit_plus_one():
    reg = build({"a": ()})

    def fn(name):
        raise RuntimeError("boom")

    result = run(reg, fn)
    assert failures(result) == [f"a: {RETRY_LIMIT + 1}"]
    assert result["retries"] == {"a": RETRY_LIMIT + 1}


def test_failures_attempts_follow_configured_retry_limit():
    reg = build({"a": ()})

    def fn(name):
        raise RuntimeError("boom")

    set_key("qz_retry_limit", 2)
    try:
        result = run(reg, fn)
        assert failures(result) == ["a: 3"]
    finally:
        set_key("qz_retry_limit", 3)


def test_failures_multiple_failed_tasks_in_topo_order():
    reg = build({"a": (), "b": (), "c": ("a", "b")})

    def fn(name):
        if name in ("a", "b"):
            raise RuntimeError("boom")
        return name

    result = run(reg, fn)
    assert result["failed"] == ["a", "b"]
    assert failures(result) == ["a: 4", "b: 4"]


def test_failures_excludes_tasks_that_succeed_after_retries():
    reg = build({"a": (), "b": ("a",)})
    calls = []

    def fn(name):
        calls.append(name)
        if name == "a" and calls.count("a") < 3:
            raise RuntimeError("flaky")
        return name

    result = run(reg, fn)
    assert result["done"] == ["a", "b"]
    assert result["retries"] == {}
    assert failures(result) == []


def test_failures_with_cancel_and_only_tag():
    reg = build({"a": (), "b": ("a",), "c": ("b",), "x": ()})
    add_task(reg, "c", depends_on=("b",), tags=("core",))

    def fn(name):
        if name == "b":
            raise RuntimeError("boom")
        return name

    result = run(reg, fn, cancel={"x"}, only_tag="core")
    assert result["done"] == ["a"]
    assert result["failed"] == ["b"]
    assert result["skipped"] == ["c", "x"]
    assert failures(result) == ["b: 4"]


def test_failures_empty_registry():
    assert failures(run({}, lambda name: name)) == []
