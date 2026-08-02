"""P2 deletion-gate read counters — per-channel, durable projection. No model/network.

Contract: read events land in the auto-materializing _compatibility_writes channel dict AND
(read channels only) in the durable knowledge runtime_metadata "read-counter:<channel>" row, so
a 7-day zero-read claim survives process restarts. Write channels stay in-process only."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sliceagent-core", "src"))

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def _memory(tmp):
    os.environ["SLICEAGENT_CACHE_DIR"] = tmp
    import sliceagent_cli.memory as memory_mod
    from sliceagent_cli.knowledge import KnowledgeRepository
    from sliceagent_cli.memory import LocalMemory
    m = LocalMemory.__new__(LocalMemory)
    m._knowledge = KnowledgeRepository(":memory:")
    assert hasattr(memory_mod, "_DURABLE_READ_CHANNELS")
    return m


@check
def read_channel_lands_in_process_and_durable_counters():
    m = _memory(tempfile.mkdtemp())
    m._record_compatibility_write("legacy_fts_read", succeeded=True)
    m._record_compatibility_write("legacy_fts_read", succeeded=True)
    assert m._compatibility_writes["legacy_fts_read"]["succeeded"] == 2
    row = m._knowledge.get_runtime_metadata("read-counter:legacy_fts_read")
    assert row and row["count"] == 2 and row["last_read_at"], row


@check
def write_channels_stay_in_process_only():
    m = _memory(tempfile.mkdtemp())
    m._record_compatibility_write("episodic_mirror", succeeded=True)
    assert m._compatibility_writes["episodic_mirror"]["succeeded"] == 1
    assert m._knowledge.get_runtime_metadata("read-counter:episodic_mirror") is None


@check
def failed_reads_do_not_advance_the_durable_counter():
    m = _memory(tempfile.mkdtemp())
    m._record_compatibility_write("history_alias_read", succeeded=False, error=OSError("io"))
    assert m._compatibility_writes["history_alias_read"]["failed"] == 1
    assert m._knowledge.get_runtime_metadata("read-counter:history_alias_read") is None


@check
def missing_knowledge_repo_never_breaks_the_read():
    m = _memory(tempfile.mkdtemp())
    m._knowledge = None
    m._record_compatibility_write("search_history_tool", succeeded=True)   # must not raise
    assert m._compatibility_writes["search_history_tool"]["succeeded"] == 1


@check
def all_gate_channels_are_declared_durable():
    import sliceagent_cli.memory as memory_mod
    assert memory_mod._DURABLE_READ_CHANNELS == {
        "legacy_fts_read", "history_alias_read", "search_history_tool",
        "episodic_consolidation_read", "topic_switch_slash",
    }


@check
def history_alias_choke_point_reports_the_channel():
    from sliceagent_cli.hippocampus import HistoryFS

    class _Mem:
        def __init__(self):
            self.reported = []

        def _record_compatibility_write(self, channel, *, succeeded, error=None):
            self.reported.append((channel, succeeded))

        def read_episodes(self, session_id, *, limit=None):
            return []

    mem = _Mem()
    HistoryFS(mem, "s-1")._lines_for("s-1")
    assert mem.reported == [("history_alias_read", True)], mem.reported


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
