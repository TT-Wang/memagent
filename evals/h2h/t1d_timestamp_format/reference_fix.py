import os

# Correct, full implementation of the job runner after all eight turns.
# VALIDATION ONLY -- never shown to the benchmarked agents.
#
# The buried detail (turn-2 aside) lives in _audit_ts(): audit timestamps are
# UTC ISO-8601 with a trailing 'Z'. Everything else is the cumulative feature
# work: args/kwargs, retries, FIFO+priority queue, cancellation, runpy CLI,
# summary/names, and the append-mode audit log.

REFERENCE_RUNNER = '''\
"""jobrun: a tiny synchronous job runner with retries, a priority queue,
cancellation, reporting helpers, and an audit log.

Design notes:
  * ``Runner._queue`` holds (priority, seq, name); run_queued() sorts by
    (-priority, seq) so higher priority runs first and equal priority is FIFO.
  * Audit timestamps are UTC ISO-8601 with a trailing 'Z' (ops' log parser
    requirement).
"""

from datetime import datetime, timezone


def _audit_ts():
    # UTC ISO-8601 with a trailing 'Z' -- ops' parser chokes on anything else.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class Job:
    def __init__(self, name, fn, args=(), kwargs=None, max_retries=0):
        self.name = name
        self.fn = fn
        self.args = tuple(args)
        self.kwargs = dict(kwargs or {})
        self.max_retries = max_retries
        self.status = "pending"
        self.result = None
        self.error = None
        self.attempts = 0


class Runner:
    def __init__(self, audit_path=None):
        self._jobs = {}
        self._queue = []  # (priority, seq, name)
        self._seq = 0
        self.audit_path = audit_path

    # ----- registration ----------------------------------------------------
    def submit(self, job):
        if job.name in self._jobs:
            raise ValueError("duplicate job name: %r" % job.name)
        self._jobs[job.name] = job

    def get(self, name):
        return self._jobs[name]

    # ----- audit -------------------------------------------------------------
    def _audit(self, line):
        if not self.audit_path:
            return
        with open(self.audit_path, "a") as f:
            f.write(line + "\\n")

    # ----- execution ---------------------------------------------------------
    def run(self, name):
        job = self._jobs[name]
        if job.status == "cancelled":
            raise RuntimeError("job %r is cancelled" % name)
        self._audit("%s START %s" % (_audit_ts(), job.name))
        job.status = "running"
        job.attempts = 0
        last_exc = None
        for _ in range(job.max_retries + 1):
            job.attempts += 1
            try:
                job.result = job.fn(*job.args, **job.kwargs)
            except Exception as exc:
                last_exc = exc
            else:
                job.status = "done"
                job.error = None
                break
        else:
            job.status = "failed"
            job.error = last_exc
            job.result = None
        self._audit("%s FINISH %s %s" % (_audit_ts(), job.name, job.status))
        return job.result

    # ----- queue -------------------------------------------------------------
    def enqueue(self, name, priority=0):
        if name not in self._jobs:
            raise KeyError(name)
        if any(entry[2] == name for entry in self._queue):
            raise ValueError("already queued: %r" % name)
        self._queue.append((priority, self._seq, name))
        self._seq += 1

    def run_queued(self):
        order = sorted(self._queue, key=lambda entry: (-entry[0], entry[1]))
        self._queue = []
        ran = []
        for _, _, name in order:
            if self._jobs[name].status == "cancelled":
                continue
            self.run(name)
            ran.append(name)
        return ran

    # ----- cancellation --------------------------------------------------------
    def cancel(self, name):
        job = self._jobs[name]
        if job.status != "pending":
            raise RuntimeError("cannot cancel job %r in status %r"
                               % (name, job.status))
        job.status = "cancelled"
        self._queue = [entry for entry in self._queue if entry[2] != name]

    # ----- reporting -------------------------------------------------------------
    def summary(self):
        counts = {}
        for job in self._jobs.values():
            counts[job.status] = counts.get(job.status, 0) + 1
        return counts

    def names(self, status=None):
        if status is None:
            return sorted(self._jobs)
        return sorted(n for n, j in self._jobs.items() if j.status == status)
'''

REFERENCE_CLI = '''\
"""Command-line interface for jobrun.

Spec files are plain Python files defining a top-level list ``JOBS`` of Job
instances; they are loaded with runpy.
"""
import argparse
import runpy
import sys

from .runner import Runner


def _load(spec_path):
    ns = runpy.run_path(spec_path)
    jobs = ns.get("JOBS")
    if not isinstance(jobs, list):
        raise SystemExit("spec file must define a top-level JOBS list")
    runner = Runner()
    for job in jobs:
        runner.submit(job)
    return runner, jobs


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jobrun")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--spec", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("name")
    p_run.add_argument("--spec", required=True)
    args = parser.parse_args(argv)

    runner, jobs = _load(args.spec)
    if args.cmd == "list":
        for job in jobs:
            print("%s %s" % (job.name, job.status))
        return 0
    # run
    if args.name not in [job.name for job in jobs]:
        print("unknown job: %s" % args.name, file=sys.stderr)
        return 2
    runner.run(args.name)
    print("%s: %s" % (args.name, runner.get(args.name).status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def apply(workdir):
    pkg = os.path.join(workdir, "jobrun")
    with open(os.path.join(pkg, "runner.py"), "w") as f:
        f.write(REFERENCE_RUNNER)
    with open(os.path.join(pkg, "cli.py"), "w") as f:
        f.write(REFERENCE_CLI)
