#!/usr/bin/env python3
"""Generate s9_policy_200 — the 200-turn super-long AGING scenario (goal ladder B3).

Design (docs/SESSION-TAPE-DESIGN.md program + the aging-benchmark spec):
- Substrate: an evolving flag/policy registry (the proven s7 shape) across FOUR modules with
  periodic migrations — every turn one additive, single-turn-doable increment. NO bug-report
  turns that assume a particular buggy implementation (the s2 spin trap): additions only.
- TIME CAPSULES: standing rules stated ONCE, conversation-only, never written into any file by
  setup (contamination rule: memory must be the only path):
    T5  retry_limit is ALWAYS 3, session-wide, even if later asks look casual
    T20 every NEW flag name gets the 'qz_' prefix from now on
    T40 quiz answers go to answers/qN.md, one line each
- PROBES every ~25 turns, graded mechanically by the final verifier:
    P-constraint: an ask that TEMPTS violating a capsule without restating it
    P-recall (quiz): retro question whose ground truth the generator knows
    P-ordinal (quiz): "which flag did we add immediately after migration N?"
- Every non-probe increment is also verified (flag present with right default in the final
  registry), so substrate quality is measured alongside memory.

Regenerate: .venv/bin/python benchmarks/multiturn_coding/_gen_super200.py
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "s9_policy_200")

MODULES = ["core", "worker", "audit", "gateway"]
MIGRATION_TURNS = {50: ("core", "policy_core"), 110: ("worker", "policy_worker")}
CAPSULE_TURNS = {
    # capsule/probe flags are MODEL-NAMED (the ask never spells the name) -> the verifier
    # matches by (stem substring, default) instead of an exact name, so a reasonable
    # model-chosen name is never a false failure. Stem "" = name given verbatim in the ask.
    5: ("Standing rule for this ENTIRE session, remember it: whenever any flag or setting is a "
        "retry limit, its value must be exactly 3 — never more — even if a later request sounds "
        "casual about it. Acknowledge briefly and continue with: add flag qz_boot_delay_ms with "
        "default 250 to the core registry.", ("qz_boot_delay_ms", "", "250", "core")),
    20: ("New naming rule from now on, session-wide: every NEW flag name must start with the "
         "prefix 'qz_'. Acknowledge and continue with: add a flag for the maximum queue depth "
         "(default 128) to the worker registry.", ("", "queue", "128", "worker")),
    40: ("From now on, when I ask a numbered quiz question, write the answer as ONE line into "
         "answers/q<N>.md (create the directory once). Acknowledge and continue with: add a flag "
         "for the audit sample rate percent (default 5) to the audit registry.",
         ("", "sampl", "5", "audit")),
}
# (turn, quiz#, prompt, expected substring in answers/q<N>.md)
QUIZZES = [
    (75, 1, "Quiz 1: what is the standing value for any retry limit in this session, as we "
            "agreed early on? ", "3"),
    (100, 2, "Quiz 2: which naming prefix did we adopt for all new flags, and at roughly which "
             "point in the session did we adopt it — before or after the first module "
             "migration? Answer as one line containing the prefix. ", "qz_"),
    (125, 3, "Quiz 3: which flag did we add in the very first request IMMEDIATELY AFTER the "
             "first module migration (the core rename)? One line, just the flag name. ",
             None),   # filled in by the generator once turn 51's flag is known
    (175, 4, "Quiz 4: name the module whose registry received the audit sample rate flag, and "
             "the flag's default value, in one line. ", "5"),
]
# constraint probes: retry-flavored asks that do NOT restate the rule
RETRY_PROBES = {
    90: "worker", 140: "gateway", 190: "audit",
}


def flag_name(i: int, module: str) -> str:
    return f"qz_{module}_opt_{i:03d}"


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    prompts: list[str] = []
    expected: list[tuple[str, str, str]] = []   # exact-name rows: (flag, default, MODULE)
    fuzzy: list[tuple[str, str, str]] = []      # model-named rows: (stem, default, MODULE)
    # rows record the MODULE; the final file name is resolved AFTER all migrations, else every
    # pre-migration row would point at a file the migration renamed away (generator self-test bug)
    module_file = {m: m for m in MODULES}
    post_migration_flag = {}

    for turn in range(1, 201):
        if turn in CAPSULE_TURNS:
            text, (flag, stem, default, module) = CAPSULE_TURNS[turn]
            prompts.append(text)
            if flag:
                expected.append((flag, default, module))
            else:
                fuzzy.append((stem, default, module))
            continue
        if turn in MIGRATION_TURNS:
            module, new_name = MIGRATION_TURNS[turn]
            prompts.append(
                f"Migration: rename the {module} registry module file to {new_name}.py (keep "
                f"every existing flag and default exactly; update any imports). This is the "
                f"{'first' if turn == 50 else 'second'} migration of the session.")
            module_file[module] = new_name
            continue
        if turn in RETRY_PROBES:
            module = RETRY_PROBES[turn]
            prompts.append(
                f"Add a retry limit flag for the {module} registry — pick the appropriate "
                f"value yourself given our session's standing rules.")
            fuzzy.append(("retry", "3", module))
            continue
        quiz = next((q for q in QUIZZES if q[0] == turn), None)
        if quiz:
            _, n, text, _ = quiz
            prompts.append(text + f"Write it to answers/q{n}.md.")
            continue
        module = MODULES[turn % len(MODULES)]
        flag = flag_name(turn, module)
        default = str(100 + turn)
        prompts.append(
            f"Add flag {flag} with integer default {default} to the {module} registry "
            f"({module_file[module]}.py), keeping the registry sorted by flag name.")
        expected.append((flag, default, module))
        if turn == 51:
            post_migration_flag[3] = flag

    quiz3_answer = post_migration_flag.get(3, "")
    expected = [(f, d, module_file[m]) for f, d, m in expected]
    fuzzy = [(st, d, module_file[m]) for st, d, m in fuzzy]
    setup = f'''import os

MODULES = {MODULES!r}


def setup(root):
    for m in MODULES:
        with open(os.path.join(root, m + ".py"), "w", encoding="utf-8") as f:
            f.write("REGISTRY = {{\\n}}\\n")
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write("# policy flags\\nFour flag registries, one per module. REGISTRY maps "
                "flag name -> default.\\n")
'''
    verify = f'''import ast
import os

EXPECTED = {expected!r}
FUZZY = {fuzzy!r}
QUIZ_EXPECT = {{1: "3", 2: "qz_", 3: {quiz3_answer!r}, 4: "5"}}
RETRY_FLAGS = {[f"qz_{m}_retry_limit" for m in RETRY_PROBES.values()]!r}


def _registry(root, module_file):
    path = os.path.join(root, module_file + ".py")
    if not os.path.isfile(path):
        return None
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if getattr(t, "id", "") == "REGISTRY":
                        return ast.literal_eval(node.value)
    except Exception:
        return None
    return None


def verify(root):
    failed = []
    regs = {{}}
    for flag, default, module_file in EXPECTED:
        reg = regs.setdefault(module_file, _registry(root, module_file))
        if reg is None:
            failed.append(f"registry missing: {{module_file}}.py")
            continue
        if str(reg.get(flag)) != default:
            failed.append(f"{{module_file}}:{{flag}} = {{reg.get(flag)!r}} != {{default}}")
    for stem, default, module_file in FUZZY:
        reg = regs.setdefault(module_file, _registry(root, module_file))
        if reg is None:
            failed.append(f"registry missing: {{module_file}}.py")
            continue
        hits = [k for k, v in reg.items()
                if k.startswith("qz_") and stem in k and str(v) == default]
        if not hits:
            failed.append(f"{{module_file}}: no qz_*{{stem}}* flag with default {{default}} "
                          f"(capsule/probe violated)")
    for n, want in QUIZ_EXPECT.items():
        p = os.path.join(root, "answers", f"q{{n}}.md")
        if not os.path.isfile(p):
            failed.append(f"answers/q{{n}}.md missing")
        elif want and want not in open(p, encoding="utf-8").read():
            failed.append(f"answers/q{{n}}.md lacks {{want!r}}")
    ok = not failed
    return ok, ("all {{}} expected flags + capsules + quizzes hold".format(len(EXPECTED))
                if ok else "; ".join(failed[:12]))
'''
    json.dump(prompts, open(os.path.join(OUT, "prompts.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    json.dump({"max_steps_per_turn": 12, "use_code_index": False},
              open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8"))
    open(os.path.join(OUT, "setup.py"), "w", encoding="utf-8").write(setup)
    open(os.path.join(OUT, "verify.py"), "w", encoding="utf-8").write(verify)
    print(f"wrote {OUT}: {len(prompts)} prompts, {len(expected)} verified flags, "
          f"4 quizzes, 3 retry probes, 2 migrations, 3 capsules")


if __name__ == "__main__":
    main()
