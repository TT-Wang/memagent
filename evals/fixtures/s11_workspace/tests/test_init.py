"""Tests for the taskdag package's public exports (taskdag/__init__.py)."""

import taskdag
from taskdag import *  # noqa: F401,F403


def test_all_names_exist_in_package():
    assert set(taskdag.__all__) <= set(dir(taskdag))


def test_all_names_cover_documented_public_api():
    expected = {
        "__version__",
        "CycleError",
        "Registry",
        "RETRY_LIMIT",
        "add_task",
        "by_tag",
        "dry_run",
        "failures",
        "merge",
        "remove_task",
        "results_to_json",
        "run",
        "summarize",
        "topo_order",
        "waves",
    }
    assert set(taskdag.__all__) == expected


def test_star_import_provides_the_public_api():
    assert Registry is taskdag.Registry
    assert CycleError is taskdag.CycleError
    assert add_task is taskdag.add_task
    assert remove_task is taskdag.remove_task
    assert by_tag is taskdag.by_tag
    assert merge is taskdag.merge
    assert topo_order is taskdag.topo_order
    assert waves is taskdag.waves
    assert run is taskdag.run
    assert dry_run is taskdag.dry_run
    assert RETRY_LIMIT == taskdag.RETRY_LIMIT
    assert summarize is taskdag.summarize
    assert failures is taskdag.failures
    assert results_to_json is taskdag.results_to_json
    assert __version__ == taskdag.__version__
