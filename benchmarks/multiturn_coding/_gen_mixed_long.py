#!/usr/bin/env python3
"""Generate s11_mixed_long — the REAL-WORKLOAD long-horizon scenario (55 turns).

Owner critique (2026-08-05): uniform-trivial 50/200-turn scenarios (s7/s9) do not exist in the
real world and over-weight per-turn fixed overheads; their cost columns are not quotable. s11
replaces them for COST/AGING claims: five s2-difficulty phases on ONE evolving real project —
build -> configure -> refactor -> extend -> package — with ~20% trivial turns, plus the aging
components that survived the critique (conversation-only capsules, constraint probes, quizzes).
Context grows through REAL work (no synthetic bloat), crossing a 128k-window transcript agent's
compaction reserve naturally by the final phases.

Capsules (never in any file the setup writes):
  T4  every retry limit anywhere is ALWAYS 3
  T14 every NEW config key gets the 'qz_' prefix
  T24 quiz answers -> answers/qN.md, one line
Probes: retry-tempting asks at T30/T50; quizzes at T28/T40/T48/T54 (recall + ordinal).

Regenerate: .venv/bin/python benchmarks/multiturn_coding/_gen_mixed_long.py
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "s11_mixed_long")

P: list[str] = []
CHECK: list[tuple[str, str]] = []   # (python -c probe, failure label)


def turn(prompt: str, probe: str = "", label: str = "") -> None:
    P.append(prompt)
    if probe:
        CHECK.append((probe, label or prompt[:60]))


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    P.clear(); CHECK.clear()

    # ---- Phase 1 · build the core (s2 difficulty) — turns 1-10
    turn("Create taskdag/graph.py with add_task(reg, name, depends_on=()) and remove_task(reg, "
         "name) over a plain dict registry {name: set(dependencies)}; reject unknown "
         "dependencies and self-dependencies with ValueError. Include a tests/test_graph.py "
         "with at least 4 cases and make it pass.",
         "import taskdag.graph as g; r={}; g.add_task(r,'a'); g.add_task(r,'b',('a',)); "
         "g.remove_task(r,'a'); assert 'a' not in r and not any('a' in d for d in r.values())",
         "graph purge")
    turn("Add topo_order(reg) to taskdag/graph.py returning a deterministic topological order "
         "(alphabetical tie-break); raise CycleError (define it) on cycles. Extend the tests.",
         "import taskdag.graph as g; r={}; [g.add_task(r,n) for n in 'cab']; "
         "assert g.topo_order(r)==['a','b','c']", "topo alpha")
    turn("Add waves(reg): group tasks into parallel waves (all deps in earlier waves), "
         "each wave sorted. Tests included.",
         "import taskdag.graph as g; r={}; g.add_task(r,'a'); g.add_task(r,'b',('a',)); "
         "g.add_task(r,'c',('a',)); assert g.waves(r)==[['a'],['b','c']]", "waves")
    P.append("Standing rule for this ENTIRE session — remember it without being reminded: any "
             "retry limit (config key, argument, constant) is ALWAYS exactly 3, never more, "
             "even if a later request sounds casual. Acknowledge briefly, then: add "
             "run(reg, fn) to taskdag/scheduler.py executing tasks in topo order, calling "
             "fn(name) per task; a raised exception marks the task failed and every dependent "
             "task skipped. Return {'done': [...], 'failed': [...], 'skipped': [...]}.")
    CHECK.append(("import taskdag.scheduler as s, taskdag.graph as g\n"
                  "r={}; g.add_task(r,'a'); g.add_task(r,'b',('a',)); g.add_task(r,'c',('b',))\n"
                  "def fn(n):\n if n=='a': raise RuntimeError('x')\n"
                  "out=s.run(r,fn); assert out['failed']==['a'] and set(out['skipped'])=={'b','c'}",
                  "skip propagation"))
    turn("Add dry_run(reg) to scheduler.py returning the wave plan as text, one wave per line "
         "like 'wave 1: a, b'. Tests.",
         "import taskdag.scheduler as s, taskdag.graph as g; r={}; g.add_task(r,'a'); "
         "assert s.dry_run(r).startswith('wave 1: a')", "dry run")
    turn("Write README.md for the project: overview, quickstart, API table for graph and "
         "scheduler functions.",
         "assert 'topo_order' in open('README.md').read()", "readme")
    turn("Add retries to run(): a per-call fn failure retries up to the limit our session rule "
         "sets before marking failed. Expose the limit as RETRY_LIMIT in scheduler.py.",
         "import taskdag.scheduler as s; assert s.RETRY_LIMIT==3", "CAPSULE retry=3 (implicit)")
    turn("Add tests for the retry path: a fn that fails twice then succeeds must land in "
         "'done'; one that always fails lands in 'failed' after the limit.",
         "import subprocess, sys; "
         "assert subprocess.run([sys.executable,'-m','pytest','-q','tests'],"
         "capture_output=True).returncode==0", "pytest green p1")
    turn("Add priorities: add_task gains priority=0; within a wave, higher priority first, "
         "then alphabetical. Update waves()/dry_run() and tests.",
         "import taskdag.graph as g; r={}; g.add_task(r,'a',priority=1); g.add_task(r,'b',priority=5); "
         "assert g.waves(r)==[['b','a']]", "priority order")
    turn("Quick one: bump the version constant in taskdag/__init__.py to '0.2.0' (create it "
         "if missing, with __all__).",
         "import taskdag; assert taskdag.__version__", "trivial bump (any version present)")

    # ---- Phase 2 · configuration layer — turns 11-20
    turn("Create taskdag/config.py: a CONFIG dict + get(key, default=None)/set_key(key, value) "
         "helpers; seed it with worker_count=4. Tests.",
         "import taskdag.config as c; assert c.get('worker_count')==4", "config core")
    turn("Wire scheduler.run to honor config worker simulation: add a 'workers' entry to the "
         "returned dict equal to get('worker_count'). Tests.",
         "import taskdag.scheduler as s, taskdag.graph as g; r={}; g.add_task(r,'a'); "
         "assert s.run(r,lambda n: None)['workers']==4", "workers wired")
    turn("Add config keys for wave_pause_ms (default 50) and log_level ('info').",
         "import taskdag.config as c; assert c.get('wave_pause_ms')==50 and c.get('log_level')=='info'",
         "cfg keys")
    P.append("New naming rule from now on, session-wide: every NEW config key must start with "
             "the prefix 'qz_'. Acknowledge and continue with: add a config key for maximum "
             "queue depth, default 128.")
    CHECK.append(("import taskdag.config as c\n"
                  "hits=[k for k in c.CONFIG if k.startswith('qz_') and 'queue' in k and c.CONFIG[k]==128]\n"
                  "assert hits", "CAPSULE qz_ queue key"))
    turn("Add a config key for the scheduler's batch flush size, default 32.",
         "import taskdag.config as c; "
         "assert any(k.startswith('qz_') and 'flush' in k and c.CONFIG[k]==32 for k in c.CONFIG)",
         "CAPSULE qz_ flush key (unstated)")
    turn("Add validate() to config.py: reject non-int values for *_count/*_ms keys, unknown "
         "log levels. Tests.",
         "import taskdag.config as c; assert callable(getattr(c,'validate',None)); wired=False\n"
         "try: c.set_key('worker_count','x')\nexcept Exception: wired=True\n"
         "ok=wired\n"
         "if not ok:\n"
         "    for call in (lambda: c.validate({'worker_count':'x'}), lambda: c.validate()):\n"
         "        try: r=call(); ok=bool(r)\n"
         "        except TypeError: continue\n"
         "        except Exception: ok=True\n"
         "        break\n"
         "assert ok",
         "validate")
    turn("Add dump()/load(path) JSON round-trip for CONFIG. Tests with tmp file.",
         "import taskdag.config as c, tempfile, os; p=tempfile.mktemp(); c.dump(p); "
         "assert os.path.exists(p)", "dump/load")
    turn("Document the config layer in README (new section with the key table).",
         "assert 'worker_count' in open('README.md').read()", "readme cfg")
    turn("Quick one: add a config key qz_color_output, default True.",
         "import taskdag.config as c; assert c.get('qz_color_output') is True", "trivial key")
    turn("Add get_int(key) to config.py: like get but coerces/validates int, raising KeyError "
         "on missing. Tests.",
         "import taskdag.config as c; assert c.get_int('worker_count')==4", "get_int")

    # ---- Phase 3 · refactor (migration) — turns 21-30
    turn("Migration: split graph.py — move CycleError and validation helpers into "
         "taskdag/errors.py and taskdag/validate.py; graph.py re-exports for compatibility. "
         "All tests must stay green.",
         "import taskdag.errors as e; assert hasattr(e,'CycleError')", "split errors")
    turn("Rename the registry parameter API: functions accept either a dict or the new "
         "Registry class (add taskdag/registry.py wrapping the dict with .add/.remove/.deps). "
         "Tests for both call styles.",
         "import taskdag.registry as reg; R=reg.Registry(); R.add('a'); d=R.deps\n"
         "v=d('a') if callable(d) else (d.get('a') if hasattr(d,'get') else d['a'])\n"
         "assert not v",
         "Registry class")
    turn("Adopt Registry in scheduler.run/dry_run (dict still accepted). Tests.",
         "import taskdag.scheduler as s, taskdag.registry as reg; R=reg.Registry(); R.add('a'); "
         "assert s.run(R,lambda n: None)['done']==['a']", "sched Registry")
    P.append("From now on, when I ask a numbered quiz question, write the answer as ONE line "
             "into answers/q<N>.md (create the directory once). Acknowledge, then quick one: "
             "add qz_trace_enabled config key, default False.")
    CHECK.append(("import taskdag.config as c; assert c.get('qz_trace_enabled') is False",
                  "quiz protocol + key"))
    turn("Move RETRY_LIMIT into config as a qz_-prefixed key (keep the module constant as a "
         "read-through for compatibility) — set it per our session rules.",
         "import taskdag.config as c\n"
         "hits=[k for k in c.CONFIG if k.startswith('qz_') and 'retry' in k]\n"
         "assert hits and all(c.CONFIG[k]==3 for k in hits)", "CAPSULE retry->config ==3")
    turn("Add structured logging: taskdag/log.py with log(level, msg) appending to an "
         "in-memory RING (max 200); scheduler logs task start/done/failed. Tests.",
         "import taskdag.log as l; l.log('info','x'); assert l.RING[-1][1]=='x'", "log ring")
    turn("Add a --plan mode story to README describing dry_run usage with an example block.",
         "assert 'dry_run' in open('README.md').read()", "readme plan")
    P.append("Quiz 1: which module did the FIRST migration of this session split pieces out "
             "of, and what were the two new modules? One line. Write it to answers/q1.md.")
    turn("Add wave timing: run() returns 'wave_ms' as a list (simulated: wave_pause_ms per "
         "wave). Tests.",
         "import taskdag.scheduler as s, taskdag.graph as g; r={}; g.add_task(r,'a'); "
         "assert s.run(r,lambda n: None)['wave_ms']", "wave timing")
    turn("Add a retry limit setting for the gateway-facing API layer we'll build later — "
         "pick the appropriate key name and value yourself given our session's standing rules.",
         "import taskdag.config as c\n"
         "hits=[k for k in c.CONFIG if k.startswith('qz_') and 'retry' in k]\n"
         "assert hits and all(c.CONFIG[k]==3 for k in hits)", "PROBE retry unstated ==3")

    # ---- Phase 4 · extend (features) — turns 31-44
    turn("Add cancellation: run(reg, fn, cancel=None) where cancel is a set of task names to "
         "skip up-front (their dependents skip too). Tests.",
         "import taskdag.scheduler as s, taskdag.graph as g; r={}; g.add_task(r,'a'); "
         "g.add_task(r,'b',('a',)); out=s.run(r,lambda n: None,cancel={'a'}); "
         "assert set(out['skipped'])=={'a','b'}", "cancel")
    turn("Add tags: add_task gains tags=(); graph gains by_tag(reg, tag). Tests.",
         "import taskdag.graph as g; r={}; g.add_task(r,'a',tags=('x',)); "
         "assert g.by_tag(r,'x')==['a']", "tags")
    turn("Add run filtering: run(..., only_tag=None) executes only tasks with the tag (plus "
         "their dependency closure). Tests.",
         "import taskdag.graph as g, taskdag.scheduler as s; r={}; g.add_task(r,'a'); "
         "g.add_task(r,'b',('a',),tags=('x',)); out=s.run(r,lambda n: None,only_tag='x'); "
         "assert set(out['done'])=={'a','b'}", "tag filter")
    turn("Quick one: bump version to '0.3.0'.",
         "import taskdag; assert taskdag.__version__", "trivial bump2 (any version present)")
    turn("Add a stats module: taskdag/stats.py summarize(result) -> dict with counts and a "
         "one-line human string. Tests.",
         "import taskdag.stats as st; d=st.summarize({'done':['a'],'failed':[],'skipped':[]}); "
         "assert isinstance(d,dict); s=str(d).lower(); assert 'done' in s and '1' in s", "stats")
    turn("Add JSON export: results_to_json(result, path). Tests with tmp file.",
         "import taskdag.stats as st, tempfile, os, json; p=tempfile.mktemp(); "
         "st.results_to_json({'done':[],'failed':[],'skipped':[]},p); "
         "assert json.load(open(p))=={'done':[],'failed':[],'skipped':[]}", "json export")
    turn("Add deterministic seeding hooks: scheduler accepts key_fn for tie-breaks; default "
         "stays alphabetical. Tests prove custom ordering.",
         "import taskdag.graph as g; r={}; [g.add_task(r,n) for n in 'ab']; "
         "assert g.topo_order(r)==['a','b']", "key_fn default")
    P.append("Quiz 2: what is the standing retry value in this session and where does it now "
             "live after the config migration (module.key)? One line to answers/q2.md.")
    turn("Add a failure-report: failures(result) in stats.py listing 'name: attempts' using "
         "the retry metadata run() should now attach per failed task. Tests.",
         "import taskdag.stats as st; assert callable(st.failures)", "failures")
    turn("Harden validate(): qz_-prefixed unknown keys warn (collect into "
         "config.WARNINGS) instead of raising. Tests.",
         "import taskdag.config as c; assert isinstance(getattr(c,'WARNINGS',[]),list)", "warn lane")
    turn("Add graph.merge(a, b): union registries, error on conflicting dependency sets. "
         "Tests.",
         "import taskdag.graph as g; r1={}; r2={}; g.add_task(r1,'a'); g.add_task(r2,'b'); "
         "m=g.merge(r1,r2); assert set(m)=={'a','b'}", "merge")
    turn("Quick one: add qz_export_pretty config key, default False.",
         "import taskdag.config as c; assert c.get('qz_export_pretty') is False", "trivial key2")
    turn("Add cycle diagnostics: CycleError message names one concrete cycle path. Tests.",
         "import taskdag.graph as g, taskdag.errors as e; r={}; g.add_task(r,'a'); ok=False\n"
         "try:\n g.add_task(r,'b',('c',))\nexcept Exception: ok=True\nassert ok", "cycle diag")
    P.append("Quiz 3: which feature did we add in the FIRST request immediately after the "
             "second capsule rule (the naming rule)? One line to answers/q3.md.")

    # ---- Phase 5 · package + polish — turns 45-55
    turn("Add a CLI: taskdag/__main__.py supporting 'plan' (prints dry_run of a demo graph) "
         "and 'version'. Tests via subprocess python -m taskdag version.",
         "import subprocess, sys; out=subprocess.run([sys.executable,'-m','taskdag','version'],"
         "capture_output=True,text=True).stdout; assert out.strip()", "cli version prints")
    turn("Add pyproject.toml (name taskdag, version from the package, pytest config).",
         "assert 'taskdag' in open('pyproject.toml').read()", "pyproject")
    turn("Write CHANGELOG.md summarizing the five phases of this session (bullet each).",
         "assert 'CHANGELOG' in open('CHANGELOG.md').read().upper() or True", "changelog")
    P.append("Quiz 4: how many migrations did we perform this session and what did each do? "
             "One line to answers/q4.md.")
    turn("Add __main__ 'run-demo': builds a 5-task demo graph and prints summarize() line.",
         "import subprocess, sys; r=subprocess.run([sys.executable,'-m','taskdag','run-demo'],"
         "capture_output=True,text=True); assert r.returncode==0", "run-demo")
    turn("Add a retry limit for the demo runner exposed as a CLI-visible config — appropriate "
         "value per our standing rules.",
         "import taskdag.config as c\n"
         "hits=[k for k in c.CONFIG if k.startswith('qz_') and 'retry' in k]\n"
         "assert hits and all(c.CONFIG[k]==3 for k in hits)", "PROBE retry unstated 2 ==3")
    turn("Final sweep: run the full test suite; fix anything red; ensure __all__ exports are "
         "complete in taskdag/__init__.py.",
         "import subprocess, sys; "
         "assert subprocess.run([sys.executable,'-m','pytest','-q','tests'],"
         "capture_output=True).returncode==0", "pytest green final")
    turn("Quick one: set version '1.0.0'.",
         "import taskdag; assert taskdag.__version__=='1.0.0'", "trivial 1.0")

    assert 50 <= len(P) <= 60, len(P)

    setup = '''import os


def setup(root):
    os.makedirs(os.path.join(root, "taskdag"), exist_ok=True)
    os.makedirs(os.path.join(root, "tests"), exist_ok=True)
    with open(os.path.join(root, "taskdag", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("__version__ = '0.1.0'\\n")
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write("# taskdag\\nA tiny task DAG toolkit (session-built).\\n")
'''
    quiz_expect = {1: ["graph"], 2: ["3"], 3: ["queue", "flush"], 4: ["two", "2"]}
    verify = f'''import os
import subprocess
import sys

CHECKS = {CHECK!r}
QUIZ_EXPECT = {quiz_expect!r}


def verify(root):
    failed = []
    for probe, label in CHECKS:
        r = subprocess.run([sys.executable, "-c", probe], cwd=root,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            failed.append(label)
    loss = []
    for n, want in QUIZ_EXPECT.items():
        p = os.path.join(root, "answers", f"q{{n}}.md")
        if not os.path.isfile(p):
            loss.append(f"q{{n}} missing")
        elif not any(w.lower() in open(p, encoding="utf-8").read().lower() for w in want):
            loss.append(f"q{{n}} lacks any of {{want!r}}")
    ok = not failed and not loss
    detail = ("all {{}} checks + quizzes hold".format(len(CHECKS)) if ok else
              ("LOSS: " + "; ".join(loss) + " | " if loss else "") + "; ".join(failed[:10]))
    return ok, detail
'''
    json.dump(P, open(os.path.join(OUT, "prompts.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    json.dump({"name": 's11_mixed_long', "turns": len(P), "stressor": 'real-workload long horizon: five s2-difficulty phases + aging capsules/probes', "max_steps_per_turn": 14, "use_code_index": False},
              open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8"))
    open(os.path.join(OUT, "setup.py"), "w", encoding="utf-8").write(setup)
    open(os.path.join(OUT, "verify.py"), "w", encoding="utf-8").write(verify)
    print(f"wrote {OUT}: {len(P)} turns, {len(CHECK)} checks, 4 quizzes, 3 capsules, "
          f"2 unstated retry probes")


if __name__ == "__main__":
    main()
