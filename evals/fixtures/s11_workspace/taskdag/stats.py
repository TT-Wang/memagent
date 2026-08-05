"""Summaries, failure reports and JSON export of run() results."""

import json


def summarize(result):
    """Return task counts and a one-line human summary of ``result``.

    ``result`` is the dict returned by :func:`taskdag.scheduler.run`,
    whose ``'done'``, ``'failed'`` and ``'skipped'`` lists partition the
    registry's tasks. The returned dict holds the ``'total'``, ``'done'``,
    ``'failed'`` and ``'skipped'`` counts plus ``'summary'``, a one-line
    human-readable string such as ``"2 done, 1 failed, 1 skipped"``.
    """
    done = len(result["done"])
    failed = len(result["failed"])
    skipped = len(result["skipped"])
    return {
        "total": done + failed + skipped,
        "done": done,
        "failed": failed,
        "skipped": skipped,
        "summary": f"{done} done, {failed} failed, {skipped} skipped",
    }



def results_to_json(result, path):
    """Write ``result`` to ``path`` as a JSON document.

    ``result`` is the dict returned by :func:`taskdag.scheduler.run`;
    every value must be JSON-serializable (lists, ints, strings). The
    file at ``path`` is created or overwritten with the JSON encoding,
    preserving the result dict's key order.
    """
    with open(path, "w") as f:
        json.dump(result, f)


def failures(result):
    """Return ``"name: attempts"`` lines for every failed task.

    ``result`` is the dict returned by :func:`taskdag.scheduler.run`,
    whose ``'retries'`` entry maps each failed task's name to the number
    of ``fn`` calls made for it (the retries plus the final failing
    attempt). Returns one ``"name: attempts"`` string per failed task,
    in the same (topological) order as the result's ``'failed'`` list;
    ``[]`` when nothing failed.
    """
    return [
        f"{name}: {attempts}"
        for name, attempts in result.get("retries", {}).items()
    ]
