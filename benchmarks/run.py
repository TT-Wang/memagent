#!/usr/bin/env python3
"""Reproduce the sliceagent multi-turn coding benchmark (README §3).

Drives sliceagent over a scenario's fixed, pre-written turns (a scripted "user" — deterministic, so both
a slice agent and any transcript agent get byte-identical turns), then scores the final repo with the
scenario's own verifier and reports per-turn + total metrics: pass, per-call peak input, tokens
(input/cached/output), wall, steps.

Usage (needs `pip install "sliceagent[tui]"` and an LLM configured — LLM_API_KEY + AGENT_MODEL, or
`sliceagent init`):

    python benchmarks/run.py                         # all three scenarios
    python benchmarks/run.py --scenario s1_longhorizon_debug
    AGENT_REASONING=high python benchmarks/run.py    # match the published run
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.join(HERE, "multiturn_coding")
if HERE not in sys.path:
    # `from meter import …` below must resolve regardless of the caller's cwd — the harness is
    # loaded via importlib from probes/tests that never chdir into benchmarks/.
    sys.path.insert(0, HERE)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_scenario(name):
    d = os.path.join(TASKS, name)
    return {
        "name": name,
        "meta": json.load(open(os.path.join(d, "meta.json"))),
        "prompts": json.load(open(os.path.join(d, "prompts.json"))),
        "setup": _load(os.path.join(d, "setup.py"), f"{name}_setup").setup,
        "verify": _load(os.path.join(d, "verify.py"), f"{name}_verify").verify,
    }


def _configured_llm():
    """Use the same env-over-config provider resolution promised by the benchmark README."""
    from sliceagent.config import load_config, load_prefs
    from sliceagent.llm import OpenAILLM

    cfg = load_config()
    prefs = load_prefs()
    providers = cfg.providers() or {}
    pinned = prefs.get("provider")
    table = providers.get(pinned) if pinned in providers else None
    if table and table.get("api_key"):
        configured_key, configured_base = table["api_key"], table.get("base_url") or ""
        preferred_model = prefs.get("model") or table.get("model") or cfg.model
    else:
        configured_key, configured_base = cfg.api_key, cfg.base_url
        preferred_model = (None if pinned and pinned not in providers else prefs.get("model")) or cfg.model
    model = os.environ.get("AGENT_MODEL") or preferred_model
    api_key = os.environ.get("LLM_API_KEY") or configured_key
    base_url = os.environ.get("LLM_BASE_URL") or configured_base
    if not model or not api_key:
        raise ValueError("No configured model/key. Run `sliceagent init` or export AGENT_MODEL + LLM_API_KEY.")
    return OpenAILLM(model=model, api_key=api_key, base_url=base_url or None, timeout=60.0)


class _Tap:
    """Wrap the LLM to capture every call's token usage + latency."""
    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def __getattr__(self, name):
        # Instrumentation must be transparent: model identity, context-window hints, retry classification,
        # provider endpoint, and cache hooks are part of the model-runner contract.
        return getattr(self.inner, name)

    def _record(self, r, t0):
        u = (r.usage or {}) if hasattr(r, "usage") else {}
        cached = u.get("input_cache_read", 0) or u.get("cached_tokens", 0) or 0
        self.calls.append({"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0),
                           "cached": cached, "wall": time.time() - t0})
        return r

    def complete(self, messages, tools):
        return self._record(self.inner.complete(messages, tools), time.time())

    def complete_with_control(self, messages, schemas, **kw):
        # model_runner FEATURE-DETECTS this richer seam and prefers it whenever the adapter has it
        # (production OpenAILLM does). __getattr__ forwarded the probe to the inner adapter, so the
        # entire run flowed through the UNwrapped method and the meter read zero for every field
        # while the scenario passed — a fully plausible-looking dead meter. Both entry points must
        # be wrapped; transparent forwarding is exactly what makes the miss invisible.
        t0 = time.time()
        inner_fn = getattr(self.inner, "complete_with_control", None)
        if inner_fn is None:
            # The tap must MIRROR the inner adapter's capability, not add one: a class-level method
            # makes the feature probe true even when the wrapped LLM (a two-argument fake) lacks the
            # seam. Fall back exactly as model_runner would on the unwrapped adapter.
            return self._record(self.inner.complete(messages, schemas), t0)
        return self._record(inner_fn(messages, schemas, **kw), t0)


def run(scn, memory_mode="real"):
    from sliceagent.code_index import make_code_index
    from sliceagent.events import make_dispatcher
    from sliceagent.loop import run_turn
    from sliceagent.memory import NullMemory
    from sliceagent.pfc import Slice, record_user, slice_sink
    from sliceagent.retriever import NullRetriever
    from sliceagent.seed import make_build_slice
    from sliceagent.tools import LocalToolHost

    workdir = tempfile.mkdtemp(prefix=f"bench-{scn['name']}-")
    scn["setup"](workdir)
    meta, prompts = scn["meta"], scn["prompts"]
    max_steps = int(meta.get("max_steps_per_turn", 20))

    state = Slice(); state.reset(prompts[0])
    tools = LocalToolHost(root=workdir)
    retriever = make_code_index(workdir) if meta.get("use_code_index") else NullRetriever()
    tap = _Tap(_configured_llm())
    if hasattr(tap.inner, "set_cache_key"):
        tap.inner.set_cache_key(os.path.basename(workdir))
    # PRODUCTION PARITY IS THE DEFAULT. The original harness wired NullMemory, whose
    # episode_manifest/search_episodes return empty — the recall channel (hippocampus paging,
    # search_history, history/ locators) was STRUCTURALLY absent from every published multi-turn
    # number, and a scenario built to exercise recall (s6) silently tested an agent without it.
    # "null" remains available as the labeled carried-slice-only ABLATION, never the default.
    session_id = f"bench-{scn['name']}-{os.getpid()}"
    sinks = []
    telem = None
    if memory_mode == "real":
        vault = tempfile.mkdtemp(prefix=f"bench-vault-{scn['name']}-")
        os.environ["SLICEAGENT_VAULT"] = vault          # isolate: never the user's ~/.sliceagent
        from sliceagent.hippocampus import EpisodeSink, HistoryFS, make_search_history_tool
        from sliceagent.memory import LocalMemory
        from sliceagent.telemetry import make_telemetry_sink
        memory = LocalMemory(prefer_memem=False)
        episodic = EpisodeSink(memory, session_id=session_id, task_id_fn=lambda: "t-bench",
                               title_fn=lambda: scn["name"], outcome_fn=lambda: {})
        telem = make_telemetry_sink()
        sinks = [episodic, telem]
        tools._history = HistoryFS(memory, session_id)
        tools.registry.register(make_search_history_tool(memory, session_id))
    else:
        memory = NullMemory()
    # SESSION TAPE recorder (AGENT_SESSION_TAPE=1): collects the turn's successful file-tool rows
    # so the seal can append host-authored bases/patches (SESSION-TAPE-DESIGN §2). Inert otherwise.
    from sliceagent.tape import TapeRecorder, tape_seal_update
    tape_on = os.environ.get("AGENT_SESSION_TAPE", "").strip() == "1"
    recorder = TapeRecorder(tools)
    if tape_on:
        sinks.append(recorder.sink)
    # State reduction is authoritative, not a best-effort observer. A reducer failure must fail the eval.
    dispatch = make_dispatcher(*sinks, required=(slice_sink(state),))

    per_turn = []; t0 = time.time(); err = ""
    tape_drift = 0; tape_rebased = 0; tape_compactions = 0
    try:
        for i, p in enumerate(prompts):
            record_user(state, p)
            n0 = len(tap.calls)
            result = run_turn(build_slice=make_build_slice(state, tools, retriever, memory, p,
                                                           session_id),
                              llm=tap, tools=tools, dispatch=dispatch, max_steps=max_steps)
            ct = tap.calls[n0:]
            from meter import enrich as _enrich
            per_turn.append({"turn": i + 1, "stop": result.stop_reason,
                             "wall": round(sum(c["wall"] for c in ct), 1),
                             **_enrich({"calls": len(ct),
                                        "peak_in": max((c["in"] for c in ct), default=0),
                                        "in_total": sum(c["in"] for c in ct),
                                        "in_cached": sum(c["cached"] for c in ct),
                                        "out_total": sum(c["out"] for c in ct)},
                                       os.environ.get("AGENT_MODEL", "deepseek-v4-flash"))})
            # Match the real host lifecycle: semantic state carries; detailed calls/trajectory counters do not.
            state.seal()
            if tape_on:
                # SESSION TAPE seal update (digest + bases + patches + honesty net), ONE renderer
                # family for every producer; the recorder resets per turn.
                _reply = ""
                if getattr(state, "conversation", None):
                    _reply = str(state.conversation[-1].get("assistant") or "")
                info = tape_seal_update(
                    state, tools, recorder.rows, session_id=session_id,
                    artifact_id=f"turn-{i + 1:03d}", task_id=scn["name"],
                    status="completed" if result.stop_reason == "end_turn" else str(result.stop_reason),
                    user_request=p, assistant_reply=_reply,
                )
                recorder.reset()
                tape_drift += info["drift"]; tape_rebased += len(info["rebased"])
                tape_compactions += info.get("epoch_folds", 0)   # EVENTS only; gc entry counts are not events
            else:
                # SESSION SPINE parity with the host: the CLI appends each committed turn's digest
                # (rendered once, at seal) to the session cache. The bench has no artifact store, so
                # it feeds the SAME renderer (R3: one renderer serves every producer) with bench-local
                # turn ids; the region renders it only under AGENT_SESSION_SPINE=1.
                from sliceagent.spine import render_turn_digest as _digest
                state.continuity.session_spine.append(_digest(
                    artifact_id=f"turn-{i + 1:03d}", session_id=session_id, task_id=scn["name"],
                    status="completed" if result.stop_reason == "end_turn" else str(result.stop_reason),
                    user_request=p,
                ))
            if result.stop_reason != "end_turn":
                err = f"turn {i + 1} stopped abnormally: {result.stop_reason}"
                break
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"

    passed, detail = (False, err) if err else scn["verify"](workdir)
    calls = tap.calls
    from meter import enrich as _enrich
    totals = _enrich({"calls": len(calls),
                      "peak_in": max((c["in"] for c in calls), default=0),
                      "in_total": sum(c["in"] for c in calls),
                      "in_cached": sum(c["cached"] for c in calls),
                      "out_total": sum(c["out"] for c in calls)},
                     os.environ.get("AGENT_MODEL", "deepseek-v4-flash"))
    # LIVENESS GATE: a run that claims the real memory mode must PROVE the archive was written and
    # report the recall counters. "No episodes" under memory_mode=real is a harness failure, not a
    # result — the exact defect this gate exists to make impossible to miss again.
    liveness = {"memory_mode": memory_mode, "episodes_written": None,
                "recalls": None, "re_reads": None}
    if tape_on:
        liveness.update(tape_entries=len(state.continuity.session_tape),
                        tape_drift=tape_drift, tape_rebased=tape_rebased,
                        tape_compactions=tape_compactions,
                        tape_chars=sum(len(e) for e in state.continuity.session_tape))
    if memory_mode == "real":
        eps = ()
        try:
            eps, _total = memory.episode_manifest(session_id, 200)
            liveness["episodes_written"] = len(eps)
        except Exception as exc:  # noqa: BLE001
            liveness["episodes_written"] = f"manifest_error:{type(exc).__name__}"
        if telem is not None:
            liveness.update(telem.summary())
        if not eps:
            # Liveness invalidates a PASS; it must never OVERWRITE the detail of a run that already
            # failed for its own reason (the masked-first-cause trap: the reducer error vanished
            # behind this line and the eval read as a harness problem instead of a product one).
            note = "HARNESS INVALID: memory_mode=real but zero episodes archived"
            detail = note if passed else f"{detail}; {note}"
            passed = False
    return {
        "scenario": scn["name"], "passed": bool(passed), "detail": str(detail)[:100],
        "steps": len(calls), "wall_s": round(time.time() - t0, 1),
        "per_turn": per_turn, **liveness, **totals,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the sliceagent multi-turn coding benchmark.")
    ap.add_argument("--scenario", default=None, help="one scenario name, or omit for all three")
    ap.add_argument("--json-out", default="", help="directory to also write one <scenario>.json per run")
    ap.add_argument("--memory", choices=("real", "null"), default="real",
                    help="real = production wiring (archive + recall + telemetry, isolated vault); "
                         "null = the carried-slice-only ablation, labeled as such")
    args = ap.parse_args(argv)
    names = [args.scenario] if args.scenario else sorted(
        n for n in os.listdir(TASKS) if os.path.isdir(os.path.join(TASKS, n)))
    failed = False
    for name in names:
        try:
            r = run(load_scenario(name), memory_mode=args.memory)
        except Exception as e:  # noqa: BLE001
            print(f"{name}: setup/run error — {type(e).__name__}: {e}")
            failed = True
            continue
        if args.json_out:
            os.makedirs(args.json_out, exist_ok=True)
            with open(os.path.join(args.json_out, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False)
        failed = failed or not r["passed"]
        print(f"\n{r['scenario']}: {'PASS' if r['passed'] else 'FAIL'}  "
              f"steps={r['steps']} peak_in={r['peak_in']:,} "
              f"tokens={r['in_total'] + r['out_total']:,} (cached {r['in_cached']:,}) wall={r['wall_s']}s"
              f"{'' if r['passed'] else '  · ' + r['detail']}")
        for t in r["per_turn"]:
            print(f"    turn {t['turn']}: peak_in={t['peak_in']:,} in={t['in_total']:,} "
                  f"fresh={t['in_fresh']:,} out={t['out_total']:,} wall={t['wall']}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
