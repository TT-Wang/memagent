"""Reference solution (VALIDATION ONLY -- never shown to benchmarked agents).

Extracts RetryPolicy + compute_backoff from taskq/core.py into a new module
taskq/retry.py, removes them from core.py, and rewires every importer (worker,
dispatcher, queue, runner) plus the public __init__ re-export to taskq.retry.
scheduler.py and telemetry.py are left untouched.
"""
import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def apply(workdir):
    pkg = "taskq"

    # 1) NEW module: the moved retry symbols live here now.
    _w(workdir, os.path.join(pkg, "retry.py"), '''\
"""Retry policy + backoff. Extracted out of core.py (Move Module refactor)."""


def compute_backoff(base, attempt, *, cap=None):
    """Exponential backoff delay for a given (1-based) attempt number.

    delay = base * 2 ** (attempt - 1), optionally capped at `cap`.
    """
    if attempt < 1:
        attempt = 1
    delay = base * (2 ** (attempt - 1))
    if cap is not None:
        delay = min(delay, cap)
    return delay


class RetryPolicy:
    """Decides whether/how long to wait before retrying a failed job."""

    def __init__(self, base=1.0, max_attempts=3, cap=None):
        self.base = base
        self.max_attempts = max_attempts
        self.cap = cap

    def should_retry(self, attempt):
        return attempt < self.max_attempts

    def delay_for(self, attempt):
        return compute_backoff(self.base, attempt, cap=self.cap)

    def describe(self):
        return "RetryPolicy(base=%s, max_attempts=%s, cap=%s)" % (
            self.base, self.max_attempts, self.cap,
        )
''')

    # 2) core.py: keep Job/JobResult, drop the moved symbols entirely.
    _w(workdir, os.path.join(pkg, "core.py"), '''\
"""Core value objects. (RetryPolicy + compute_backoff moved to taskq.retry.)"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Job:
    """A unit of work submitted to the queue."""
    job_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    attempts: int = 0


@dataclass
class JobResult:
    """The outcome of running a Job once."""
    job_id: str
    ok: bool
    value: Optional[Any] = None
    error: Optional[str] = None
    attempts: int = 0
''')

    # 3) Rewire importers to taskq.retry.
    _w(workdir, os.path.join(pkg, "worker.py"), '''\
"""Worker: runs a single job once and reports a JobResult."""
from .core import Job, JobResult
from .retry import RetryPolicy, compute_backoff


class Worker:
    def __init__(self, fn, policy=None):
        self.fn = fn
        self.policy = policy or RetryPolicy()

    def run_once(self, job):
        job.attempts += 1
        try:
            value = self.fn(job)
            return JobResult(job.job_id, True, value=value, attempts=job.attempts)
        except Exception as exc:  # noqa: BLE001
            return JobResult(job.job_id, False, error=repr(exc), attempts=job.attempts)

    def next_delay(self, attempt):
        return compute_backoff(self.policy.base, attempt, cap=self.policy.cap)
''')

    _w(workdir, os.path.join(pkg, "dispatcher.py"), '''\
"""Dispatcher: decides retry vs give-up for a finished JobResult."""
from .retry import RetryPolicy
from .errors import MaxRetriesExceeded


class Dispatcher:
    def __init__(self, policy=None):
        self.policy = policy or RetryPolicy()

    def plan(self, result):
        """Return ('retry', delay) or raise MaxRetriesExceeded on give-up."""
        if result.ok:
            return ("done", 0)
        if self.policy.should_retry(result.attempts):
            return ("retry", self.policy.delay_for(result.attempts))
        raise MaxRetriesExceeded(result.job_id, result.attempts)
''')

    _w(workdir, os.path.join(pkg, "queue.py"), '''\
"""TaskQueue: an ordered queue of Jobs with a default RetryPolicy."""
from collections import deque

from . import retry
from .retry import compute_backoff


class TaskQueue:
    def __init__(self, policy=None):
        self._jobs = deque()
        self.policy = policy or retry.RetryPolicy()

    def submit(self, job):
        self._jobs.append(job)
        return job.job_id

    def pop(self):
        return self._jobs.popleft() if self._jobs else None

    def __len__(self):
        return len(self._jobs)

    def peek_delay(self, attempt):
        return compute_backoff(self.policy.base, attempt, cap=self.policy.cap)
''')

    _w(workdir, os.path.join(pkg, "runner.py"), '''\
"""runner.run_until_done: drives a job through worker+dispatcher to completion."""
from .retry import compute_backoff
from .worker import Worker
from .dispatcher import Dispatcher
from .errors import MaxRetriesExceeded


def run_until_done(fn, job, policy=None, *, max_total_delay=None):
    worker = Worker(fn, policy=policy)
    dispatcher = Dispatcher(policy=worker.policy)
    total_delay = 0.0
    last = None
    while True:
        last = worker.run_once(job)
        action, delay = dispatcher.plan(last)
        if action == "done":
            return last
        bounded = compute_backoff(delay, 1, cap=max_total_delay)
        total_delay += bounded
''')

    # 4) Public re-export now points at taskq.retry.
    _w(workdir, os.path.join(pkg, "__init__.py"), '''\
"""taskq: a tiny in-process task queue with retry/backoff.

Public surface re-exported here for convenience.
"""
from .core import Job, JobResult
from .retry import RetryPolicy, compute_backoff
from .queue import TaskQueue
from .worker import Worker
from .dispatcher import Dispatcher
from .runner import run_until_done
from .errors import TaskError, MaxRetriesExceeded

__all__ = [
    "Job",
    "JobResult",
    "RetryPolicy",
    "compute_backoff",
    "TaskQueue",
    "Worker",
    "Dispatcher",
    "run_until_done",
    "TaskError",
    "MaxRetriesExceeded",
]
''')

    # 5) scheduler.py and telemetry.py are intentionally left untouched.
