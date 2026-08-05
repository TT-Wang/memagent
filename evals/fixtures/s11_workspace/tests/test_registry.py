"""Tests for taskdag.registry and the dict-or-Registry call styles."""

import pytest

from taskdag.graph import add_task, merge, remove_task, topo_order, waves
from taskdag.registry import Registry
from taskdag.scheduler import dry_run, run


def build_dict(deps):
    """Build a plain-dict registry from a {name: depends_on} mapping."""
    reg = {}
    for name, depends_on in deps.items():
        add_task(reg, name, depends_on=depends_on)
    return reg


def build_registry(deps):
    """Build a Registry from a {name: depends_on} mapping."""
    reg = Registry()
    for name, depends_on in deps.items():
        reg.add(name, depends_on=depends_on)
    return reg


def test_registry_starts_empty():
    assert Registry().deps == {}


def test_registry_add_populates_deps():
    reg = Registry()
    reg.add("build")
    reg.add("compile", depends_on=("build",))
    assert reg.deps == {"build": set(), "compile": {"build"}}


def test_registry_add_stores_priority():
    reg = Registry()
    reg.add("a", priority=5)
    assert reg.deps["a"].priority == 5


def test_registry_add_rejects_unknown_dependency():
    reg = Registry()
    reg.add("fetch")
    with pytest.raises(ValueError, match="unknown dependencies"):
        reg.add("parse", depends_on=("fetch", "missing"))


def test_registry_add_rejects_self_dependency():
    with pytest.raises(ValueError, match="cannot depend on itself"):
        Registry().add("task", depends_on=("task",))


def test_registry_remove_deletes_and_cleans_references():
    reg = Registry()
    reg.add("a")
    reg.add("b", depends_on=("a",))
    reg.remove("a")
    assert reg.deps == {"b": set()}


def test_registry_remove_missing_raises_key_error():
    with pytest.raises(KeyError):
        Registry().remove("nope")


def test_module_add_task_accepts_registry():
    reg = Registry()
    add_task(reg, "a")
    add_task(reg, "b", depends_on=("a",))
    assert reg.deps == {"a": set(), "b": {"a"}}


def test_module_remove_task_accepts_registry():
    reg = Registry()
    add_task(reg, "a")
    add_task(reg, "b", depends_on=("a",))
    remove_task(reg, "a")
    assert reg.deps == {"b": set()}


def test_topo_order_accepts_registry():
    reg = Registry()
    reg.add("b")
    reg.add("a", depends_on=("b",))
    assert topo_order(reg) == ["b", "a"]


def test_waves_accepts_registry():
    reg = Registry()
    reg.add("z")
    reg.add("a")
    assert waves(reg) == [["a", "z"]]


def test_waves_priority_ordering_accepts_registry():
    reg = Registry()
    reg.add("low")
    reg.add("high", priority=10)
    assert waves(reg) == [["high", "low"]]


def test_dry_run_accepts_registry():
    reg = Registry()
    reg.add("a")
    reg.add("b", depends_on=("a",))
    assert dry_run(reg) == "wave 1: a\nwave 2: b"


def test_run_accepts_registry():
    reg = Registry()
    reg.add("a")
    reg.add("b", depends_on=("a",))
    calls = []
    result = run(reg, calls.append)
    assert calls == ["a", "b"]
    assert result["done"] == ["a", "b"]
    assert result["failed"] == []
    assert result["skipped"] == []


def test_run_failure_propagation_accepts_registry():
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


def test_both_styles_produce_same_results():
    deps = {"a": (), "b": ("a",), "c": ("a", "b"), "d": ()}
    assert topo_order(build_dict(deps)) == topo_order(build_registry(deps))
    assert waves(build_dict(deps)) == waves(build_registry(deps))
    assert dry_run(build_dict(deps)) == dry_run(build_registry(deps))
    assert run(build_dict(deps), lambda n: n)["done"] == run(
        build_registry(deps), lambda n: n
    )["done"]


def test_functions_reject_non_dict_non_registry():
    with pytest.raises(TypeError, match="dict or Registry"):
        topo_order(object())
    with pytest.raises(TypeError, match="dict or Registry"):
        add_task(object(), "a")


def test_merge_accepts_registries():
    a = Registry()
    a.add("a")
    a.add("b", depends_on=("a",))
    b = Registry()
    b.add("x")
    assert merge(a, b) == {"a": set(), "b": {"a"}, "x": set()}


def test_merge_accepts_mixed_dict_and_registry():
    a = {}
    add_task(a, "a")
    b = Registry()
    b.add("a")
    b.add("b", depends_on=("a",))
    assert merge(a, b) == {"a": set(), "b": {"a"}}


def test_merge_rejects_non_dict_non_registry():
    with pytest.raises(TypeError, match="dict or Registry"):
        merge(object(), {})
    with pytest.raises(TypeError, match="dict or Registry"):
        merge({}, object())
