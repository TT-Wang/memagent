"""Producer-to-consumer control for change-set dependency protection. No model/network.

Review #122's blocking finding: the 40888ee closure-feeder cut also deleted the SOLE live
producer of s.protected_deps (SwapManager.prefetch's retriever.deps computation), while every
consumer survived — eviction exclusion (swap.evict), the beyond-read-budget render keep-set and
the full-render closure rule (seed.build_artifacts). The suite stayed green because surviving
tests manually assigned the field. This control NEVER assigns protected_deps by hand: it drives
prefetch() with a fake retriever and asserts the protection at every consumer. It fails on
40888ee (deps() never called, dependency evicted/omitted) and passes on the parent and on the
restored successor."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent_core.pfc import Slice  # noqa: E402
from sliceagent_core.swap import DEP_CEILING, SwapManager  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


class _FakeRetriever:
    """deps() answers only for the edited file; records every call for the ceiling assertion."""

    def __init__(self, dep_map):
        self.dep_map = dep_map
        self.calls = []

    def deps(self, path, limit=None):
        self.calls.append((path, limit))
        return self.dep_map.get(path, set())


def _workspace(files):
    root = tempfile.mkdtemp(prefix="dep-protection-")
    for name, body in files.items():
        with open(os.path.join(root, name), "w", encoding="utf-8") as f:
            f.write(body)
    return root


def _slice_with_edit(swap):
    s = Slice(); s.reset("refactor edited.py")
    swap.load(s, "edited.py", edited=True)
    swap.load(s, "caller.py")            # the already-open dependency of the edit
    return s


@check
def prefetch_populates_protection_from_the_retriever():
    retriever = _FakeRetriever({"edited.py": {"caller.py", "edited.py"}})
    swap = SwapManager(retriever=retriever)
    s = _slice_with_edit(swap)
    swap.prefetch(s)
    assert retriever.calls == [("edited.py", DEP_CEILING)], retriever.calls
    assert s.protected_deps == {"caller.py"}, \
        f"deps minus the edited set must be protected, got {s.protected_deps}"


@check
def protected_dependency_survives_eviction_behind_unrelated_reads():
    retriever = _FakeRetriever({"edited.py": {"caller.py"}})
    swap = SwapManager(retriever=retriever)
    s = _slice_with_edit(swap)
    swap.prefetch(s)
    for i in range(24):                   # push far past the exploratory read budget
        swap.load(s, f"unrelated_{i}.py")
    swap.evict(s)
    assert "caller.py" in s.active_files, \
        "the change set's dependency must stay RESIDENT behind later unrelated reads"


@check
def renderer_keeps_the_dependency_beyond_read_budget_and_in_full():
    from sliceagent_core.seed import build_artifacts

    big_caller = "\n".join(f"line_{i} = {i}" for i in range(1_500))   # over FULL_FILE_LINES
    root = _workspace({"edited.py": "def f():\n    return 1\n", "caller.py": big_caller,
                       **{f"unrelated_{i}.py": "x = 1\n" for i in range(3)}})

    class _Tools:
        def root(self):
            return root

        def read_text(self, path):
            return open(os.path.join(root, path), encoding="utf-8").read()

    retriever = _FakeRetriever({"edited.py": {"caller.py"}})
    swap = SwapManager(retriever=retriever)
    s = _slice_with_edit(swap)
    for i in range(3):
        swap.load(s, f"unrelated_{i}.py")
    swap.prefetch(s)
    rendered = build_artifacts(s, _Tools(), read_budget=2)
    assert "### caller.py" in rendered, \
        "a protected dependency must render even when >read_budget exploratory reads crowd it out"
    assert "lines — full" in rendered.split("### caller.py", 1)[1].split("```", 1)[0], \
        "a large protected dependency renders IN FULL (relevance closure), never as a partial view"


@check
def clearing_the_edit_set_clears_stale_protection():
    retriever = _FakeRetriever({"edited.py": {"caller.py"}})
    swap = SwapManager(retriever=retriever)
    s = _slice_with_edit(swap)
    swap.prefetch(s)
    assert s.protected_deps == {"caller.py"}
    s.edited_files = type(s.edited_files)()
    swap.prefetch(s)
    assert s.protected_deps == set(), "no live change set → no stale protection carried forward"


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
