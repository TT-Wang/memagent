"""Tests for taskdag.scheduler."""

import pytest

from taskdag.config import get, set_key
from taskdag.graph import CycleError, add_task, waves
from taskdag.scheduler import RETRY_LIMIT, dry_run, run


def build(deps):
    """Build a registry from a {name: depends_on} mapping via add_task."""
    reg = {}
    for name, depends_on in deps.items():
        add_task(reg, name, depends_on=depends_on)
    return reg


def test_run_successful_tasks_in_topo_order():
    reg = build({"a": (), "b": ("a",), "c": ("a", "b")})
    calls = []
    result = run(reg, calls.append)
    assert calls == ["a", "b", "c"]
    assert result == {"done": ["a", "b", "c"], "failed": [], "skipped": [], "retries": {}, "workers": get("worker_count"), "wave_ms": [get("wave_pause_ms")] * 3}


def test_run_failure_marks_dependent_skipped():
    reg = build({"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")})

    def fn(name):
        if name == "b":
            raise RuntimeError("boom")
        return name

    result = run(reg, fn)
    assert result == {"done": ["a", "c"], "failed": ["b"], "skipped": ["d"], "retries": {"b": 4}, "workers": get("worker_count"), "wave_ms": [get("wave_pause_ms")] * 3}


def test_run_failure_skips_transitive_dependents():
    reg = build({"a": (), "b": ("a",), "c": ("b",), "d": ("c",)})

    def fn(name):
        if name == "b":
            raise RuntimeError("boom")
        return name

    result = run(reg, fn)
    assert result == {"done": ["a"], "failed": ["b"], "skipped": ["c", "d"], "retries": {"b": 4}, "workers": get("worker_count"), "wave_ms": [get("wave_pause_ms")] * 4}


def test_run_unrelated_tasks_still_execute():
    reg = build({"a": (), "b": (), "c": ("a",)})

    def fn(name):
        if name == "a":
            raise RuntimeError("boom")
        return name

    result = run(reg, fn)
    assert result["done"] == ["b"]
    assert result["failed"] == ["a"]
    assert result["skipped"] == ["c"]


def test_run_never_calls_fn_for_skipped_tasks():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})
    calls = []

    def fn(name):
        calls.append(name)
        if name == "a":
            raise RuntimeError("boom")
        return name

    result = run(reg, fn)
    assert calls == ["a"] * (RETRY_LIMIT + 1)
    assert result["failed"] == ["a"]
    assert result["skipped"] == ["b", "c"]


def test_run_empty_registry():
    assert run({}, lambda name: name) == {"done": [], "failed": [], "skipped": [], "retries": {}, "workers": get("worker_count"), "wave_ms": []}


def test_run_raises_cycle_error_on_cyclic_registry():
    with pytest.raises(CycleError):
        run({"a": {"b"}, "b": {"a"}}, lambda name: name)


def test_dry_run_empty_registry():
    assert dry_run({}) == ""


def test_dry_run_sorts_single_wave():
    reg = build({"z": (), "a": (), "m": ()})
    assert dry_run(reg) == "wave 1: a, m, z"


def test_dry_run_chain_one_wave_per_line():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})
    assert dry_run(reg) == "wave 1: a\nwave 2: b\nwave 3: c"


def test_dry_run_diamond():
    reg = build(
        {"start": (), "left": ("start",), "right": ("start",), "end": ("left", "right")}
    )
    assert dry_run(reg) == "wave 1: start\nwave 2: left, right\nwave 3: end"


def test_dry_run_raises_cycle_error():
    with pytest.raises(CycleError):
        dry_run({"a": {"b"}, "b": {"a"}})


def test_retry_limit_is_exactly_three():
    assert RETRY_LIMIT == 3


def test_run_retries_then_succeeds():
    reg = build({"a": (), "b": ("a",)})
    calls = []

    def fn(name):
        calls.append(name)
        if name == "a" and calls.count("a") < 3:
            raise RuntimeError("flaky")
        return name

    result = run(reg, fn)
    assert result == {"done": ["a", "b"], "failed": [], "skipped": [], "retries": {}, "workers": get("worker_count"), "wave_ms": [get("wave_pause_ms")] * 2}
    assert calls.count("a") == 3


def test_run_retry_success_unblocks_dependents():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})
    calls = []

    def fn(name):
        calls.append(name)
        if name == "b" and calls.count("b") < 2:
            raise RuntimeError("flaky")
        return name

    result = run(reg, fn)
    assert result == {"done": ["a", "b", "c"], "failed": [], "skipped": [], "retries": {}, "workers": get("worker_count"), "wave_ms": [get("wave_pause_ms")] * 3}


def test_run_marks_failed_after_exhausting_retries():
    reg = build({"a": (), "b": ("a",)})
    calls = []

    def fn(name):
        calls.append(name)
        if name == "a":
            raise RuntimeError("boom")
        return name

    result = run(reg, fn)
    assert result == {"done": [], "failed": ["a"], "skipped": ["b"], "retries": {"a": 4}, "workers": get("worker_count"), "wave_ms": [get("wave_pause_ms")] * 2}
    assert calls.count("a") == RETRY_LIMIT + 1


def test_dry_run_higher_priority_first_then_alphabetical():
    reg = build({"z": (), "a": ()})
    add_task(reg, "m", priority=5)
    assert dry_run(reg) == "wave 1: m, a, z"

def test_run_workers_entry_matches_config():
    reg = build({"a": ()})
    set_key("worker_count", 3)
    try:
        result = run(reg, lambda name: name)
        assert result["workers"] == get("worker_count")
        assert result["workers"] == 3
    finally:
        set_key("worker_count", 4)


def test_run_workers_defaults_to_seeded_four():
    result = run(build({"a": ()}), lambda name: name)
    assert result["workers"] == 4
    assert result["workers"] == get("worker_count")


def test_run_accepts_registry():
    from taskdag.registry import Registry

    reg = Registry()
    reg.add("a")
    reg.add("b", depends_on=("a",))
    calls = []
    result = run(reg, calls.append)
    assert calls == ["a", "b"]
    assert result["done"] == ["a", "b"]
    assert result["failed"] == []
    assert result["skipped"] == []
    assert result["workers"] == get("worker_count")


def test_run_accepts_registry_with_failure_propagation():
    from taskdag.registry import Registry

    reg = Registry()
    reg.add("a")
    reg.add("b", depends_on=("a",))
    reg.add("c", depends_on=("a",))
    reg.add("d", depends_on=("b", "c"))

    def fn(name):
        if name == "b":
            raise RuntimeError("boom")
        return name

    result = run(reg, fn)
    assert result["done"] == ["a", "c"]
    assert result["failed"] == ["b"]
    assert result["skipped"] == ["d"]


def test_dry_run_accepts_registry_with_priority():
    from taskdag.registry import Registry

    reg = Registry()
    reg.add("z")
    reg.add("a")
    reg.add("m", priority=5)
    assert dry_run(reg) == "wave 1: m, a, z"



def test_retry_limit_reads_through_config():
    assert get("qz_retry_limit") == 3
    assert RETRY_LIMIT == get("qz_retry_limit")


def test_run_wave_ms_one_entry_per_wave():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})
    result = run(reg, lambda name: name)
    assert result["wave_ms"] == [get("wave_pause_ms")] * 3
    assert len(result["wave_ms"]) == len(waves(reg))


def test_run_wave_ms_single_wave_single_entry():
    reg = build({"z": (), "a": (), "m": ()})
    result = run(reg, lambda name: name)
    assert result["wave_ms"] == [get("wave_pause_ms")]


def test_run_wave_ms_follows_config_value():
    reg = build({"a": (), "b": (), "c": ("a", "b")})
    set_key("wave_pause_ms", 25)
    try:
        result = run(reg, lambda name: name)
        assert result["wave_ms"] == [25, 25]
    finally:
        set_key("wave_pause_ms", 50)


def test_run_wave_ms_empty_registry_is_empty():
    assert run({}, lambda name: name)["wave_ms"] == []


def test_run_cancel_skips_cancelled_task_and_dependents():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})
    calls = []

    def fn(name):
        calls.append(name)
        return name

    result = run(reg, fn, cancel={"b"})
    assert result == {
        "done": ["a"],
        "failed": [],
        "skipped": ["b", "c"],
        "retries": {},
        "workers": get("worker_count"),
        "wave_ms": [get("wave_pause_ms")] * 3,
    }
    assert calls == ["a"]


def test_run_cancel_skips_transitive_dependents():
    reg = build({"a": (), "b": ("a",), "c": ("b",), "d": ("c",)})
    result = run(reg, lambda name: name, cancel={"b"})
    assert result["done"] == ["a"]
    assert result["skipped"] == ["b", "c", "d"]


def test_run_cancel_only_skips_affected_branch():
    # "c" depends on the cancelled "b", so it and its dependent "d" skip;
    # the unrelated "a" still runs.
    reg = build({"a": (), "b": (), "c": ("b",), "d": ("a", "c")})
    result = run(reg, lambda name: name, cancel={"b"})
    assert result["done"] == ["a"]
    assert result["failed"] == []
    assert result["skipped"] == ["b", "c", "d"]


def test_run_cancel_empty_set_runs_everything():
    reg = build({"a": (), "b": ("a",)})
    result = run(reg, lambda name: name, cancel=set())
    assert result == {
        "done": ["a", "b"],
        "failed": [],
        "skipped": [],
        "retries": {},
        "workers": get("worker_count"),
        "wave_ms": [get("wave_pause_ms")] * 2,
    }


def test_run_cancel_default_none_runs_everything():
    reg = build({"a": (), "b": ("a",)})
    result = run(reg, lambda name: name)
    assert result["done"] == ["a", "b"]
    assert result["skipped"] == []


def test_run_cancel_unknown_names_are_ignored():
    reg = build({"a": (), "b": ("a",)})
    result = run(reg, lambda name: name, cancel={"no_such_task"})
    assert result["done"] == ["a", "b"]
    assert result["failed"] == []
    assert result["skipped"] == []


def test_run_cancel_never_calls_fn_for_cancelled_tasks():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})
    calls = []

    def fn(name):
        calls.append(name)
        return name

    run(reg, fn, cancel={"b"})
    assert calls == ["a"]


def test_run_only_tag_runs_tagged_tasks_and_dependency_closure():
    reg = build({"a": (), "b": ("a",), "c": ("b",), "x": (), "y": ()})
    add_task(reg, "b", depends_on=("a",), tags=("core",))
    calls = []
    result = run(reg, calls.append, only_tag="core")
    assert calls == ["a", "b"]
    assert result["done"] == ["a", "b"]
    assert result["failed"] == []
    assert result["skipped"] == ["c", "x", "y"]


def test_run_only_tag_closure_is_transitive():
    reg = build({"a": (), "b": ("a",), "c": ("b",), "d": ("c",)})
    add_task(reg, "d", depends_on=("c",), tags=("top",))
    result = run(reg, lambda name: name, only_tag="top")
    assert result["done"] == ["a", "b", "c", "d"]
    assert result["skipped"] == []


def test_run_only_tag_excludes_untagged_branches():
    reg = build({"a": (), "b": ("a",), "c": (), "d": ("c",)})
    add_task(reg, "b", depends_on=("a",), tags=("keep",))
    result = run(reg, lambda name: name, only_tag="keep")
    assert result["done"] == ["a", "b"]
    assert result["skipped"] == ["c", "d"]


def test_run_only_tag_untagged_root_with_tagged_dependent():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})
    add_task(reg, "c", depends_on=("b",), tags=("target",))
    calls = []
    result = run(reg, calls.append, only_tag="target")
    assert result["done"] == ["a", "b", "c"]
    assert calls == ["a", "b", "c"]


def test_run_only_tag_no_match_skips_everything():
    reg = build({"a": (), "b": ("a",)})
    calls = []
    result = run(reg, calls.append, only_tag="nope")
    assert result["done"] == []
    assert result["skipped"] == ["a", "b"]
    assert calls == []


def test_run_only_tag_default_none_runs_everything():
    reg = build({"a": (), "b": ("a",)})
    result = run(reg, lambda name: name, only_tag=None)
    assert result["done"] == ["a", "b"]
    assert result["skipped"] == []


def test_run_only_tag_never_calls_fn_for_excluded_tasks():
    reg = build({"a": (), "b": ("a",), "x": ()})
    add_task(reg, "b", depends_on=("a",), tags=("core",))
    calls = []
    run(reg, calls.append, only_tag="core")
    assert calls == ["a", "b"]


def test_run_only_tag_with_failure_propagation():
    reg = build({"a": (), "b": ("a",), "c": ("b",), "x": ()})
    add_task(reg, "c", depends_on=("b",), tags=("core",))

    def fn(name):
        if name == "b":
            raise RuntimeError("boom")
        return name

    result = run(reg, fn, only_tag="core")
    assert result["done"] == ["a"]
    assert result["failed"] == ["b"]
    assert result["skipped"] == ["c", "x"]


def test_run_only_tag_combines_with_cancel():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})
    add_task(reg, "b", depends_on=("a",), tags=("core",))
    result = run(reg, lambda name: name, cancel={"b"}, only_tag="core")
    assert result["done"] == ["a"]
    assert result["skipped"] == ["b", "c"]


def test_run_only_tag_accepts_registry():
    from taskdag.registry import Registry

    reg = Registry()
    reg.add("a")
    reg.add("b", depends_on=("a",), tags=("io",))
    reg.add("z")
    result = run(reg, lambda name: name, only_tag="io")
    assert result["done"] == ["a", "b"]
    assert result["skipped"] == ["z"]


def test_run_only_tag_wave_ms_matches_full_registry():
    reg = build({"a": (), "b": ("a",), "c": ("b",), "x": (), "y": ()})
    add_task(reg, "b", depends_on=("a",), tags=("core",))
    result = run(reg, lambda name: name, only_tag="core")
    assert result["wave_ms"] == [get("wave_pause_ms")] * 3


def test_run_key_fn_changes_execution_order():
    reg = build({"aa": (), "ab": (), "ba": ()})
    calls = []
    result = run(reg, calls.append, key_fn=lambda name: name[::-1])
    assert calls == ["aa", "ba", "ab"]
    assert result["done"] == ["aa", "ba", "ab"]
    assert result["failed"] == []
    assert result["skipped"] == []


def test_run_key_fn_default_none_stays_alphabetical():
    reg = build({"aa": (), "ab": (), "ba": ()})
    result = run(reg, lambda name: name)
    assert result["done"] == ["aa", "ab", "ba"]


def test_run_key_fn_dependencies_still_dominate():
    reg = build({"a": (), "b": ("a",), "c": ("b",)})
    calls = []
    run(reg, calls.append, key_fn=lambda name: -ord(name[0]))
    assert calls == ["a", "b", "c"]


def test_dry_run_key_fn_changes_wave_order():
    reg = build({"aa": (), "ab": (), "ba": ()})
    assert dry_run(reg) == "wave 1: aa, ab, ba"
    assert dry_run(reg, key_fn=lambda name: name[::-1]) == "wave 1: aa, ba, ab"


def test_dry_run_key_fn_applies_after_priority():
    reg = build({"za": (), "az": ()})
    add_task(reg, "ma", priority=5)
    assert dry_run(reg) == "wave 1: ma, az, za"
    assert dry_run(reg, key_fn=lambda name: name[::-1]) == "wave 1: ma, za, az"


def test_run_key_fn_wave_ms_unchanged():
    reg = build({"aa": (), "ab": (), "ba": ()})
    result = run(reg, lambda name: name, key_fn=lambda name: name[::-1])
    assert result["wave_ms"] == [get("wave_pause_ms")]


def test_run_key_fn_combines_with_only_tag():
    reg = build({"aa": (), "ab": (), "ba": ()})
    add_task(reg, "ab", tags=("core",))
    calls = []
    result = run(reg, calls.append, only_tag="core", key_fn=lambda name: name[::-1])
    assert result["done"] == ["ab"]
    assert result["skipped"] == ["aa", "ba"]
    assert calls == ["ab"]


def test_run_key_fn_accepts_registry():
    from taskdag.registry import Registry

    reg = Registry()
    reg.add("aa")
    reg.add("ab")
    reg.add("ba")
    calls = []
    result = run(reg, calls.append, key_fn=lambda name: name[::-1])
    assert calls == ["aa", "ba", "ab"]
    assert result["done"] == ["aa", "ba", "ab"]

def test_run_explicit_retry_limit_overrides_config():
    reg = build({"a": ()})

    def fn(name):
        raise RuntimeError("boom")

    set_key("qz_retry_limit", 2)
    try:
        result = run(reg, fn, retry_limit=3)
        assert result["retries"] == {"a": 4}
    finally:
        set_key("qz_retry_limit", 3)


def test_run_retry_limit_none_defaults_to_config():
    reg = build({"a": ()})

    def fn(name):
        raise RuntimeError("boom")

    result = run(reg, fn, retry_limit=None)
    assert result["retries"] == {"a": RETRY_LIMIT + 1}
