#!/usr/bin/env python3
"""Generate the two 50-turn accumulation scenarios (s7_flagtable_50, s8_router_50).

These two exist to make CONTEXT ACCUMULATION visible, not to be hard: every turn is a small,
concrete edit (2-4 steps), but turns reference state created many turns earlier, so no turn can be
skipped and the session cannot be compressed into one ask. Over 50 such turns a transcript agent's
per-call input grows roughly linearly while a bounded-slice agent's should stay flat — that curve
is the deliverable.

Generated, not hand-written: 50 interdependent prompts hand-authored WILL drift from their own
verifier. Here one op-list drives everything — the prompts, the reference implementation, and the
verifier's expected state are all rendered from the same replay, so they cannot disagree. The op
sequence is deterministic (seeded RNG with validity constraints); re-running the generator is
idempotent.

  python benchmarks/multiturn_coding/_gen_long50.py          # writes/overwrites both scenario dirs
  python benchmarks/multiturn_coding/_gen_long50.py --check  # also red-green both against s1's contract
"""
from __future__ import annotations

import argparse
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- s7: feature-flag registry
S7_SEED = '''\
"""Feature-flag registry for the app.

Conventions (KEEP THESE — reviewers reject violations):
  * flag names are kebab-case strings
  * each flag is a dict with exactly two fields: "default" (bool) and "owner" (str team name)
  * retiring a flag MOVES its entry (unchanged) from FLAGS to RETIRED — never delete history
"""

FLAGS = {
    "dark-mode": {"default": False, "owner": "web"},
    "new-checkout": {"default": False, "owner": "payments"},
    "beta-search": {"default": True, "owner": "search"},
}

RETIRED = {}


def is_enabled(name: str) -> bool:
    return bool(FLAGS.get(name, {}).get("default", False))
'''

S7_WORDS = ["uploads", "billing", "avatars", "exports", "digest", "mobile-nav", "checkout-v3",
            "retries", "audit-trail", "quotas", "webhooks", "dashboards", "invites", "backups",
            "profiles", "sessions", "rate-limit", "previews", "themes", "search-v2", "imports",
            "alerts", "sandbox", "receipts", "labels", "archives", "metrics", "sso", "comments"]
S7_TEAMS = ["platform", "payments", "web", "search", "infra", "growth", "mobile", "data"]


def gen_s7():
    rng = random.Random(7)
    flags = {"dark-mode": {"default": False, "owner": "web"},
             "new-checkout": {"default": False, "owner": "payments"},
             "beta-search": {"default": True, "owner": "search"}}
    retired: dict = {}
    born: dict[str, int] = {k: 0 for k in flags}
    prompts, words = [], list(S7_WORDS)
    rng.shuffle(words)

    def alive_older(turn, min_age=3):
        return [n for n in flags if turn - born[n] >= min_age]

    add_t = ["Add a feature flag called {n}, default off, owned by the {o} team.",
             "We need a new flag: {n}. Start it disabled; {o} owns it.",
             "Register a {n} flag for the {o} team, off by default.",
             "Create flag {n} (owner {o}). Ship it dark — default false."]
    flip_t = ["Flip the default of {n} — it has been stable long enough.",
              "Turn {n} on by default now.", "Enable {n} for everyone (default true)."]
    ren_t = ["Rename the {a} flag to {b}; keep everything else about it as is.",
             "{a} is a confusing name — call it {b} instead, same default and owner."]
    chown_t = ["Ownership change: {n} belongs to the {o} team now.",
               "Move {n} under the {o} team."]
    ret_t = ["Retire {n} — the rollout is done.",
             "{n} is fully launched; retire the flag."]

    for turn in range(1, 51):
        ops = ["add"]
        if alive_older(turn):
            ops += ["flip", "flip", "rename", "chown"]
            if len(flags) > 6:
                ops.append("retire")
        op = rng.choice(ops) if turn > 6 and words else ("add" if words else rng.choice(["flip", "chown"]))
        if op == "add":
            n = f"{words.pop()}"
            o = rng.choice(S7_TEAMS)
            flags[n] = {"default": False, "owner": o}
            born[n] = turn
            prompts.append(rng.choice(add_t).format(n=n, o=o))
        elif op == "flip":
            n = rng.choice(alive_older(turn))
            flags[n]["default"] = not flags[n]["default"]
            verb = "on" if flags[n]["default"] else "off"
            prompts.append(rng.choice(flip_t).format(n=n) if flags[n]["default"]
                           else f"Actually turn {n} back {verb} by default.")
        elif op == "rename":
            a = rng.choice(alive_older(turn))
            b = a + "-v2" if not a.endswith("-v2") else a.replace("-v2", "-next")
            flags[b] = flags.pop(a)
            born[b] = born.pop(a)
            prompts.append(rng.choice(ren_t).format(a=a, b=b))
        elif op == "chown":
            n = rng.choice(alive_older(turn))
            o = rng.choice([t for t in S7_TEAMS if t != flags[n]["owner"]])
            flags[n]["owner"] = o
            prompts.append(rng.choice(chown_t).format(n=n, o=o))
        else:  # retire
            n = rng.choice(alive_older(turn, min_age=5))
            retired[n] = flags.pop(n)
            born.pop(n)
            prompts.append(rng.choice(ret_t).format(n=n))
    return prompts, flags, retired


# ---------------------------------------------------------------- s8: URL router table
S8_SEED = '''\
"""Tiny URL router.

ROUTES is an ordered list of entries. Two entry shapes:
  {"path": "/x", "endpoint": "handler_name"}            — a terminal route
  {"path": "/old", "redirect_to": "/new"}               — a redirect (resolve() follows it)

resolve(path) returns the endpoint name after following redirects (max 5 hops), or None.
Keep entries EXACT — tests compare this table literally.
"""

ROUTES = [
    {"path": "/", "endpoint": "home"},
    {"path": "/login", "endpoint": "auth_login"},
    {"path": "/docs", "endpoint": "docs_index"},
]


def resolve(path: str, _depth: int = 0):
    if _depth > 5:
        return None
    for r in ROUTES:
        if r["path"] == path:
            if "redirect_to" in r:
                return resolve(r["redirect_to"], _depth + 1)
            return r["endpoint"]
    return None
'''

S8_PARTS = ["reports", "teams", "billing", "settings", "export", "profile", "search", "admin",
            "invoices", "webhooks", "usage", "tokens", "alerts", "audit", "labels", "archive",
            "keys", "plans", "digest", "status", "help", "jobs", "runs", "files", "notes"]


def gen_s8():
    rng = random.Random(8)
    routes = [{"path": "/", "endpoint": "home"},
              {"path": "/login", "endpoint": "auth_login"},
              {"path": "/docs", "endpoint": "docs_index"}]
    born = {"/": 0, "/login": 0, "/docs": 0}
    prompts, parts = [], list(S8_PARTS)
    rng.shuffle(parts)

    def terminals(turn, age=3):
        return [r for r in routes if "endpoint" in r and turn - born[r["path"]] >= age
                and r["path"] != "/"]

    add_t = ["Add a route: {p} should hit the {e} handler.",
             "Wire up {p} to {e}.", "New page at {p} — endpoint {e}."]

    for turn in range(1, 51):
        op = "add"
        if turn > 6:
            choices = ["add", "add", "rename_ep", "move"]
            if len([r for r in routes if "endpoint" in r]) > 8 and terminals(turn, age=5):
                choices.append("drop")
            op = rng.choice(choices) if parts else rng.choice(["rename_ep", "move"])
        if op == "add" and parts:
            seg = parts.pop()
            p, e = f"/{seg}", f"{seg.replace('-', '_')}_page"
            routes.append({"path": p, "endpoint": e})
            born[p] = turn
            prompts.append(rng.choice(add_t).format(p=p, e=e))
        elif op == "rename_ep":
            r = rng.choice(terminals(turn))
            new = r["endpoint"] + "_v2" if not r["endpoint"].endswith("_v2") else r["endpoint"][:-3] + "_v3"
            prompts.append(f"Rename the handler for {r['path']} from {r['endpoint']} to {new}.")
            r["endpoint"] = new
        elif op == "move":
            r = rng.choice(terminals(turn))
            old = r["path"]
            new = old + "-v2" if not old.endswith("-v2") else old.replace("-v2", "-next")
            routes.append({"path": new, "endpoint": r["endpoint"]})
            born[new] = turn
            r.pop("endpoint")
            r["redirect_to"] = new
            prompts.append(f"Move the {old} page to {new}, and keep {old} working as a redirect "
                           f"to the new path.")
        else:  # drop
            r = rng.choice(terminals(turn, age=5))
            routes.remove(r)
            born.pop(r["path"], None)
            prompts.append(f"Remove the {r['path']} route entirely — the page is gone, "
                           f"no redirect needed.")
    return prompts, routes


# ---------------------------------------------------------------- rendering
def _w(d, name, s):
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(s)


def render(dirname, seed_rel, seed_src, prompts, final_render, verify_src, meta):
    d = os.path.join(HERE, dirname)
    os.makedirs(d, exist_ok=True)
    _w(d, "prompts.json", json.dumps(prompts, indent=1, ensure_ascii=False))
    _w(d, "meta.json", json.dumps(meta, indent=2, ensure_ascii=False))
    _w(d, "setup.py", f'''import os

SEED = {seed_src!r}


def setup(workdir):
    pkg = os.path.join(workdir, {os.path.dirname(seed_rel)!r})
    os.makedirs(pkg, exist_ok=True)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    with open(os.path.join(workdir, {seed_rel!r}), "w", encoding="utf-8") as f:
        f.write(SEED)
''')
    _w(d, "reference_fix.py", f'''import os

# Correct final module after ALL 50 turns — rendered by _gen_long50.py from the same op replay
# that produced prompts.json and verify.py. VALIDATION ONLY.
FINAL = {final_render!r}


def apply(workdir):
    with open(os.path.join(workdir, {seed_rel!r}), "w", encoding="utf-8") as f:
        f.write(FINAL)
''')
    _w(d, "verify.py", verify_src)
    return d


def s7_final_src(flags, retired):
    def lit(d):
        rows = ",\n".join(f'    "{k}": {{"default": {v["default"]}, "owner": "{v["owner"]}"}}'
                          for k, v in sorted(d.items()))
        return "{\n" + rows + ",\n}" if rows else "{}"
    return S7_SEED.split("FLAGS = ")[0] + f"FLAGS = {lit(flags)}\n\nRETIRED = {lit(retired)}\n" + '''

def is_enabled(name: str) -> bool:
    return bool(FLAGS.get(name, {}).get("default", False))
'''


def s8_final_src(routes):
    rows = ",\n".join("    " + json.dumps(r) for r in routes)
    return S8_SEED.split("ROUTES = ")[0] + f"ROUTES = [\n{rows},\n]\n" + '''

def resolve(path: str, _depth: int = 0):
    if _depth > 5:
        return None
    for r in ROUTES:
        if r["path"] == path:
            if "redirect_to" in r:
                return resolve(r["redirect_to"], _depth + 1)
            return r["endpoint"]
    return None
'''


S7_VERIFY = '''\
"""Oracle for s7_flagtable_50: exact final registry state (rendered from the generator replay).

Subprocess import (never in-process), exact dict comparison. A single missed/mis-applied turn
anywhere in the 50 shows up as a diff here.
"""
import json
import os
import subprocess
import sys

EXPECTED_FLAGS = %(flags)s
EXPECTED_RETIRED = %(retired)s

PROBE = r\'\'\'
import json, sys
sys.path.insert(0, sys.argv[1])
from flags import registry
print(json.dumps({"FLAGS": registry.FLAGS, "RETIRED": registry.RETIRED,
                  "en": registry.is_enabled(sys.argv[2])}))
\'\'\'


def verify(workdir):
    fails = []
    probe_flag = sorted(EXPECTED_FLAGS)[0] if EXPECTED_FLAGS else "dark-mode"
    try:
        out = subprocess.run([sys.executable, "-c", PROBE, workdir, probe_flag],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return False, [f"import_or_run: {out.stderr.strip()[-300:]}"]
        got = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return False, [f"probe_crash: {type(exc).__name__}"]
    if got["FLAGS"] != EXPECTED_FLAGS:
        missing = sorted(set(EXPECTED_FLAGS) - set(got["FLAGS"]))
        extra = sorted(set(got["FLAGS"]) - set(EXPECTED_FLAGS))
        wrong = sorted(k for k in set(got["FLAGS"]) & set(EXPECTED_FLAGS)
                       if got["FLAGS"][k] != EXPECTED_FLAGS[k])
        fails.append(f"flags_mismatch missing={missing[:4]} extra={extra[:4]} wrong={wrong[:4]}")
    if got["RETIRED"] != EXPECTED_RETIRED:
        fails.append("retired_mismatch")
    if got["en"] != EXPECTED_FLAGS.get(probe_flag, {}).get("default"):
        fails.append("is_enabled_broken")
    return (not fails), fails
'''

S8_VERIFY = '''\
"""Oracle for s8_router_50: exact final table + behavioral resolve() probes incl. redirect chains."""
import json
import os
import subprocess
import sys

EXPECTED_ROUTES = %(routes)s

PROBE = r\'\'\'
import json, sys
sys.path.insert(0, sys.argv[1])
from webr import router
paths = json.loads(sys.argv[2])
print(json.dumps({"ROUTES": router.ROUTES,
                  "resolved": {p: router.resolve(p) for p in paths}}))
\'\'\'


def _expected_resolve(path, depth=0):
    if depth > 5:
        return None
    for r in EXPECTED_ROUTES:
        if r["path"] == path:
            if "redirect_to" in r:
                return _expected_resolve(r["redirect_to"], depth + 1)
            return r["endpoint"]
    return None


def verify(workdir):
    fails = []
    probes = [r["path"] for r in EXPECTED_ROUTES] + ["/definitely-missing"]
    try:
        out = subprocess.run([sys.executable, "-c", PROBE, workdir, json.dumps(probes)],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return False, [f"import_or_run: {out.stderr.strip()[-300:]}"]
        got = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return False, [f"probe_crash: {type(exc).__name__}"]
    if got["ROUTES"] != EXPECTED_ROUTES:
        gp = {r["path"] for r in got["ROUTES"]}; ep = {r["path"] for r in EXPECTED_ROUTES}
        fails.append(f"table_mismatch missing={sorted(ep-gp)[:4]} extra={sorted(gp-ep)[:4]}")
    for p in probes:
        want = _expected_resolve(p)
        if got["resolved"].get(p) != want:
            fails.append(f"resolve({p})={got['resolved'].get(p)!r} want {want!r}")
            if len(fails) > 6:
                break
    return (not fails), fails
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    p7, flags, retired = gen_s7()
    d7 = render(
        "s7_flagtable_50", "flags/registry.py", S7_SEED, p7, s7_final_src(flags, retired),
        S7_VERIFY % {"flags": repr(dict(sorted(flags.items()))),
                     "retired": repr(dict(sorted(retired.items())))},
        {"name": "s7_flagtable_50",
         "stressor": "pure context accumulation — 50 trivial dependent edits to one registry",
         "turns": 50, "max_steps_per_turn": 8, "use_code_index": False,
         "source_note": "Generated by _gen_long50.py (seeded, deterministic): 50 add/flip/rename/"
                        "chown/retire ops on a feature-flag dict. Prompts, reference and verifier "
                        "are rendered from ONE op replay so they cannot disagree.",
         "discriminator": "No single turn is hard (a 2-4 step edit). The stress is that turn k "
                          "routinely mutates a flag created 10-30 turns earlier under a name that "
                          "may have since been renamed — and that a transcript arm's per-call "
                          "input grows with every one of the 50 turns while a bounded-slice arm "
                          "re-reads one small live file. The deliverable is the per-turn "
                          "peak-input curve, not the pass bit."}
    )
    p8, routes = gen_s8()
    d8 = render(
        "s8_router_50", "webr/router.py", S8_SEED, p8, s8_final_src(routes),
        S8_VERIFY % {"routes": repr(routes)},
        {"name": "s8_router_50",
         "stressor": "pure context accumulation — 50 trivial dependent edits to a routing table",
         "turns": 50, "max_steps_per_turn": 8, "use_code_index": False,
         "source_note": "Generated by _gen_long50.py (seeded, deterministic): 50 add/rename-"
                        "endpoint/move-with-redirect/drop ops on an ordered ROUTES list. Prompts, "
                        "reference and verifier rendered from one replay.",
         "discriminator": "Same accumulation shape as s7 but with redirect chains: a 'move' turn "
                          "leaves the old path as a redirect, so later turns and the final "
                          "verifier depend on hops created many turns apart. Trivial per turn; "
                          "the transcript arm pays for all 50 turns on every call."}
    )
    print(f"generated: {d7}\ngenerated: {d8}")
    print(f"s7: {len(p7)} turns · final {len(flags)} live + {len(retired)} retired flags")
    print(f"s8: {len(p8)} turns · final {len(routes)} route entries "
          f"({sum(1 for r in routes if 'redirect_to' in r)} redirects)")

    if a.check:
        import importlib.util
        import tempfile

        def load(path, name):
            spec = importlib.util.spec_from_file_location(name, path)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m

        for d in (d7, d8):
            name = os.path.basename(d)
            setup = load(os.path.join(d, "setup.py"), name + "_s").setup
            verify = load(os.path.join(d, "verify.py"), name + "_v").verify
            fix = load(os.path.join(d, "reference_fix.py"), name + "_r").apply
            red = tempfile.mkdtemp(prefix=f"{name}-red-")
            setup(red)
            ok, fails = verify(red)
            assert not ok, f"{name}: verify PASSED on the bare seed — scenario is vacuous"
            green = tempfile.mkdtemp(prefix=f"{name}-green-")
            setup(green)
            fix(green)
            ok, fails = verify(green)
            assert ok, f"{name}: verify FAILED on the reference: {fails}"
            print(f"  {name}: red-green OK (seed fails, reference passes)")


if __name__ == "__main__":
    main()
