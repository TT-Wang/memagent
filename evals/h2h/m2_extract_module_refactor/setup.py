import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def setup(workdir):
    """Seed a small, realistic 'taskq' package.

    The GOD MODULE `taskq/core.py` currently holds two cohesive retry symbols
    (the `RetryPolicy` class + the `compute_backoff` helper) alongside the
    unrelated `Job` / `JobResult` value objects. The refactor must EXTRACT the
    two retry symbols into a NEW module `taskq/retry.py` and REWIRE every
    importer (worker, dispatcher, queue, runner, the public __init__) to import
    them from `taskq.retry` instead of `taskq.core` -- with no dangling
    `from .core import RetryPolicy` / `from taskq.core import compute_backoff`
    references left anywhere.

    DISTRACTOR: `taskq/scheduler.py` defines its OWN, unrelated, local
    `RetryPolicy` (a cron-ish reschedule policy) that has nothing to do with the
    retry/backoff one. It must NOT be repointed to taskq.retry; a blanket
    find/replace of "RetryPolicy" or "from .core import" breaks it.
    """
    pkg = "taskq"

    # ------------------------------------------------------------------ __init__
    # Public surface RE-EXPORTS RetryPolicy + compute_backoff. After the move
    # these two re-exports MUST come from taskq.retry, not taskq.core.
    _w(workdir, os.path.join(pkg, "__init__.py"), '''\
"""taskq: a tiny in-process task queue with retry/backoff.

Public surface re-exported here for convenience.
"""
from .core import Job, JobResult, RetryPolicy, compute_backoff
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

    # ------------------------------------------------------------------ errors
    _w(workdir, os.path.join(pkg, "errors.py"), '''\
"""Exception hierarchy for taskq. (No retry symbols here.)"""


class TaskError(Exception):
    pass


class MaxRetriesExceeded(TaskError):
    def __init__(self, job_id, attempts):
        self.job_id = job_id
        self.attempts = attempts
        super().__init__(
            "job %r gave up after %d attempt(s)" % (job_id, attempts)
        )
''')

    # ------------------------------------------------------------------ core (GOD MODULE)
    # Holds Job/JobResult (stay) AND RetryPolicy/compute_backoff (MOVE OUT).
    _w(workdir, os.path.join(pkg, "core.py"), '''\
"""Core value objects AND the retry policy.

REFACTOR TARGET: `RetryPolicy` and `compute_backoff` are a cohesive retry
concern that does not belong in this grab-bag module. EXTRACT both into a new
module `taskq/retry.py` and update every importer. `Job` and `JobResult` stay
here.
"""
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
    """Decides whether/how long to wait before retrying a failed job.

    A retry policy is fully described by its base delay, max attempts, and an
    optional delay cap. `delay_for(attempt)` reuses `compute_backoff`.
    """

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

    # ------------------------------------------------------------------ worker (IMPORTER 1)
    _w(workdir, os.path.join(pkg, "worker.py"), '''\
"""Worker: runs a single job once and reports a JobResult.

IMPORTER: pulls RetryPolicy + compute_backoff from core. Must be rewired to
taskq.retry after the extraction.
"""
from .core import Job, JobResult, RetryPolicy, compute_backoff


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
        # Worker exposes the raw backoff too, sourced from the moved helper.
        return compute_backoff(self.policy.base, attempt, cap=self.policy.cap)
''')

    # ------------------------------------------------------------------ dispatcher (IMPORTER 2)
    _w(workdir, os.path.join(pkg, "dispatcher.py"), '''\
"""Dispatcher: decides retry vs give-up for a finished JobResult.

IMPORTER: uses RetryPolicy from core. Must be rewired to taskq.retry.
"""
from .core import RetryPolicy
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

    # ------------------------------------------------------------------ queue (IMPORTER 3)
    _w(workdir, os.path.join(pkg, "queue.py"), '''\
"""TaskQueue: an ordered queue of Jobs with a default RetryPolicy.

IMPORTER: imports both compute_backoff and RetryPolicy from core (qualified
module import style). Must be rewired to taskq.retry.
"""
from collections import deque

from . import core
from .core import compute_backoff


class TaskQueue:
    def __init__(self, policy=None):
        self._jobs = deque()
        # NOTE: qualified access through the `core` module object below.
        self.policy = policy or core.RetryPolicy()

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

    # ------------------------------------------------------------------ runner (IMPORTER 4)
    _w(workdir, os.path.join(pkg, "runner.py"), '''\
"""runner.run_until_done: drives a job through worker+dispatcher to completion.

IMPORTER: imports compute_backoff from core (used to sanity-bound delays).
Must be rewired to taskq.retry.
"""
from .core import compute_backoff
from .worker import Worker
from .dispatcher import Dispatcher
from .errors import MaxRetriesExceeded


def run_until_done(fn, job, policy=None, *, max_total_delay=None):
    """Run `job` via Worker/Dispatcher until it succeeds or retries are
    exhausted. Returns the final JobResult. Records the cumulative planned
    delay; if max_total_delay is set, delays are bounded by compute_backoff's
    own cap (kept here only to exercise the moved helper)."""
    worker = Worker(fn, policy=policy)
    dispatcher = Dispatcher(policy=worker.policy)
    total_delay = 0.0
    last = None
    while True:
        last = worker.run_once(job)
        action, delay = dispatcher.plan(last)
        if action == "done":
            return last
        # action == "retry": sanity-bound the delay via the moved helper.
        bounded = compute_backoff(delay, 1, cap=max_total_delay)
        total_delay += bounded
        # loop continues; dispatcher.plan raises MaxRetriesExceeded on give-up.
''')

    # ------------------------------------------------------------------ scheduler (DISTRACTOR)
    # Defines its OWN local RetryPolicy with a DIFFERENT meaning + API.
    # It must stay byte-identical. A blanket find/replace of "RetryPolicy"
    # or "from .core import" would wrongly touch this file.
    _w(workdir, os.path.join(pkg, "scheduler.py"), '''\
"""Scheduler: a SEPARATE concern (calendar-style rescheduling).

DISTRACTOR -- DO NOT change for this refactor.

This module defines its OWN, unrelated `RetryPolicy` describing how a *recurring*
job is rescheduled (by fixed interval), NOT the failure/backoff RetryPolicy in
core.py. It must not be repointed to taskq.retry. Note it also imports `Job`
from core, which is fine: `Job` is NOT being moved.
"""
from .core import Job


class RetryPolicy:
    """Calendar reschedule policy: fire again every `interval_minutes`.

    Deliberately the SAME NAME but a DIFFERENT, unrelated API (no backoff, no
    max_attempts). Repointing this to taskq.retry would be a bug.
    """

    def __init__(self, interval_minutes=60):
        self.interval_minutes = interval_minutes

    def next_run(self, now_minutes):
        return now_minutes + self.interval_minutes


class Scheduler:
    def __init__(self, policy=None):
        self.policy = policy or RetryPolicy()

    def schedule(self, job, now_minutes=0):
        assert isinstance(job, Job)
        return self.policy.next_run(now_minutes)
''')

    # ------------------------------------------------------------------ telemetry (DISTRACTOR 2)
    # Mentions 'compute_backoff' and 'RetryPolicy' only as PROSE / a docstring
    # and a string label. No import of the moved symbols. Must stay identical.
    _w(workdir, os.path.join(pkg, "telemetry.py"), '''\
"""Telemetry helpers. DO NOT change for this refactor.

The strings 'RetryPolicy' and 'compute_backoff' appear here only as human-
readable labels in emitted metrics, NOT as imports of the moved symbols.
"""


def metric_name(kind):
    # e.g. metric_name("RetryPolicy") -> "taskq.retrypolicy.count"
    return "taskq.%s.count" % (kind.lower(),)


KNOWN_KINDS = ("RetryPolicy", "compute_backoff", "Worker", "Dispatcher")
''')

    # ------------------------------------------------------------------ README
    _w(workdir, "README.md", '''\
# taskq

A tiny in-process task queue with retry/backoff.

```python
from taskq import TaskQueue, Worker, RetryPolicy, run_until_done, Job

policy = RetryPolicy(base=0.5, max_attempts=4)
result = run_until_done(lambda job: 1 / 0, Job("j1"), policy=policy)
```

`RetryPolicy` and `compute_backoff` describe the failure/backoff strategy.
The `scheduler` module has a separate, unrelated reschedule policy.
''')
