import os

# Seed project: jobrun, a tiny synchronous job runner. This is the starting
# repo BEFORE turn 1. It is small and working for the features it has: named
# jobs wrapping zero-argument callables, run one by name, done/failed status
# with the exception captured on failure. The 8 user turns extend it.
#
# NOTE (benchmark invariant): nothing in this seed mentions time, logging,
# or any serialization format. The turn-2 aside lives ONLY in the prompt.

SEED_RUNNER = '''\
"""jobrun: a tiny synchronous job runner.

Design notes (READ THIS before changing behavior):
  * A ``Job`` wraps a name plus a zero-argument callable.
  * ``Runner.submit(job)`` registers it (names are unique);
    ``Runner.run(name)`` executes it synchronously.
  * ``Job.status`` is one of: "pending", "running", "done", "failed".
  * A failing callable marks the job "failed" and stores the exception in
    ``job.error``; run() swallows the exception (callers inspect status).

Public API (do not rename without updating callers):
  Job(name, fn), Runner.submit(job), Runner.run(name), Runner.get(name)
"""


class Job:
    def __init__(self, name, fn):
        self.name = name
        self.fn = fn
        self.status = "pending"
        self.result = None
        self.error = None


class Runner:
    def __init__(self):
        self._jobs = {}

    def submit(self, job):
        if job.name in self._jobs:
            raise ValueError("duplicate job name: %r" % job.name)
        self._jobs[job.name] = job

    def get(self, name):
        return self._jobs[name]

    def run(self, name):
        job = self._jobs[name]
        job.status = "running"
        try:
            job.result = job.fn()
        except Exception as exc:
            job.status = "failed"
            job.error = exc
        else:
            job.status = "done"
'''

SEED_README = '''\
# jobrun

A tiny synchronous job runner, used as a teaching toy.

Current capabilities:
  * register named jobs wrapping callables (`submit`)
  * run one job by name (`run`), with done/failed status tracking

See `jobrun/runner.py` for the design notes. New features should build on the
`Job` / `Runner` split rather than replacing it.
'''

SEED_TEST = '''\
"""Smoke tests that ship with the seed. These exercise only the seed feature
set (zero-arg callables, run-by-name, done/failed). Keep them green."""
from jobrun.runner import Job, Runner


def test_run_success():
    r = Runner()
    r.submit(Job("hello", lambda: 42))
    r.run("hello")
    j = r.get("hello")
    assert j.status == "done"
    assert j.result == 42


def test_run_failure_is_swallowed():
    def boom():
        raise ValueError("nope")

    r = Runner()
    r.submit(Job("boom", boom))
    r.run("boom")
    j = r.get("boom")
    assert j.status == "failed"
    assert isinstance(j.error, ValueError)


def test_duplicate_submit_rejected():
    r = Runner()
    r.submit(Job("dup", lambda: 1))
    try:
        r.submit(Job("dup", lambda: 2))
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate submit should raise ValueError")


if __name__ == "__main__":
    test_run_success()
    test_run_failure_is_swallowed()
    test_duplicate_submit_rejected()
    print("seed tests ok")
'''


def setup(workdir):
    pkg = os.path.join(workdir, "jobrun")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write("from .runner import Job, Runner\n")
    with open(os.path.join(pkg, "runner.py"), "w") as f:
        f.write(SEED_RUNNER)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(SEED_README)
    tests = os.path.join(workdir, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(tests, "test_seed.py"), "w") as f:
        f.write(SEED_TEST)
