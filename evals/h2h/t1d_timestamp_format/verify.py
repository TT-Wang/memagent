"""Independent behavioral oracle for the buried-detail timestamp-format scenario.

This file is NOT given to the benchmarked agent to edit. It imports the agent's
final ``jobrun`` package in a fresh subprocess and exercises the cumulative
behavior demanded by all eight turns.

SPLIT VERDICT: the probe accumulates two independent failure lists.
  * task_fails  -> everything the eight turns asked for EXCEPT the format
                   aside (args/kwargs, retries, queue, cancel, priorities,
                   CLI, summary/names, audit-log mechanics).
  * detail_fails -> ONLY the turn-2 aside: every audit timestamp must match
                   ^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d+)?Z$ AND
                   parse (as UTC) to the actual wall-clock window of the run,
                   so a local-time clock with a decorative 'Z' also fails.
verify() returns (task_ok and detail_ok, msg) where msg always contains the
literal markers ``task_ok=True/False detail_ok=True/False``.

Run via subprocess so a crashing/looping import cannot take down the parent and
so import caching never masks a broken module.
"""
import os
import re
import subprocess
import sys
import textwrap

PY = sys.executable


# The probe runs inside the agent's workdir. It uses ONLY the public API the
# turns established: Job(name, fn, args, kwargs, max_retries), submit/get/run,
# enqueue/run_queued, cancel, summary/names, cli.main, Runner(audit_path=...).
PROBE = textwrap.dedent(r'''
    import contextlib, io, os, re, sys, time
    from datetime import datetime, timezone

    task_fails = []
    detail_fails = []

    def t(cond, tag):
        if not cond:
            task_fails.append(tag)

    def d(cond, tag):
        if not cond:
            detail_fails.append(tag)

    def finish():
        print("TASK_FAILS:" + ",".join(task_fails))
        print("DETAIL_FAILS:" + ",".join(detail_fails))
        sys.exit(0 if not (task_fails or detail_fails) else 1)

    def cli_call(cli, argv):
        """Call cli.main tolerating both `return code` and `sys.exit(code)`."""
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(argv)
        except SystemExit as e:
            rc = e.code
        return (0 if rc is None else rc), out.getvalue(), err.getvalue()

    def parse_audit(path):
        """Return (raw_lines, parsed, shape_ok). parsed = (ts, kind, rest)."""
        with open(path) as f:
            raw = [ln.rstrip("\n") for ln in f if ln.strip()]
        parsed, ok = [], True
        for ln in raw:
            toks = ln.split()
            idx = None
            for i, tk in enumerate(toks):
                if tk in ("START", "FINISH"):
                    idx = i
                    break
            if idx is None or idx == 0:
                ok = False
                continue
            parsed.append((" ".join(toks[:idx]), toks[idx], toks[idx + 1:]))
        return raw, parsed, ok

    try:
        from jobrun.runner import Job, Runner

        # ---- seed behavior still intact --------------------------------
        r = Runner()
        r.submit(Job("ok", lambda: 42))
        def boom():
            raise ValueError("nope")
        r.submit(Job("bad", boom))
        r.run("ok")
        r.run("bad")
        t(r.get("ok").status == "done" and r.get("ok").result == 42, "seed_done")
        t(r.get("bad").status == "failed" and isinstance(r.get("bad").error, ValueError), "seed_failed")
        raised = False
        try:
            r.submit(Job("ok", lambda: 0))
        except ValueError:
            raised = True
        t(raised, "seed_dup_submit")

        # ---- turn 1: args/kwargs + run() returns result -----------------
        r = Runner()
        r.submit(Job("add", lambda a, b=0: a + b, args=(2,), kwargs={"b": 3}))
        t(r.run("add") == 5, "t1_run_returns_result")
        t(r.get("add").result == 5, "t1_result_stored")

        # ---- turn 2: retries --------------------------------------------
        calls = {"n": 0}
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("flake")
            return "recovered"
        r = Runner()
        r.submit(Job("flaky", flaky, max_retries=3))
        t(r.run("flaky") == "recovered", "t2_retry_recovers")
        j = r.get("flaky")
        t(j.status == "done" and j.attempts == 3, "t2_attempts_on_success")
        def alwaysfail():
            raise KeyError("x")
        r.submit(Job("dies", alwaysfail, max_retries=1))
        t(r.run("dies") is None, "t2_failed_returns_none")
        j = r.get("dies")
        t(j.status == "failed" and j.attempts == 2 and isinstance(j.error, KeyError), "t2_exhausted")
        r.submit(Job("clean", lambda: 1))
        r.run("clean")
        t(r.get("clean").attempts == 1, "t2_single_attempt")

        # ---- turn 3: FIFO queue ------------------------------------------
        r = Runner()
        order = []
        for nm in "abc":
            r.submit(Job(nm, lambda nm=nm: order.append(nm)))
        r.enqueue("b"); r.enqueue("a"); r.enqueue("c")
        ran = r.run_queued()
        t(ran == ["b", "a", "c"] and order == ["b", "a", "c"], "t3_fifo_order")
        t(r.run_queued() == [], "t3_queue_cleared")
        raised = False
        try:
            r.enqueue("zzz")
        except KeyError:
            raised = True
        t(raised, "t3_unknown_enqueue")
        r2 = Runner(); r2.submit(Job("x", lambda: None)); r2.enqueue("x")
        raised = False
        try:
            r2.enqueue("x")
        except ValueError:
            raised = True
        t(raised, "t3_dup_enqueue")

        # ---- turn 4: cancellation ----------------------------------------
        r = Runner()
        hit = {"c1": False}
        r.submit(Job("c1", lambda: hit.__setitem__("c1", True)))
        r.submit(Job("c2", lambda: "v"))
        r.enqueue("c1"); r.enqueue("c2")
        r.cancel("c1")
        t(r.get("c1").status == "cancelled", "t4_status")
        ran = r.run_queued()
        t(ran == ["c2"] and not hit["c1"], "t4_skipped_in_queue")
        raised = False
        try:
            r.run("c1")
        except RuntimeError:
            raised = True
        t(raised and not hit["c1"], "t4_run_cancelled_raises")
        raised = False
        try:
            r.cancel("c2")   # already done
        except RuntimeError:
            raised = True
        t(raised, "t4_cancel_nonpending_raises")

        # ---- turn 5: priorities ------------------------------------------
        r = Runner()
        order = []
        for nm in ["p0a", "p0b", "hi", "mid"]:
            r.submit(Job(nm, lambda nm=nm: order.append(nm)))
        r.enqueue("p0a")
        r.enqueue("hi", priority=5)
        r.enqueue("p0b")
        r.enqueue("mid", priority=2)
        ran = r.run_queued()
        t(ran == ["hi", "mid", "p0a", "p0b"], "t5_priority_order")
        t(order == ["hi", "mid", "p0a", "p0b"], "t5_priority_exec")

        # ---- turn 6: CLI --------------------------------------------------
        with open("cli_spec.py", "w") as f:
            f.write("from jobrun import Job\n"
                    "JOBS = [Job('alpha', lambda: 1), Job('beta', lambda: 2)]\n")
        import jobrun.cli as cli
        rc, out, err = cli_call(cli, ["list", "--spec", "cli_spec.py"])
        t(rc == 0, "t6_list_rc")
        t("alpha pending" in out and "beta pending" in out, "t6_list_output")
        t(out.find("alpha pending") < out.find("beta pending"), "t6_list_order")
        rc, out, err = cli_call(cli, ["run", "alpha", "--spec", "cli_spec.py"])
        t(rc == 0 and "alpha: done" in out, "t6_run_output")
        rc, out, err = cli_call(cli, ["run", "ghost", "--spec", "cli_spec.py"])
        t(rc == 2, "t6_unknown_rc")

        # ---- turn 7: summary / names ---------------------------------------
        r = Runner()
        r.submit(Job("s1", lambda: 1))
        r.submit(Job("s2", boom))
        r.submit(Job("s3", lambda: 3))
        r.submit(Job("s4", lambda: 4))
        r.run("s1"); r.run("s2"); r.cancel("s4")
        t(r.summary() == {"done": 1, "failed": 1, "pending": 1, "cancelled": 1}, "t7_summary")
        t(r.names() == ["s1", "s2", "s3", "s4"], "t7_names_all")
        t(r.names(status="failed") == ["s2"], "t7_names_filtered")

        # ---- turn 8: audit log (TASK mechanics) ----------------------------
        audit = os.path.abspath("audit_probe.log")
        if os.path.exists(audit):
            os.remove(audit)
        t_lo = time.time()
        r = Runner(audit_path=audit)
        r.submit(Job("a_ok", lambda: 1))
        def afail():
            raise RuntimeError("af")
        r.submit(Job("a_bad", afail, max_retries=1))
        r.submit(Job("a_q", lambda: 2))
        r.submit(Job("a_cancel", lambda: 3))
        r.cancel("a_cancel")
        r.run("a_ok")
        r.run("a_bad")
        r.enqueue("a_q")
        r.run_queued()
        if not os.path.isfile(audit):
            t(False, "t8_audit_file_missing")
            d(False, "detail_no_audit_file")
            finish()
        raw, parsed, shape_ok = parse_audit(audit)
        t(shape_ok, "t8_line_shape")
        t(len(raw) == 6, "t8_line_count")
        starts = [p for p in parsed if p[1] == "START"]
        fins = [p for p in parsed if p[1] == "FINISH"]
        t([p[2][0] for p in starts if p[2]] == ["a_ok", "a_bad", "a_q"], "t8_start_names")
        fin_map = {p[2][0]: p[2][1] for p in fins if len(p[2]) >= 2}
        t(fin_map.get("a_ok") == "done", "t8_finish_ok")
        t(fin_map.get("a_bad") == "failed", "t8_finish_failed")
        t(fin_map.get("a_q") == "done", "t8_finish_queued")
        t(all("a_cancel" not in p[2] for p in parsed), "t8_no_cancelled_lines")
        # one START/FINISH pair per execution, in order, retries included
        pair_ok = len(parsed) == 6
        if pair_ok:
            for i in range(0, 6, 2):
                if not (parsed[i][1] == "START" and parsed[i + 1][1] == "FINISH"
                        and parsed[i][2][:1] == parsed[i + 1][2][:1]):
                    pair_ok = False
        t(pair_ok, "t8_pairing")
        # append mode: another run adds exactly two lines
        r.submit(Job("a_ok2", lambda: 9))
        r.run("a_ok2")
        t_hi = time.time()
        raw2, parsed2, shape_ok2 = parse_audit(audit)
        t(shape_ok2 and len(raw2) == len(raw) + 2, "t8_append_mode")

        # ---- turn-2 aside (DETAIL): UTC ISO-8601 trailing-Z timestamps -----
        ts_list = [p[0] for p in parsed2]
        if not ts_list:
            d(False, "detail_no_timestamps")
        pat = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
        for ts in ts_list:
            if not pat.match(ts):
                d(False, "detail_regex_mismatch:" + ts.replace(",", ";")[:48])
                break
        else:
            for ts in ts_list:
                body = ts[:-1]
                if "." in body:
                    sec, frac = body.split(".", 1)
                    body = sec + "." + frac[:6]
                    fmt = "%Y-%m-%dT%H:%M:%S.%f"
                else:
                    fmt = "%Y-%m-%dT%H:%M:%S"
                try:
                    dtv = datetime.strptime(body, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    d(False, "detail_unparseable:" + ts.replace(",", ";")[:48])
                    break
                ep = dtv.timestamp()
                if not (t_lo - 30 <= ep <= t_hi + 30):
                    d(False, "detail_not_utc_clock:" + ts.replace(",", ";")[:48])
                    break

    except SystemExit:
        raise
    except BaseException as exc:
        task_fails.append("probe_exception:%s:%s"
                          % (type(exc).__name__, str(exc)[:120].replace(",", ";").replace("\n", " ")))
        detail_fails.append("probe_exception")
    finish()
''')


def verify(workdir):
    runner_py = os.path.join(workdir, "jobrun", "runner.py")
    if not os.path.isfile(runner_py):
        return (False, "jobrun/runner.py is missing | task_ok=False detail_ok=False")
    try:
        proc = subprocess.run(
            [PY, "-c", PROBE],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return (False, "probe timed out (possible infinite loop) | task_ok=False detail_ok=False")
    out = (proc.stdout or "") + (proc.stderr or "")
    m_task = re.search(r"TASK_FAILS:(.*)", out)
    m_det = re.search(r"DETAIL_FAILS:(.*)", out)
    if not m_task or not m_det:
        tail = "\n".join(out.strip().splitlines()[-10:]) or "(no output)"
        return (False, "probe produced no verdict markers | task_ok=False detail_ok=False\n" + tail)
    task_list = [x for x in m_task.group(1).strip().split(",") if x]
    det_list = [x for x in m_det.group(1).strip().split(",") if x]
    task_ok = not task_list
    detail_ok = not det_list
    msg = "task_ok=%s detail_ok=%s" % (task_ok, detail_ok)
    if task_list:
        msg += " | task fails: " + ",".join(task_list[:12])
    if det_list:
        msg += " | detail fails: " + ",".join(det_list[:12])
    if task_ok and detail_ok:
        msg += " | all cumulative checks passed; every audit timestamp is UTC ISO-8601 with a trailing Z"
    return (task_ok and detail_ok, msg)
