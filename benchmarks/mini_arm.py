#!/usr/bin/env python3
"""mini-swe-agent as the TRANSCRIPT control arm on the multi-turn coding scenarios.

Drives vanilla mini (DefaultAgent) over a scenario's fixed pre-written turns — the same turns
benchmarks/run.py feeds sliceagent — so the two arms differ in architecture, not in what the user
asked. mini keeps ONE growing message list across all turns and resends it on every model call;
that accumulation is the property under measurement, so nothing here trims, summarises, or
restarts the conversation.

WHY NOT THE `mini` CLI. The CLI's only multi-turn channel is the interactive `confirm_exit` prompt
("Agent wants to finish → type a new task"). Driving that over a pipe needs one line per prompt
read, and our turns are multi-line — collapsing them would break the byte-identical-turns
guarantee. Worse, we have measured what that prompt does to unattended stdin: it reads garbage and
feeds it back as a phantom task (tasks that finished at step 31 were pushed to 197). So this
driver replicates the SAME continuation semantics programmatically: run to the agent's own finish
signal, drop the terminal exit marker, append the next user turn verbatim, resume the loop.

Per-turn accounting comes from the provider's own usage records (extra.response.usage on each
assistant message) — never from character counts, which understated the mini arm's real peak by
38% when we tried them.

A turn that ends in anything but Submitted (context-window overflow, litellm error, format-error
loop) is RECORDED with its exit status and the scenario stops there: for a 50-turn accumulation
scenario, where the transcript arm hits the wall is a result, not a failure to handle.

Usage:
  set -a; source "../agent design/.env"; set +a
  python benchmarks/mini_arm.py --scenario s1_longhorizon_debug --model deepseek-v4-flash \
      --minienv <site-packages of a mini-swe-agent install> --out /tmp/mini_arm_results
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


def _usage_of(msg: dict) -> dict:
    ex = msg.get("extra") or {}
    return ((ex.get("response") or {}) if isinstance(ex, dict) else {}).get("usage") or {}


def run_mini(scenario: dict, model_name: str, workdir: str) -> dict:
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.models.litellm_model import LitellmModel
    import yaml

    # mini's own shipped templates — the same config the CLI uses. Loading the file (rather than
    # `-c key=val`) is deliberate: `-c` REPLACES the whole config, which is how an earlier run
    # dropped system_template and killed every task at startup.
    import minisweagent
    cfg_path = os.path.join(os.path.dirname(minisweagent.__file__), "config", "mini.yaml")
    house = yaml.safe_load(open(cfg_path))["agent"]

    model = LitellmModel(model_name=f"openai/{model_name}")
    env = LocalEnvironment(cwd=workdir)
    agent = DefaultAgent(
        model, env,
        system_template=house["system_template"],
        instance_template=house["instance_template"],
        step_limit=0, cost_limit=0, wall_time_limit_seconds=0,
    )

    turns_out = []
    prompts = scenario["prompts"]
    t0 = time.time()
    for ti, turn in enumerate(prompts):
        turn_t0 = time.time()
        start_idx = len(agent.messages)
        if ti == 0:
            # replicate run()'s opening exactly, but keep our hands on the loop
            agent.extra_template_vars |= {"task": turn}
            agent.messages = []
            agent.add_messages(
                agent.model.format_message(role="system",
                                           content=agent._render_template(agent.config.system_template)),
                agent.model.format_message(role="user",
                                           content=agent._render_template(agent.config.instance_template)),
            )
        else:
            # The finish command arrives as an assistant tool_call, and Submitted is raised while
            # EXECUTING it — so its tool response never gets appended, and the provider rejects a
            # history where a tool_call_id has no answering tool message the moment we add the next
            # user turn. mini's own convention for an un-executed action (seen in its trajectories)
            # is a placeholder observation; mirror it exactly rather than inventing a shape.
            tail = agent.messages[-1] if agent.messages else {}
            if tail.get("role") == "assistant":
                for c in tail.get("tool_calls") or []:
                    cid = (c or {}).get("id")
                    if cid:
                        agent.add_messages({
                            "role": "tool", "tool_call_id": cid,
                            "content": json.dumps({"returncode": 0,
                                                   "output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}),
                        })
            # the interactive UserNewTask path, minus the interactive prompt: next turn VERBATIM
            agent.add_messages({"role": "user", "content": turn})

        exit_status = ""
        from minisweagent.exceptions import FormatError, InterruptAgentFlow
        while True:
            try:
                agent.step()
                agent.n_consecutive_format_errors = 0
            except FormatError as e:
                agent.cost += e.messages[0].get("extra", {}).get("cost", 0.0)
                agent.n_consecutive_format_errors += 1
                if 0 < agent.config.max_consecutive_format_errors <= agent.n_consecutive_format_errors:
                    exit_status = "RepeatedFormatError"
                    break
                agent.add_messages(*e.messages)
            except InterruptAgentFlow as e:  # Submitted lands here
                agent.add_messages(*e.messages)
            except Exception as e:  # noqa: BLE001 — overflow/API death is DATA for this benchmark
                exit_status = type(e).__name__
                break
            if agent.messages and agent.messages[-1].get("role") == "exit":
                exit_status = (agent.messages[-1].get("extra") or {}).get("exit_status") or "exit"
                # The exit marker is bookkeeping, not conversation: the interactive continuation
                # never appends one (it intercepts Submitted before that), and a role="exit"
                # message inside the history would be rejected by the provider on the next call.
                agent.messages.pop()
                break

        seg = agent.messages[start_idx:]
        calls = peak = pin = cin = out = 0
        for m in seg:
            u = _usage_of(m)
            if not u:
                continue
            p = int(u.get("prompt_tokens", 0) or 0)
            calls += 1
            pin += p
            peak = max(peak, p)
            cin += int(u.get("prompt_cache_hit_tokens", 0) or 0)
            out += int(u.get("completion_tokens", 0) or 0)
        turns_out.append({
            "turn": ti + 1, "exit": exit_status, "calls": calls, "peak_in": peak,
            "in_total": pin, "in_cached": cin, "out_total": out,
            "wall_s": round(time.time() - turn_t0, 1),
            "transcript_msgs": len(agent.messages),
        })
        print(f"  turn {ti+1:>2}/{len(prompts)}: {exit_status:<22} calls={calls:<3} "
              f"peak={peak/1000:>6.1f}k msgs={len(agent.messages)}", flush=True)
        if exit_status != "Submitted":
            print(f"  STOPPING scenario at turn {ti+1}: {exit_status} "
                  f"(for an accumulation scenario this is a finding, not a bug)", flush=True)
            break

    ok, failed = scenario["verify"](workdir)
    return {
        "scenario": scenario["name"], "agent": "mini", "model": model_name,
        "passed": bool(ok), "failed_checks": list(failed or []),
        "turns_completed": sum(1 for t in turns_out if t["exit"] == "Submitted"),
        "turns_total": len(prompts),
        "wall_s": round(time.time() - t0, 1),
        "turns": turns_out,
        "messages": agent.messages,   # the full transcript, for audit + re-scoring
    }


def _meterize(res, model):
    sys.path.insert(0, HERE)
    from meter import enrich, summarize
    res["turns"] = [{**t, **enrich(t, model)} if "in_total" in t else t for t in res.get("turns", [])]
    res.update(summarize([t for t in res.get("turns", []) if "in_total" in t], model))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL", "deepseek-v4-flash"))
    ap.add_argument("--minienv", required=True,
                    help="site-packages dir of a mini-swe-agent install (kept out of this repo's venv)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    sys.path.insert(0, a.minienv)
    os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")  # no litellm price row for deepseek
    if os.environ.get("DEEPSEEK_API_KEY"):
        os.environ["OPENAI_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]
        os.environ["OPENAI_API_BASE"] = os.environ["OPENAI_BASE_URL"] = "https://api.deepseek.com/v1"

    scn = load_scenario(a.scenario)
    workdir = tempfile.mkdtemp(prefix=f"mini-arm-{a.scenario}-")
    scn["setup"](workdir)
    print(f"[{a.scenario}] {scn['meta']['turns']} turns · model={a.model} · workdir={workdir}")
    res = _meterize(run_mini(scn, a.model, workdir), a.model)

    os.makedirs(a.out, exist_ok=True)
    out_path = os.path.join(a.out, f"{a.scenario}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    print(f"pass={res['passed']} turns={res['turns_completed']}/{res['turns_total']} "
          f"-> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
