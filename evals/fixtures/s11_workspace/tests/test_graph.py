"""Tests for taskdag.graph."""

import pytest

from taskdag.graph import CycleError, add_task, by_tag, merge, remove_task, topo_order, waves


def test_add_task_without_deps():
    reg = {}
    add_task(reg, "build")
    assert reg == {"build": set()}


def test_add_task_with_existing_deps():
    reg = {}
    add_task(reg, "fetch")
    add_task(reg, "parse")
    add_task(reg, "compile", depends_on=("fetch", "parse"))
    assert reg == {"fetch": set(), "parse": set(), "compile": {"fetch", "parse"}}


def test_add_task_rejects_unknown_dependency():
    reg = {}
    add_task(reg, "fetch")
    with pytest.raises(ValueError, match="unknown dependencies"):
        add_task(reg, "parse", depends_on=("fetch", "missing"))


def test_add_task_rejects_self_dependency():
    reg = {}
    with pytest.raises(ValueError, match="cannot depend on itself"):
        add_task(reg, "task", depends_on=("task",))


def test_remove_task_removes_and_cleans_references():
    reg = {}
    add_task(reg, "a")
    add_task(reg, "b", depends_on=("a",))
    remove_task(reg, "a")
    assert reg == {"b": set()}


def test_remove_missing_task_raises_key_error():
    with pytest.raises(KeyError):
        remove_task({}, "nope")


def test_topo_order_empty_registry():
    assert topo_order({}) == []


def test_topo_order_simple_chain():
    reg = {}
    add_task(reg, "a")
    add_task(reg, "b", depends_on=("a",))
    add_task(reg, "c", depends_on=("b",))
    assert topo_order(reg) == ["a", "b", "c"]


def test_topo_order_alphabetical_tie_break():
    reg = {}
    add_task(reg, "z")
    add_task(reg, "a")
    add_task(reg, "m", depends_on=("z",))
    assert topo_order(reg) == ["a", "z", "m"]


def test_topo_order_diamond():
    reg = {}
    add_task(reg, "start")
    add_task(reg, "left", depends_on=("start",))
    add_task(reg, "right", depends_on=("start",))
    add_task(reg, "end", depends_on=("left", "right"))
    assert topo_order(reg) == ["start", "left", "right", "end"]


def test_topo_order_raises_cycle_error():
    # cycles can only exist in a hand-built registry (add_task rejects unknowns)
    reg = {"a": {"b"}, "b": {"a"}}
    with pytest.raises(CycleError):
        topo_order(reg)


def test_topo_order_self_cycle_raises_cycle_error():
    # self-dependency can only appear in a hand-built registry (add_task rejects it)
    with pytest.raises(CycleError):
        topo_order({"a": {"a"}})


def test_waves_empty_registry():
    assert waves({}) == []


def test_waves_independent_tasks_single_wave():
    reg = {}
    add_task(reg, "z")
    add_task(reg, "a")
    add_task(reg, "m")
    assert waves(reg) == [["a", "m", "z"]]


def test_waves_chain_is_one_task_per_wave():
    reg = {}
    add_task(reg, "a")
    add_task(reg, "b", depends_on=("a",))
    add_task(reg, "c", depends_on=("b",))
    assert waves(reg) == [["a"], ["b"], ["c"]]


def test_waves_diamond():
    reg = {}
    add_task(reg, "start")
    add_task(reg, "left", depends_on=("start",))
    add_task(reg, "right", depends_on=("start",))
    add_task(reg, "end", depends_on=("left", "right"))
    assert waves(reg) == [["start"], ["left", "right"], ["end"]]


def test_waves_waits_for_all_dependencies():
    # "end" depends on both "left" and "right", so it must wait until both
    # have finished (i.e. it lands one wave after the slower of the two).
    reg = {}
    add_task(reg, "left")
    add_task(reg, "right", depends_on=("left",))
    add_task(reg, "end", depends_on=("left", "right"))
    assert waves(reg) == [["left"], ["right"], ["end"]]


def test_waves_raises_cycle_error():
    reg = {"a": {"b"}, "b": {"a"}}
    with pytest.raises(CycleError):
        waves(reg)


def test_add_task_default_priority_is_zero():
    reg = {}
    add_task(reg, "a")
    assert reg["a"].priority == 0


def test_add_task_stores_priority():
    reg = {}
    add_task(reg, "a", priority=5)
    assert reg["a"].priority == 5
    assert reg["a"] == set()


def test_waves_higher_priority_first():
    reg = {}
    add_task(reg, "low")
    add_task(reg, "high", priority=10)
    assert waves(reg) == [["high", "low"]]


def test_waves_priority_then_alphabetical():
    reg = {}
    add_task(reg, "z", priority=1)
    add_task(reg, "a", priority=1)
    add_task(reg, "m", priority=2)
    assert waves(reg) == [["m", "a", "z"]]


def test_waves_priority_applies_within_each_wave():
    reg = {}
    add_task(reg, "start")
    add_task(reg, "left", depends_on=("start",), priority=1)
    add_task(reg, "right", depends_on=("start",), priority=9)
    assert waves(reg) == [["start"], ["right", "left"]]

def test_cycle_error_reexported_from_errors():
    from taskdag.errors import CycleError as ErrorsCycleError

    assert CycleError is ErrorsCycleError


def test_validate_dependencies_reexported_from_validate():
    from taskdag.graph import validate_dependencies as graph_helper
    from taskdag.validate import validate_dependencies as validate_helper

    assert graph_helper is validate_helper

def test_add_task_default_tags_is_empty():
    reg = {}
    add_task(reg, "a")
    assert reg["a"].tags == frozenset()


def test_add_task_stores_tags():
    reg = {}
    add_task(reg, "a", tags=("build", "fast"))
    assert reg["a"].tags == frozenset({"build", "fast"})


def test_add_task_with_tags_and_deps():
    reg = {}
    add_task(reg, "base")
    add_task(reg, "top", depends_on=("base",), tags=("ui",))
    assert reg["top"].tags == frozenset({"ui"})
    assert reg["top"] == {"base"}


def test_by_tag_returns_matching_tasks_sorted():
    reg = {}
    add_task(reg, "z", tags=("fast",))
    add_task(reg, "a", tags=("fast",))
    add_task(reg, "m")
    assert by_tag(reg, "fast") == ["a", "z"]


def test_by_tag_returns_empty_when_no_task_has_tag():
    reg = {}
    add_task(reg, "a", tags=("build",))
    assert by_tag(reg, "nope") == []


def test_by_tag_untagged_tasks_never_match():
    # by_tag matches tags, not task names: an untagged task "a" is not
    # returned for the tag "a".
    reg = {}
    add_task(reg, "a")
    assert by_tag(reg, "a") == []


def test_by_tag_hand_built_plain_dict_has_no_tags():
    # Plain sets registered by hand carry no tags, so nothing matches.
    assert by_tag({"a": set(), "b": {"a"}}, "anything") == []


def test_by_tag_accepts_registry():
    from taskdag.registry import Registry

    reg = Registry()
    reg.add("x", tags=("io",))
    reg.add("y", tags=("io",))
    reg.add("z")
    assert by_tag(reg, "io") == ["x", "y"]


def test_by_tag_empty_registry():
    assert by_tag({}, "tag") == []


def test_topo_order_key_fn_custom_tie_break():
    reg = {}
    add_task(reg, "aa")
    add_task(reg, "ab")
    add_task(reg, "ba")
    assert topo_order(reg) == ["aa", "ab", "ba"]
    assert topo_order(reg, key_fn=lambda name: name[::-1]) == ["aa", "ba", "ab"]


def test_topo_order_key_fn_dependencies_still_dominate():
    reg = {}
    add_task(reg, "a")
    add_task(reg, "b", depends_on=("a",))
    add_task(reg, "c", depends_on=("b",))
    # key_fn would order c, b, a -- but dependencies must win.
    assert topo_order(reg, key_fn=lambda name: -ord(name[0])) == ["a", "b", "c"]


def test_waves_key_fn_custom_tie_break():
    reg = {}
    add_task(reg, "aa")
    add_task(reg, "ab")
    add_task(reg, "ba")
    assert waves(reg) == [["aa", "ab", "ba"]]
    assert waves(reg, key_fn=lambda name: name[::-1]) == [["aa", "ba", "ab"]]


def test_waves_key_fn_applies_after_priority():
    reg = {}
    add_task(reg, "za")
    add_task(reg, "az")
    add_task(reg, "ma", priority=5)
    assert waves(reg) == [["ma", "az", "za"]]
    assert waves(reg, key_fn=lambda name: name[::-1]) == [["ma", "za", "az"]]

def test_merge_disjoint_registries_unions_names_and_deps():
    a = {}
    add_task(a, "fetch")
    add_task(a, "parse", depends_on=("fetch",))
    b = {}
    add_task(b, "compile")
    merged = merge(a, b)
    assert merged == {"fetch": set(), "parse": {"fetch"}, "compile": set()}


def test_merge_overlapping_identical_deps_ok():
    a = {}
    add_task(a, "common")
    b = {}
    add_task(b, "common")
    assert merge(a, b) == {"common": set()}


def test_merge_conflicting_deps_raises_value_error():
    a = {}
    add_task(a, "base")
    add_task(a, "shared", depends_on=("base",))
    b = {}
    add_task(b, "shared")
    with pytest.raises(
        ValueError, match="conflicting dependency sets for task 'shared'"
    ):
        merge(a, b)


def test_merge_error_message_lists_both_dependency_sets():
    a = {}
    add_task(a, "base")
    add_task(a, "shared", depends_on=("base",))
    b = {}
    add_task(b, "shared")
    with pytest.raises(ValueError, match=r"\['base'\] vs \[\]"):
        merge(a, b)


def test_merge_empty_with_nonempty_returns_copy():
    b = {}
    add_task(b, "a")
    add_task(b, "c", depends_on=("a",))
    merged = merge({}, b)
    assert merged == {"a": set(), "c": {"a"}}


def test_merge_two_empty_registries():
    assert merge({}, {}) == {}


def test_merge_does_not_mutate_inputs():
    a = {}
    add_task(a, "a")
    add_task(a, "b", depends_on=("a",))
    b = {}
    add_task(b, "x")
    merge(a, b)
    assert a == {"a": set(), "b": {"a"}}
    assert b == {"x": set()}


def test_merge_result_works_with_topo_order_and_waves():
    a = {}
    add_task(a, "start")
    b = {}
    add_task(b, "start")
    add_task(b, "end", depends_on=("start",))
    merged = merge(a, b)
    assert topo_order(merged) == ["start", "end"]
    assert waves(merged) == [["start"], ["end"]]


def test_merge_preserves_priority_and_tags():
    a = {}
    add_task(a, "hi", priority=9, tags=("io",))
    b = {}
    add_task(b, "lo")
    merged = merge(a, b)
    assert merged["hi"].priority == 9
    assert merged["hi"].tags == frozenset({"io"})


def test_merge_identical_overlap_keeps_first_registrys_attributes():
    a = {}
    add_task(a, "t", priority=3)
    b = {}
    add_task(b, "t", priority=7)
    merged = merge(a, b)
    assert merged["t"].priority == 3


def test_cycle_error_message_names_cycle_path():
    with pytest.raises(CycleError) as excinfo:
        topo_order({"a": {"b"}, "b": {"a"}})
    assert str(excinfo.value) == "cycle detected in task registry: a -> b -> a"


def test_cycle_error_message_longer_cycle_path():
    with pytest.raises(CycleError) as excinfo:
        topo_order({"a": {"b"}, "b": {"c"}, "c": {"a"}})
    assert str(excinfo.value) == "cycle detected in task registry: a -> b -> c -> a"


def test_cycle_error_message_self_cycle_path():
    with pytest.raises(CycleError) as excinfo:
        topo_order({"a": {"a"}})
    assert str(excinfo.value) == "cycle detected in task registry: a -> a"


def test_cycle_error_path_excludes_unrelated_tasks():
    reg = {"a": {"b"}, "b": {"a"}, "x": set()}
    with pytest.raises(CycleError) as excinfo:
        topo_order(reg)
    assert "x" not in str(excinfo.value)


def test_cycle_error_path_is_a_real_closed_loop():
    reg = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"c"}}
    with pytest.raises(CycleError) as excinfo:
        topo_order(reg)
    names = str(excinfo.value).split(": ", 1)[1].split(" -> ")
    assert names[0] == names[-1]
    assert all(nxt in reg[cur] for cur, nxt in zip(names, names[1:]))


def test_waves_cycle_error_message_names_cycle_path():
    with pytest.raises(CycleError) as excinfo:
        waves({"a": {"b"}, "b": {"a"}})
    assert str(excinfo.value) == "cycle detected in task registry: a -> b -> a"
