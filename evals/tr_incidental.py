"""T-recall Phase 2 — the INCIDENTAL-fact probe: does recall_history fire when a load-bearing fact is
BURIED in an unrelated turn's tool output (weak need→origin mapping), and does a CONTENT-NAMING
manifest cue restore it? Reuses the real episodic cache + recall_history tool. See tr_PREDICTION.md.

The fact (a provisioned instance id) is (1) agent-discovered — only in the stdout of ./provision.sh at
the DISCOVERY turn; (2) off-disk — the script is deleted right after; (3) incidental — that turn's job
is just "pass/fail", so the model has no reason to note the id; (4) needed 5 turns later, past the
4-exchange RECENT ring. So recall_history is the ONLY recovery path.

Four arms, one independent variable (the manifest cue for the discovery turn, via a harness-controlled
title_fn — TEST-ONLY, no product change):
  resident  — ceiling: the id is handed to the model in the payoff prompt (proves task+verifier work).
  invisible — floor:   the PAGED-OUT HISTORY manifest is suppressed (built with session_id="").
  coarse    — current: manifest on, discovery-turn title ≈ the prompt ("ran the provisioning script").
  content   — candidate fix: discovery-turn title NAMES the fact ("…produced a provisioned instance id"),
              value withheld — so the model must recall to READ it.

Run (deepseek):
  cd "../agent design"; set -a; . ./.env; set +a; cd -
  LLM_API_KEY=$DEEPSEEK_API_KEY LLM_BASE_URL=https://api.deepseek.com AGENT_MODEL=deepseek-chat \
    PYTHONPATH=src .venv/bin/python evals/tr_incidental.py
"""
import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

INSTANCE_ID = "inst-7q4k9"          # the buried, off-disk fact; a confabulator invents a DIFFERENT id
DISCOVERY_TURN = 2                  # 1-based turn whose tool output holds the id (incidental to its task)
PAYOFF_TURN = 7                    # 1-based turn that needs it — gap of 5 > MAX_CONVERSATION (4)

# id sits MID-output; the tail (health/complete lines) is what observe(out,100) keeps for the action
# tally, so the id is NOT retained in the slice's REPEATED/FAILING view — it truly leaves the slice.
PROVISION_SH = """#!/bin/sh
echo "== provisioning environment =="
echo "region: us-west-2   vpc: vpc-3f2a   namespace: acme-prod"
echo "allocating compute (c5.large) ..."
echo "provisioned instance id: %s"
echo "attaching volume vol-9932 (40 GiB)"
echo "security-group: sg-web-default"
echo "dns: acme-prod.internal registered"
echo "health check: OK"
echo "provisioning complete: SUCCESS"
""" % INSTANCE_ID

# Arm-specific title for the DISCOVERY turn's episode (what the PAGED-OUT HISTORY manifest shows).
TITLES = {
    "coarse":  "Run the provisioning script and confirm it succeeded",              # ≈ the prompt
    "content": "Ran provision.sh — it produced a provisioned instance id",       # names the fact, not the value
}


def _setup(wd):
    p = os.path.join(wd, "provision.sh")
    with open(p, "w") as f:
        f.write(PROVISION_SH)
    os.chmod(p, 0o755)
    os.makedirs(os.path.join(wd, "deploy"), exist_ok=True)
    with open(os.path.join(wd, "deploy", "notes.md"), "w") as f:
        f.write("# Deploy notes\n")


def _turns(resident: bool):
    # v2: the payoff references the fact by IDENTITY only ("our provisioned instance's id") — it does NOT
    # name the origin turn/step. The model must (a) realize it needs a past fact not in context, (b) locate
    # which turn produced it — where the manifest cue granularity becomes the deciding variable. (v1 said
    # "from the provisioning step earlier", spoon-feeding turn 2, which defeated the whole manipulation.)
    src = ("our provisioned instance's id"
           if not resident else
           f"our provisioned instance's id (it is {INSTANCE_ID})")
    payoff = ("Add a line to deploy/record.md that reads exactly:\n"
              "Deployed instance: <ID>\n"
              f"where <ID> is {src}. It must be the EXACT id — do not fabricate one or use a placeholder.")
    return [
        "Create deploy/record.md with a single title line: # Deploy Record",              # T1 setup
        "Run ./provision.sh and tell me ONLY whether it completed successfully (pass or fail). "
        "Do NOT repeat the individual output lines or any ids/values it prints.",           # T2 DISCOVERY
        "In deploy/notes.md, append a line that reads exactly: reviewed: yes",              # T3 filler
        "Create deploy/owners.md containing exactly: team-platform",                        # T4 filler
        "Append a second line to deploy/owners.md containing exactly: oncall-web",          # T5 filler
        "Create deploy/status.md containing exactly the word: green",                       # T6 filler
        payoff,                                                                              # T7 PAYOFF
    ]


def _run_session(turns, *, session_id, workdir, model, memory, title_override=None,
                 manifest_off=False, delete_after=None, max_steps=12):
    """Fork of memory_recall_test.run_session with two knobs: `title_override` (str) sets the
    DISCOVERY-turn episode title (the manifest cue) while every other turn keeps its natural title;
    `manifest_off` builds the slice with session_id="" so PAGED-OUT HISTORY is suppressed (the recall
    tool is still registered with the REAL session_id, so recall CAN fire if the model reaches for it)."""
    from sliceagent.pfc import Slice, slice_sink, record_user
    from sliceagent.seed import make_build_slice
    from sliceagent.text_utils import one_line
    from sliceagent.loop import run_turn
    from sliceagent.tools import LocalToolHost
    from sliceagent.code_index import make_code_index
    from sliceagent.events import AssistantText, ToolResult, make_dispatcher
    from sliceagent.hippocampus import make_episode_sink, make_search_history_tool
    from sliceagent.code_grep import make_grep_tool
    from sliceagent.llm import OpenAILLM

    state = Slice(); state.reset(turns[0])
    base = LocalToolHost(workdir)
    base.registry.register(make_grep_tool(base))
    if getattr(memory, "is_durable", False):                       # recall_history uses the REAL sid
        base.registry.register(make_search_history_tool(memory, session_id))
    retriever = make_code_index(workdir)

    cur = [1]                                                       # 1-based current turn (read by title_fn)

    def _title():
        if title_override and cur[0] == DISCOVERY_TURN:
            return title_override
        return one_line(state.goal, 80)

    episodic = make_episode_sink(memory, session_id=session_id, task_id_fn=lambda: "t1", title_fn=_title)

    turn_tools, turn_reply, recall_outs = [], [""], []
    def cap(e):
        if isinstance(e, ToolResult):
            turn_tools.append({"name": e.name, "args": {k: str(v)[:80] for k, v in (e.args or {}).items()}})
            if e.name == "recall_history":                     # capture what recall RETURNED (fidelity audit)
                recall_outs.append(str(getattr(e, "output", ""))[:1500])
        elif isinstance(e, AssistantText) and (e.content or "").strip():
            turn_reply[0] = e.content
    dispatch = make_dispatcher(slice_sink(state), episodic, cap)
    llm = OpenAILLM(model=model, timeout=90.0)
    llm.set_cache_key(session_id)
    build_sid = "" if manifest_off else session_id                 # suppress the manifest for the invisible arm
    build = make_build_slice(state, base, retriever, memory, turns[0], session_id=build_sid)

    out = []
    for i, p in enumerate(turns):
        cur[0] = i + 1
        if i > 0:
            state.goal = p
        turn_tools.clear(); turn_reply[0] = ""; recall_outs.clear()
        record_user(state, p)
        slice_seen = build()[-1]["content"]
        try:
            run_turn(build_slice=build, llm=llm, tools=base, dispatch=dispatch, max_steps=max_steps)
        except Exception as e:  # noqa: BLE001
            turn_reply[0] = f"(run error: {type(e).__name__}: {e})"
        out.append({"turn": i + 1, "prompt": p, "reply": turn_reply[0],
                    "tools": [t["name"] for t in turn_tools], "tool_detail": list(turn_tools),
                    "recall_outs": list(recall_outs), "slice_seen": slice_seen})
        if delete_after and i in delete_after:
            for rel in delete_after[i]:
                try:
                    os.remove(os.path.join(workdir, rel))
                except OSError:
                    pass
    return out


_ID_RE = re.compile(r"\binst-[a-z0-9]{4,8}\b", re.I)   # an id-shaped token → tells confab from absent


def run_arm(arm, model, make_memory):
    wd = tempfile.mkdtemp(prefix=f"tr-{arm}-")
    # ISOLATE each run in a FRESH vault — else the durable ~/.sliceagent/vault + a reused session_id lets
    # id-bearing turn-7 breadcrumbs from PRIOR runs leak into this run's manifest (a stale-archive artifact
    # that the divergence gate correctly flagged as a leak). Fresh vault → the manifest reflects THIS run only.
    os.environ["SLICEAGENT_VAULT"] = tempfile.mkdtemp(prefix=f"trvault-{arm}-")
    _setup(wd)
    resident = arm == "resident"
    title_override = TITLES.get(arm)                    # only "coarse"/"content" have one
    memory = make_memory()
    sid = f"tr-{model.replace('/', '_')}-{arm}"
    rows = _run_session(_turns(resident), session_id=sid, workdir=wd, model=model, memory=memory,
                        title_override=title_override, manifest_off=(arm == "invisible"),
                        delete_after={DISCOVERY_TURN - 1: ["provision.sh"]})   # 0-based i for the discovery turn

    recalled = any("recall_history" in t for r in rows for t in r["tools"])
    # DIVERGENCE GATE (the red-team's core doubt, now measured): is the fact GENUINELY paged out of the
    # resident slice at payoff — or did the note→findings→breadcrumb pipeline re-import it? slice_seen is
    # captured at payoff-turn START (before any recall this turn), so it IS the resident state. If the id is
    # in it, the value leaked into a resident tier and the run is VOID (no divergence → nothing to retrieve).
    payoff_slice = rows[PAYOFF_TURN - 1]["slice_seen"] or ""
    genuinely_paged_out = INSTANCE_ID not in payoff_slice
    rec_path = os.path.join(wd, "deploy", "record.md")
    written = open(rec_path).read() if os.path.exists(rec_path) else ""
    final_reply = rows[-1]["reply"] or ""
    haystack = written + "\n" + final_reply
    answer_ok = INSTANCE_ID in haystack
    other_ids = [m for m in _ID_RE.findall(haystack) if m.lower() != INSTANCE_ID.lower()]
    confabulated = (not answer_ok) and bool(other_ids)
    # audit: did the id leak into the DISCOVERY-turn findings/reply (would let it pass WITHOUT recall)?
    disc = rows[DISCOVERY_TURN - 1]
    id_leaked_at_discovery = INSTANCE_ID in (disc["reply"] or "")
    manifest_named_fact = (title_override == TITLES.get("content")) and \
        ("produced a provisioned instance id" in (rows[PAYOFF_TURN - 1]["slice_seen"] or ""))
    return {
        "arm": arm,
        "genuinely_paged_out": genuinely_paged_out,   # divergence gate: True = valid run, False = VOID (leaked)
        "recall_fired": recalled,
        "answer_ok": answer_ok,
        "confabulated": confabulated,
        "confab_ids": other_ids[:3],
        "id_leaked_at_discovery": id_leaked_at_discovery,     # contamination check — should be False for B/C
        "manifest_named_fact_at_payoff": manifest_named_fact, # Arm C sanity: the cue actually rendered
        "payoff_tools": rows[PAYOFF_TURN - 1]["tools"],
        "written_record": written[:200],
        "final_reply": final_reply[:200],
        "workdir": wd,
        "rows": rows,
    }


def main():
    from sliceagent.memory import make_memory
    model = os.environ.get("AGENT_MODEL", "deepseek-chat")
    arms = os.environ.get("TR_ARMS", "coarse").split(",")               # default: the clean addressing condition
    trials = int(os.environ.get("TR_TRIALS", "3"))
    print(f"# T-addressing probe (divergence-gated) · model={model} · id={INSTANCE_ID} · "
          f"discovery=T{DISCOVERY_TURN} payoff=T{PAYOFF_TURN} · trials={trials}\n", flush=True)
    results = []
    for arm in arms:
        for k in range(trials):
            t0 = time.time()
            try:
                r = run_arm(arm.strip(), model, make_memory)
            except Exception as e:  # noqa: BLE001
                import traceback; traceback.print_exc()
                r = {"arm": arm, "error": f"{type(e).__name__}: {e}"}
            r["wall_s"] = round(time.time() - t0, 1)
            r["trial"] = k + 1
            results.append(r)
            print(f"[{r.get('arm'):8} #{k+1}] paged_out={str(r.get('genuinely_paged_out')):5} "
                  f"recall={str(r.get('recall_fired')):5} answer_ok={str(r.get('answer_ok')):5} "
                  f"confab={str(r.get('confabulated')):5} ({r['wall_s']}s)  {r.get('written_record','')!r}", flush=True)
    out = os.path.join(os.path.dirname(__file__), f"tr_incidental_{model.replace('/', '_').replace('.', '_')}.json")
    with open(out, "w") as f:
        json.dump({"model": model, "id": INSTANCE_ID, "trials": trials, "results": results}, f, indent=1, default=str)
    print(f"\nwrote {out}", flush=True)
    # ── divergence-gated scoreboard ──────────────────────────────────────────
    ok = [r for r in results if "error" not in r]
    valid = [r for r in ok if r.get("genuinely_paged_out")]     # the fact genuinely paged out → a REAL retrieval test
    void = [r for r in ok if not r.get("genuinely_paged_out")]  # note-pipeline re-imported it → nothing to retrieve
    print("\n" + "=" * 64)
    print(f"DIVERGENCE GATE: {len(valid)}/{len(ok)} runs genuinely paged the fact out of the slice "
          f"(the seen-but-not-noted condition held)")
    if void:
        print(f"  VOID: {len(void)}/{len(ok)} runs LEAKED the fact into a resident tier "
              f"(note→findings→breadcrumb re-import — the red-team's predicted failure)")
    if valid:
        rec = sum(1 for r in valid if r.get("recall_fired"))
        ans = sum(1 for r in valid if r.get("answer_ok"))
        print(f"  of the {len(valid)} VALID runs: recall_fired {rec}/{len(valid)} · answer_ok {ans}/{len(valid)}")


if __name__ == "__main__":
    main()
