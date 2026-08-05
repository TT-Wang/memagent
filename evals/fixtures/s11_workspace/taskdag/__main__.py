"""Command-line interface for taskdag.

Run ``python -m taskdag`` with a command: ``plan`` prints the wave plan
of a small demo graph (via :func:`taskdag.scheduler.dry_run`);
``run-demo`` executes a 5-task demo graph with a retry limit of
exactly 3 and prints the retry limit plus the
:func:`taskdag.stats.summarize` line; ``version`` prints the installed
``taskdag`` version.
"""

import sys

from . import __version__
from .config import get
from .graph import add_task
from .scheduler import dry_run, run
from .stats import summarize

USAGE = "usage: python -m taskdag {plan|run-demo|version}"


def demo_graph():
    """Build the demo registry used by the ``plan`` command."""
    reg = {}
    add_task(reg, "fetch")
    add_task(reg, "parse", depends_on=("fetch",))
    add_task(reg, "compile", depends_on=("parse",))
    return reg


def demo_run_graph():
    """Build the 5-task demo registry used by the ``run-demo`` command."""
    reg = {}
    add_task(reg, "fetch")
    add_task(reg, "parse", depends_on=("fetch",))
    add_task(reg, "compile", depends_on=("parse",))
    add_task(reg, "test", depends_on=("compile",))
    add_task(reg, "deploy", depends_on=("test",))
    return reg


def main(argv=None):
    """Dispatch the CLI commands and return the process exit code.

    ``plan`` prints ``dry_run(demo_graph())`` and exits 0; ``run-demo``
    runs the 5-task ``demo_run_graph()`` with a retry limit of exactly 3
    (defaulting to the ``qz_demo_retry_limit`` config value; the
    ``--retry-limit 3`` flag is accepted, any other value is rejected)
    and prints the retry limit followed by the resulting ``summarize()``
    line (e.g. ``retry limit: 3`` / ``5 done, 0 failed, 0 skipped``)
    and exits 0; ``version`` prints :data:`taskdag.__version__` and
    exits 0.
    No command or an unknown command prints a message to stderr and
    exits 2.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    command = args[0]
    if command == "version":
        print(__version__)
        return 0
    if command == "plan":
        print(dry_run(demo_graph()))
        return 0
    if command == "run-demo":
        limit = get("qz_demo_retry_limit")
        rest = args[1:]
        if rest:
            if rest[0] != "--retry-limit" or len(rest) != 2:
                print(f"unknown argument for run-demo: {rest[0]}", file=sys.stderr)
                print(USAGE, file=sys.stderr)
                return 2
            try:
                limit = int(rest[1])
            except ValueError:
                print(f"invalid retry limit: {rest[1]!r}", file=sys.stderr)
                return 2
        if limit != 3:
            # Session rule: any retry limit is always exactly 3.
            print(
                f"retry limit must be exactly 3 (session rule), got {limit}",
                file=sys.stderr,
            )
            return 2
        result = run(demo_run_graph(), lambda name: name, retry_limit=limit)
        print(f"retry limit: {limit}")
        print(summarize(result)["summary"])
        return 0
    print(f"unknown command: {command}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
