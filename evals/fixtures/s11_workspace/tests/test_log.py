"""Tests for taskdag.log and the scheduler's logging integration."""

from taskdag.graph import add_task
from taskdag.log import RING, RING_SIZE, log
from taskdag.scheduler import run


def build(deps):
    """Build a plain-dict registry from a {name: depends_on} mapping."""
    reg = {}
    for name, depends_on in deps.items():
        add_task(reg, name, depends_on=depends_on)
    return reg


def test_ring_size_is_200():
    assert RING_SIZE == 200
    assert RING.maxlen == 200


def test_log_appends_entry():
    RING.clear()
    log("info", "hello")
    assert list(RING) == [("info", "hello")]


def test_log_appends_multiple_entries_in_order():
    RING.clear()
    log("debug", "first")
    log("error", "second")
    assert list(RING) == [("debug", "first"), ("error", "second")]


def test_ring_drops_oldest_beyond_capacity():
    RING.clear()
    for i in range(RING_SIZE + 5):
        log("info", f"msg {i}")
    entries = list(RING)
    assert len(entries) == RING_SIZE
    assert entries[0] == ("info", "msg 5")
    assert entries[-1] == ("info", f"msg {RING_SIZE + 4}")


def test_run_logs_start_and_done_for_each_task():
    RING.clear()
    run(build({"a": (), "b": ("a",)}), lambda name: name)
    entries = list(RING)
    assert ("info", "task a start") in entries
    assert ("info", "task a done") in entries
    assert ("info", "task b start") in entries
    assert ("info", "task b done") in entries


def test_run_logs_start_before_done():
    RING.clear()
    run(build({"a": ()}), lambda name: name)
    entries = list(RING)
    assert entries.index(("info", "task a start")) < entries.index(
        ("info", "task a done")
    )


def test_run_logs_failed_after_exhausting_retries():
    RING.clear()

    def fn(name):
        raise RuntimeError("boom")

    result = run(build({"a": ()}), fn)
    assert result["failed"] == ["a"]
    entries = list(RING)
    assert ("info", "task a start") in entries
    assert entries.count(("error", "task a failed")) == 1
    assert ("info", "task a done") not in entries


def test_run_retry_success_logs_start_and_done_once():
    RING.clear()
    reg = build({"a": ()})
    calls = []

    def fn(name):
        calls.append(name)
        if calls.count(name) < 3:
            raise RuntimeError("flaky")
        return name

    run(reg, fn)
    entries = list(RING)
    assert entries.count(("info", "task a start")) == 1
    assert entries.count(("info", "task a done")) == 1
    assert ("error", "task a failed") not in entries


def test_skipped_tasks_are_not_logged():
    RING.clear()
    reg = build({"a": (), "b": ("a",)})

    def fn(name):
        raise RuntimeError("boom")

    run(reg, fn)
    entries = list(RING)
    assert ("error", "task a failed") in entries
    assert ("info", "task b start") not in entries
    assert ("info", "task b done") not in entries
