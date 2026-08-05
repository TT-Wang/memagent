"""Tests for the taskdag command-line interface (taskdag/__main__.py)."""

import subprocess
import sys

from taskdag import __version__


def run_cli(*args):
    """Run ``python -m taskdag`` with ``args`` in a subprocess."""
    return subprocess.run(
        [sys.executable, "-m", "taskdag", *args],
        capture_output=True,
        text=True,
    )


def test_version_prints_current_version():
    result = run_cli("version")
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.strip() == __version__


def test_plan_prints_dry_run_of_demo_graph():
    result = run_cli("plan")
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == "wave 1: fetch\nwave 2: parse\nwave 3: compile\n"


def test_plan_and_version_agree_with_scheduler():
    from taskdag.graph import add_task
    from taskdag.scheduler import dry_run

    reg = {}
    add_task(reg, "fetch")
    add_task(reg, "parse", depends_on=("fetch",))
    add_task(reg, "compile", depends_on=("parse",))
    result = run_cli("plan")
    assert result.stdout == dry_run(reg) + "\n"


def test_no_command_prints_usage_and_exits_two():
    result = run_cli()
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_unknown_command_prints_usage_and_exits_two():
    result = run_cli("frobnicate")
    assert result.returncode == 2
    assert "unknown command" in result.stderr
    assert "usage" in result.stderr


def test_run_demo_prints_summarize_line():
    result = run_cli("run-demo")
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == "retry limit: 3\n5 done, 0 failed, 0 skipped\n"


def test_run_demo_agrees_with_stats_summarize():
    from taskdag.__main__ import demo_run_graph
    from taskdag.scheduler import run
    from taskdag.stats import summarize

    result = run_cli("run-demo")
    expected = summarize(run(demo_run_graph(), lambda name: name))["summary"]
    assert result.stdout == "retry limit: 3\n" + expected + "\n"


def test_run_demo_prints_retry_limit_from_config():
    from taskdag.config import get

    result = run_cli("run-demo")
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith(f"retry limit: {get('qz_demo_retry_limit')}\n")


def test_run_demo_accepts_explicit_retry_limit_of_three():
    result = run_cli("run-demo", "--retry-limit", "3")
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == "retry limit: 3\n5 done, 0 failed, 0 skipped\n"


def test_run_demo_rejects_retry_limit_other_than_three():
    result = run_cli("run-demo", "--retry-limit", "5")
    assert result.returncode == 2
    assert "exactly 3" in result.stderr
    assert result.stdout == ""


def test_run_demo_rejects_non_integer_retry_limit():
    result = run_cli("run-demo", "--retry-limit", "many")
    assert result.returncode == 2
    assert "invalid retry limit" in result.stderr


def test_run_demo_rejects_unknown_extra_argument():
    result = run_cli("run-demo", "--nope")
    assert result.returncode == 2
    assert "usage" in result.stderr
