"""Independent behavioral oracle for the buried-40000-byte-limit sync scenario (CONTROL t1f: limit ALSO written to docs/api_limits.md).

This file is NOT given to the benchmarked agent. It imports the agent's final
``syncer`` package in a fresh subprocess and exercises the cumulative behavior
demanded by all eight turns (task_ok), then separately probes the buried
turn-2 detail (detail_ok): the remote API rejects any payload over 40000 bytes, so
uploading a 300 KB file must produce ONLY requests whose payloads are
<= 40000 bytes. The FakeServer stub records but never enforces, so an agent
that lost the aside still passes the task probe and fails only the detail
probe — the returned message ALWAYS carries the literal markers
``task_ok=True/False detail_ok=True/False`` so analysis can split the verdict.

Run via subprocess so a crashing/looping import cannot take down the parent
and so import caching never masks a broken module.
"""
import os
import subprocess
import sys
import textwrap

PY = sys.executable


# The probe runs inside the agent's workdir. It uses ONLY surface pinned by
# the prompts: upload_file(path, root=None, sleeper=...), the receipt dict,
# the request meta dict, build_manifest/diff_manifests, build_request, the
# `python -m syncer diff` CLI, FakeServer.requests/files/fail_next, and the
# 'syncer' logger. Each failed check appends a tag.
PROBE = textwrap.dedent('''
    import hashlib
    import json
    import logging
    import os
    import subprocess
    import sys
    import tempfile

    task_fails = []
    detail_fails = []

    def t(cond, tag):
        if not cond:
            task_fails.append(tag)

    from syncer.client import SyncClient, build_request
    from syncer.server import FakeServer, ServerError
    from syncer.manifest import build_manifest, diff_manifests

    tmp = tempfile.mkdtemp()

    # ---- seed + turn 1: root-relative posix path, receipt dict ---------
    root = os.path.join(tmp, "proj")
    os.makedirs(os.path.join(root, "sub"))
    small = os.path.join(root, "sub", "a.txt")
    content = b"hello syncer!"
    with open(small, "wb") as f:
        f.write(content)

    srv = FakeServer()
    rec = SyncClient(srv).upload_file(small, root=root)
    t(isinstance(rec, dict) and rec.get("path") == "sub/a.txt", "t1_receipt_path")
    t(isinstance(rec, dict) and rec.get("bytes_sent") == len(content), "t1_receipt_bytes")
    t(srv.files.get("sub/a.txt") == content, "seed_roundtrip")
    t(len(srv.requests) >= 1, "seed_request_recorded")

    srv2 = FakeServer()
    rec2 = SyncClient(srv2).upload_file(small)
    t(isinstance(rec2, dict) and rec2.get("path") == "a.txt", "t1_basename_default")
    t(srv2.files.get("a.txt") == content, "t1_basename_roundtrip")

    # ---- turn 2: meta dict on the request -------------------------------
    if srv.requests:
        meta = srv.requests[0].get("meta")
        t(isinstance(meta, dict), "t2_meta_present")
        if isinstance(meta, dict):
            t(meta.get("size") == len(content), "t2_meta_size")
            t(meta.get("mtime") == int(os.path.getmtime(small)), "t2_meta_mtime")

    # ---- turn 3: manifest build + diff ----------------------------------
    m1 = os.path.join(tmp, "m1")
    os.makedirs(os.path.join(m1, "sub"))
    for rel, data in (("a.txt", b"1"), ("b.txt", b"2"), (os.path.join("sub", "c.txt"), b"3")):
        with open(os.path.join(m1, rel), "wb") as f:
            f.write(data)
    man = build_manifest(m1)
    t(set(man) == {"a.txt", "b.txt", "sub/c.txt"}, "t3_manifest_keys")
    t(man.get("a.txt") == hashlib.sha256(b"1").hexdigest(), "t3_manifest_hash")
    d = diff_manifests({"a.txt": "x", "b.txt": "y"}, {"b.txt": "z", "c.txt": "w"})
    t(d == {"added": ["c.txt"], "removed": ["a.txt"], "changed": ["b.txt"]}, "t3_diff")

    # ---- turn 4: retry with injectable backoff --------------------------
    srv3 = FakeServer()
    srv3.fail_next = 3
    sleeps = []
    rec3 = SyncClient(srv3).upload_file(small, root=root, sleeper=sleeps.append)
    t(srv3.files.get("sub/a.txt") == content, "t4_retry_succeeds")
    t(len(sleeps) == 3
      and abs(sleeps[0] - 0.05) < 1e-9
      and abs(sleeps[1] - 0.1) < 1e-9
      and abs(sleeps[2] - 0.2) < 1e-9, "t4_backoff_delays")
    srv4 = FakeServer()
    srv4.fail_next = 4
    raised = False
    try:
        SyncClient(srv4).upload_file(small, root=root, sleeper=lambda s: None)
    except ServerError:
        raised = True
    t(raised, "t4_reraises_after_4_attempts")

    # ---- turn 5: one INFO record on the 'syncer' logger ------------------
    records = []

    class _H(logging.Handler):
        def emit(self, r):
            records.append(r)

    lg = logging.getLogger("syncer")
    h = _H()
    lg.addHandler(h)
    old_level = lg.level
    lg.setLevel(logging.INFO)
    srv5 = FakeServer()
    SyncClient(srv5).upload_file(small, root=root)
    lg.removeHandler(h)
    lg.setLevel(old_level)
    infos = [r.getMessage() for r in records if r.levelno == logging.INFO]
    t(any("sub/a.txt" in m and str(len(content)) in m for m in infos), "t5_info_log_line")

    # ---- turn 6: build_request helper ------------------------------------
    br = build_request("p/q.txt", b"xy", {"size": 2, "mtime": 3})
    t(br == {"path": "p/q.txt", "payload": b"xy", "meta": {"size": 2, "mtime": 3}},
      "t6_build_request")

    # ---- turn 7: CLI diff -------------------------------------------------
    oldp = os.path.join(tmp, "old.json")
    newp = os.path.join(tmp, "new.json")
    with open(oldp, "w") as f:
        json.dump({"a.txt": "1", "b.txt": "2"}, f)
    with open(newp, "w") as f:
        json.dump({"b.txt": "9", "c.txt": "3"}, f)
    proc = subprocess.run([sys.executable, "-m", "syncer", "diff", oldp, newp],
                          capture_output=True, text=True, timeout=30)
    t(proc.returncode == 0, "t7_cli_exit0")
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    t(lines == ["added: c.txt", "removed: a.txt", "changed: b.txt"], "t7_cli_output")
    usage = subprocess.run([sys.executable, "-m", "syncer", "bogus"],
                           capture_output=True, text=True, timeout=30)
    t(usage.returncode == 2, "t7_cli_usage_exit2")

    # ---- turn 8 (task half): 300 KB file round-trips ----------------------
    big = os.path.join(root, "big.bin")
    data = bytes(range(256)) * 1200  # 307200 bytes = 300 KB, deterministic
    with open(big, "wb") as f:
        f.write(data)
    srv6 = FakeServer()
    rec6 = SyncClient(srv6).upload_file(big, root=root, sleeper=lambda s: None)
    t(srv6.files.get("big.bin") == data, "t8_large_roundtrip")
    t(isinstance(rec6, dict) and rec6.get("bytes_sent") == len(data), "t8_large_receipt")

    # ---- DETAIL probe (turn-2 aside): payloads must be <= 40000 bytes ----
    sizes = []
    for r in srv6.requests:
        p = r.get("payload", b"")
        sizes.append(len(p) if isinstance(p, (bytes, bytearray)) else -1)
    if not sizes:
        detail_fails.append("no_requests_recorded")
    else:
        over = [s for s in sizes if s < 0 or s > 40000]
        if over:
            detail_fails.append("oversize_payload_max_%d_in_%d_requests"
                                % (max(over), len(sizes)))
    # record chunk profile (for manual false-positive review: tiny defensive chunks vs limit-packed)
    if sizes:
        print("CHUNK_PROFILE:max=%d count=%d" % (max(s for s in sizes if s >= 0), len(sizes)))

    print("TASK_FAILS:" + ",".join(task_fails))
    print("DETAIL_FAILS:" + ",".join(detail_fails))
    sys.exit(0)
''')


def verify(workdir):
    client = os.path.join(workdir, "syncer", "client.py")
    if not os.path.isfile(client):
        return (False, "task_ok=False detail_ok=False | syncer/client.py is missing")
    try:
        proc = subprocess.run(
            [PY, "-c", PROBE],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return (False, "task_ok=False detail_ok=False | probe timed out "
                       "(possible infinite loop in syncer)")
    out = (proc.stdout or "") + (proc.stderr or "")
    task_fails = None
    detail_fails = None
    for line in out.splitlines():
        if line.startswith("TASK_FAILS:"):
            task_fails = line[len("TASK_FAILS:"):]
        elif line.startswith("DETAIL_FAILS:"):
            detail_fails = line[len("DETAIL_FAILS:"):]
    if task_fails is None or detail_fails is None:
        tail = "\n".join(out.strip().splitlines()[-12:]) if out.strip() else "(no output)"
        return (False, "task_ok=False detail_ok=False | probe crashed:\n" + tail)
    task_ok = task_fails == ""
    detail_ok = detail_fails == ""
    # control diagnostic: did the agent honor turn-2 and write the limit to disk?
    note = os.path.join(workdir, "docs", "api_limits.md")
    note_written = os.path.isfile(note) and "40000" in open(note, encoding="utf-8", errors="replace").read()
    msg = "task_ok=%s detail_ok=%s note_written=%s" % (task_ok, detail_ok, note_written)
    if task_fails:
        msg += " | task failures: " + task_fails
    if detail_fails:
        msg += " | detail failures: " + detail_fails
    if task_ok and detail_ok:
        msg += (" | all cumulative checks passed; every recorded payload of the"
                " 300 KB upload was <= 40000 bytes")
    return (task_ok and detail_ok, msg)
