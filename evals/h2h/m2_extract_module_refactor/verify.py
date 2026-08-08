"""Independent oracle for the taskq extract/move-module refactor.

This file is NOT one the agent is asked to touch. It checks:

  (A) BEHAVIOR with FRESH inputs defined HERE (a job/handler the agent never
      saw), run in a subprocess against the real package: the moved symbols are
      importable from taskq.retry AND re-exported from taskq, the worker /
      dispatcher / queue / runner all use the SAME class object that lives in
      taskq.retry (proving every importer repointed to the new home, not a
      leftover duplicate), and the backoff math still works end to end.

  (B) STRUCTURE: the new module exists, core.py no longer defines/re-imports the
      moved symbols, and NO file has a dangling `...core import RetryPolicy /
      compute_backoff`. Every importer resolves the symbols from taskq.retry.

  (C) DISTRACTORS unchanged: scheduler.py and telemetry.py must be BYTE-IDENTICAL
      to the seed (compared against a fresh setup() into a temp dir), and the
      separate scheduler.RetryPolicy must still be a DIFFERENT class with its own
      interval API -- i.e. it was NOT repointed to taskq.retry.

Returns (passed, detail); detail names the FIRST inconsistency found.
"""
import ast
import os
import re
import subprocess
import sys
import tempfile


def _pkg(workdir):
    return os.path.join(workdir, "taskq")


def _read(path):
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Behavioral test: subprocess against the real package with FRESH inputs.
# ---------------------------------------------------------------------------
_BEHAVIOR = r'''
import sys, json
sys.path.insert(0, {workdir!r})

out = {{}}

# 1) The moved symbols import from the NEW home.
from taskq.retry import RetryPolicy as RP_retry, compute_backoff as cb_retry
# 2) ... and are re-exported from the package root.
from taskq import RetryPolicy as RP_pkg, compute_backoff as cb_pkg, Job, JobResult
# 3) Importers expose / use the same class object.
from taskq.worker import Worker, RetryPolicy as RP_worker, compute_backoff as cb_worker
from taskq.dispatcher import Dispatcher, RetryPolicy as RP_disp
from taskq.queue import TaskQueue, compute_backoff as cb_queue
from taskq import runner as runner_mod
from taskq.runner import run_until_done

# Identity: every importer must point at the SAME class/function objects that
# live in taskq.retry. A leftover duplicate in core (or a missed importer that
# still binds the old core symbol) breaks identity here.
out["rp_pkg_is_retry"] = (RP_pkg is RP_retry)
out["cb_pkg_is_retry"] = (cb_pkg is cb_retry)
out["rp_worker_is_retry"] = (RP_worker is RP_retry)
out["cb_worker_is_retry"] = (cb_worker is cb_retry)
out["rp_disp_is_retry"] = (RP_disp is RP_retry)
out["cb_queue_is_retry"] = (cb_queue is cb_retry)
out["cb_runner_is_retry"] = (getattr(runner_mod, "compute_backoff", None) is cb_retry)

# queue.py builds its default policy via a qualified module reference -- the
# instance it builds must be a taskq.retry.RetryPolicy now.
q = TaskQueue()
out["queue_default_policy_is_retry"] = (type(q.policy) is RP_retry)

# core.py must NOT expose the moved symbols anymore.
import taskq.core as core_mod
out["core_has_retrypolicy"] = hasattr(core_mod, "RetryPolicy")
out["core_has_compute_backoff"] = hasattr(core_mod, "compute_backoff")
# but core keeps Job/JobResult.
out["core_has_job"] = hasattr(core_mod, "Job")
out["core_has_jobresult"] = hasattr(core_mod, "JobResult")

# 4) FRESH behavioral drive (inputs defined right here, agent never saw them).
# A job whose fn fails the first 2 attempts then succeeds on the 3rd.
state = {{"calls": 0}}
def flaky(job):
    state["calls"] += 1
    if state["calls"] < 3:
        raise ValueError("boom %d" % state["calls"])
    return "ok-%d" % state["calls"]

policy = RP_pkg(base=2.0, max_attempts=5, cap=10.0)
res = run_until_done(flaky, Job("fresh-job"), policy=policy)
out["final_ok"] = res.ok
out["final_value"] = res.value
out["final_attempts"] = res.attempts

# backoff math must be preserved (base=2, attempt=4 -> 2*2**3=16, capped @10).
out["cb_4_capped"] = cb_pkg(2.0, 4, cap=10.0)   # expect 10.0
out["cb_3_uncapped"] = cb_pkg(2.0, 3)           # expect 8.0
p2 = RP_pkg(base=1.0, max_attempts=3, cap=None)
out["delay_for_3"] = p2.delay_for(3)            # 1*2**2 = 4
out["should_retry_2"] = p2.should_retry(2)      # True (2<3)
out["should_retry_3"] = p2.should_retry(3)      # False (3<3 is False)
out["describe"] = p2.describe()

# Dispatcher give-up path with a fresh failing job.
from taskq.errors import MaxRetriesExceeded
giveup_policy = RP_pkg(base=1.0, max_attempts=2)
raised = False
try:
    run_until_done(lambda job: 1 / 0, Job("doomed"), policy=giveup_policy)
except MaxRetriesExceeded as e:
    raised = True
    out["giveup_attempts"] = e.attempts
out["giveup_raised"] = raised

# 5) DISTRACTOR scheduler: its RetryPolicy is a DIFFERENT class, NOT repointed.
from taskq.scheduler import Scheduler, RetryPolicy as RP_sched
out["sched_rp_is_retry"] = (RP_sched is RP_retry)   # must be False
out["sched_rp_has_interval"] = hasattr(RP_sched(), "interval_minutes")
out["sched_next_run"] = RP_sched(interval_minutes=30).next_run(100)  # 130
sched = Scheduler()
out["sched_schedule"] = sched.schedule(Job("recurring"), now_minutes=0)  # 60

print("JSON_START")
print(json.dumps(out, default=str))
'''


def _run_behavior(workdir):
    script = _BEHAVIOR.format(workdir=workdir)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return None, "behavioral test raised:\n" + (proc.stderr or proc.stdout)
    out = proc.stdout
    if "JSON_START" not in out:
        return None, "behavioral test produced no JSON:\n" + out
    import json
    blob = out.split("JSON_START", 1)[1].strip()
    try:
        return json.loads(blob), ""
    except Exception as e:  # noqa: BLE001
        return None, "could not parse behavioral JSON (%r):\n%s" % (e, blob)


def _check_behavior(workdir):
    data, err = _run_behavior(workdir)
    if data is None:
        return False, err

    # --- moved symbols live in taskq.retry and are re-exported from package ---
    if not data.get("rp_pkg_is_retry"):
        return False, "taskq re-exports RetryPolicy but NOT from taskq.retry (it is a different object)"
    if not data.get("cb_pkg_is_retry"):
        return False, "taskq re-exports compute_backoff but NOT from taskq.retry (different object)"

    # --- every importer points at the moved objects (no leftover/duplicate) ---
    if not data.get("rp_worker_is_retry"):
        return False, "worker.py's RetryPolicy is not taskq.retry.RetryPolicy (still bound to core or a duplicate)"
    if not data.get("cb_worker_is_retry"):
        return False, "worker.py's compute_backoff is not taskq.retry.compute_backoff"
    if not data.get("rp_disp_is_retry"):
        return False, "dispatcher.py's RetryPolicy is not taskq.retry.RetryPolicy"
    if not data.get("cb_queue_is_retry"):
        return False, "queue.py's compute_backoff is not taskq.retry.compute_backoff"
    if not data.get("queue_default_policy_is_retry"):
        return False, "queue.py still builds its default policy from the OLD location (qualified core.RetryPolicy not repointed)"
    if not data.get("cb_runner_is_retry"):
        return False, "runner.py's compute_backoff is not taskq.retry.compute_backoff (importer not rewired)"

    # --- core.py no longer exposes the moved symbols, keeps Job/JobResult ---
    if data.get("core_has_retrypolicy"):
        return False, "taskq.core still defines/imports RetryPolicy; it must be removed from core after the move"
    if data.get("core_has_compute_backoff"):
        return False, "taskq.core still defines/imports compute_backoff; it must be removed from core after the move"
    if not (data.get("core_has_job") and data.get("core_has_jobresult")):
        return False, "taskq.core must KEEP Job and JobResult"

    # --- fresh behavioral drive: retry-then-succeed ---
    if not data.get("final_ok") or data.get("final_value") != "ok-3":
        return False, "run_until_done did not succeed on the 3rd attempt as expected (got %r)" % (data.get("final_value"),)
    if data.get("final_attempts") != 3:
        return False, "expected 3 attempts before success, got %r" % (data.get("final_attempts"),)

    # --- backoff math preserved ---
    if float(data.get("cb_4_capped")) != 10.0:
        return False, "compute_backoff(2.0, 4, cap=10.0) should be 10.0, got %r" % (data.get("cb_4_capped"),)
    if float(data.get("cb_3_uncapped")) != 8.0:
        return False, "compute_backoff(2.0, 3) should be 8.0, got %r" % (data.get("cb_3_uncapped"),)
    if float(data.get("delay_for_3")) != 4.0:
        return False, "RetryPolicy(base=1).delay_for(3) should be 4.0, got %r" % (data.get("delay_for_3"),)
    if data.get("should_retry_2") is not True or data.get("should_retry_3") is not False:
        return False, "RetryPolicy.should_retry semantics changed (attempt<max_attempts)"
    if data.get("describe") != "RetryPolicy(base=1.0, max_attempts=3, cap=None)":
        return False, "RetryPolicy.describe() output changed: %r" % (data.get("describe"),)

    # --- give-up path ---
    if not data.get("giveup_raised"):
        return False, "run_until_done must raise MaxRetriesExceeded when retries are exhausted"
    if data.get("giveup_attempts") != 2:
        return False, "MaxRetriesExceeded should report 2 attempts for max_attempts=2, got %r" % (data.get("giveup_attempts"),)

    # --- DISTRACTOR: scheduler.RetryPolicy must NOT be repointed ---
    if data.get("sched_rp_is_retry"):
        return False, "scheduler.RetryPolicy was WRONGLY repointed to taskq.retry; it is a separate, unrelated class"
    if not data.get("sched_rp_has_interval"):
        return False, "scheduler.RetryPolicy lost its own API (interval_minutes); it must stay the distractor class"
    if data.get("sched_next_run") != 130:
        return False, "scheduler.RetryPolicy.next_run changed; distractor was modified (got %r)" % (data.get("sched_next_run"),)
    if data.get("sched_schedule") != 60:
        return False, "scheduler.Scheduler.schedule changed; distractor was modified (got %r)" % (data.get("sched_schedule"),)

    return True, "behavior OK"


# ---------------------------------------------------------------------------
# Structural checks (source-level): new module present, no dangling old import.
# ---------------------------------------------------------------------------
def _iter_py(pkg):
    for root, _dirs, files in os.walk(pkg):
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(root, fn)


def _check_new_module(workdir):
    pkg = _pkg(workdir)
    retry = os.path.join(pkg, "retry.py")
    if not os.path.exists(retry):
        return False, "new module taskq/retry.py was not created"
    src = _read(retry)
    if not re.search(r"\bdef\s+compute_backoff\s*\(", src):
        return False, "taskq/retry.py must define compute_backoff"
    if not re.search(r"\bclass\s+RetryPolicy\b", src):
        return False, "taskq/retry.py must define class RetryPolicy"
    return True, "new module ok"


def _check_no_dangling_core_import(workdir):
    """No file may import RetryPolicy/compute_backoff from core (relative or
    absolute), and core.py itself must not define them."""
    pkg = _pkg(workdir)
    # `from .core import ...` / `from taskq.core import ...` / `from ..core import ...`
    core_from = re.compile(
        r"from\s+(?:\.+|(?:\w+\.)*)core\s+import\s+(.+)")
    offenders = []
    for p in _iter_py(pkg):
        src = _read(p)
        rel = os.path.relpath(p, workdir)
        for m in core_from.finditer(src):
            imported = m.group(1)
            # strip trailing comment
            imported = imported.split("#", 1)[0]
            names = re.split(r"[,\s()]+", imported)
            if any(n in ("RetryPolicy", "compute_backoff") for n in names):
                offenders.append("%s: dangling 'from ...core import' of a moved symbol -> %s"
                                 % (rel, m.group(0).strip()))
    # core.py must not (re)define the moved symbols.
    core = os.path.join(pkg, "core.py")
    if os.path.exists(core):
        csrc = _read(core)
        if re.search(r"\bclass\s+RetryPolicy\b", csrc):
            offenders.append("core.py: still defines class RetryPolicy")
        if re.search(r"\bdef\s+compute_backoff\s*\(", csrc):
            offenders.append("core.py: still defines def compute_backoff")
    if offenders:
        return False, "dangling old references remain:\n  " + "\n  ".join(sorted(set(offenders)))
    return True, "no dangling core imports"


def _check_importers_rewired(workdir):
    """worker/dispatcher/queue/runner must import the moved symbols from
    taskq.retry (relative `.retry`)."""
    pkg = _pkg(workdir)
    needs = {
        "worker.py": ("RetryPolicy", "compute_backoff"),
        "dispatcher.py": ("RetryPolicy",),
        "runner.py": ("compute_backoff",),
    }
    for fn, symbols in needs.items():
        src = _read(os.path.join(pkg, fn))
        retry_imports = re.findall(
            r"from\s+\.retry\s+import\s+(.+)", src)
        joined = " ".join(retry_imports)
        for sym in symbols:
            if sym not in joined:
                return False, "%s must import %s from .retry (got retry-imports: %r)" % (fn, sym, retry_imports)
    # queue.py: both the qualified module import and the direct one must target retry.
    qsrc = _read(os.path.join(pkg, "queue.py"))
    if "compute_backoff" not in " ".join(re.findall(r"from\s+\.retry\s+import\s+(.+)", qsrc)):
        return False, "queue.py must import compute_backoff from .retry"
    if not re.search(r"(from\s+\.\s+import\s+retry\b)|(import\s+taskq\.retry\b)|(\bretry\.RetryPolicy\s*\()", qsrc):
        return False, "queue.py must resolve its qualified RetryPolicy() through taskq.retry (e.g. `from . import retry` then `retry.RetryPolicy()`)"
    # and queue.py must no longer reference core.RetryPolicy(
    if re.search(r"\bcore\.RetryPolicy\s*\(", qsrc):
        return False, "queue.py still calls core.RetryPolicy(...) -- the qualified reference was not repointed"
    return True, "importers rewired"


def _check_init_reexport(workdir):
    src = _read(os.path.join(_pkg(workdir), "__init__.py"))
    retry_imports = " ".join(re.findall(r"from\s+\.retry\s+import\s+(.+)", src))
    if "RetryPolicy" not in retry_imports or "compute_backoff" not in retry_imports:
        return False, "__init__.py must re-export RetryPolicy and compute_backoff from .retry"
    # __all__ must still advertise both names (public surface unchanged).
    for name in ("RetryPolicy", "compute_backoff", "Job", "JobResult",
                 "TaskQueue", "Worker", "Dispatcher", "run_until_done"):
        if ('"%s"' % name) not in src and ("'%s'" % name) not in src:
            return False, "__init__.py __all__ no longer advertises %r (public surface changed)" % (name,)
    return True, "init re-export ok"


def _check_distractors_byte_identical(workdir):
    """scheduler.py and telemetry.py must be BYTE-IDENTICAL to a fresh seed."""
    # Re-run the scenario's own setup() into a throwaway dir to recover the
    # canonical seed bytes, then compare. This makes the oracle independent of
    # any hand-copied expected content.
    here = os.path.dirname(os.path.abspath(__file__))
    import importlib.util
    spec = importlib.util.spec_from_file_location("_m2_setup", os.path.join(here, "setup.py"))
    setup_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup_mod)

    tmp = tempfile.mkdtemp(prefix="m2-seed-ref-")
    setup_mod.setup(tmp)
    for rel in ("taskq/scheduler.py", "taskq/telemetry.py"):
        seed_path = os.path.join(tmp, rel)
        cur_path = os.path.join(workdir, rel)
        if not os.path.exists(cur_path):
            return False, "distractor file removed: %s" % (rel,)
        with open(seed_path, "rb") as f:
            seed_bytes = f.read()
        with open(cur_path, "rb") as f:
            cur_bytes = f.read()
        if seed_bytes != cur_bytes:
            return False, "distractor %s was modified (must stay BYTE-IDENTICAL to the seed)" % (rel,)
    return True, "distractors byte-identical"


def _check_syntax(workdir):
    pkg = _pkg(workdir)
    for p in _iter_py(pkg):
        try:
            ast.parse(_read(p))
        except SyntaxError as e:
            return False, "syntax error in %s: %s" % (os.path.relpath(p, workdir), e)
    return True, "syntax ok"


def verify(workdir):
    checks = [
        ("syntax", _check_syntax),
        ("new_module", _check_new_module),
        ("no_dangling_core_import", _check_no_dangling_core_import),
        ("importers_rewired", _check_importers_rewired),
        ("init_reexport", _check_init_reexport),
        ("distractors_byte_identical", _check_distractors_byte_identical),
        ("behavior", _check_behavior),
    ]
    for name, fn in checks:
        ok, detail = fn(workdir)
        if not ok:
            return False, "[%s] %s" % (name, detail)
    return True, "all checks passed: retry concern extracted to taskq.retry and every importer rewired consistently"
