"""Named-agent registry — file-defined subagent KINDS.

sliceagent's subagents were two HARDCODED kinds (read-only explorer + writable). The kinds are now a
pluggable REGISTRY: each agent is a {name, description, tools-allowlist, reasoning, system-prompt}
definition, discovered from `<root>/agents/*.md` (markdown + frontmatter — sliceagent's own SKILL.md idiom),
and the model spawns one BY NAME via the generic `spawn_agent` tool. Built-ins (explorer, general) ship
in-tree; user files add or override by name.

Periphery, NOT the moat: a spawned agent still runs the bounded slice loop and returns only a summary.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass

# An EXPLORER's read-only surface — the single source of truth (subagent.py imports this).
# `grep` (find by CONTENT) + `glob` (find by NAME) are the two discovery tools; both read-only.
READ_ONLY_TOOLS = ("read_file", "list_files", "grep", "glob", "skill", "search_history", "code_review")
_READ_ONLY_SET = frozenset(READ_ONLY_TOOLS)   # mutability is decided against this KNOWN-safe set (pessimistic)

# Tools NO subagent may use, regardless of its allowlist. A
# subagent must not stop to ask the END-USER — ambiguity is the parent's job; a child that blocks on input
# is a stall (and racy/meaningless when several run in parallel). It returns its summary instead.
SUBAGENT_EXCLUDED_TOOLS = frozenset({"ask_user", "change_workspace", "update_work"})


@dataclass(frozen=True)
class AgentSpec:
    """One subagent KIND. `tools=None` → inherit the parent's FULL tool surface (a 'general' agent)."""
    name: str
    description: str = ""
    tools: tuple[str, ...] | None = None   # allowlist of tool names the child may use (None = all)
    reasoning: str | None = None           # "fast" | "full" | None (inherit the parent's)
    system_prompt: str = ""                # extra system-prompt layer prepended for the child
    summary_is_deliverable: bool = False   # the child's SUMMARY is the product (a trailing failing check is
    #                                        intentional, not a crash) — like a verifier that ends on a FAIL.

    @property
    def read_only(self) -> bool:
        """A child is read-only iff EVERY tool in its allowlist is a KNOWN read-only tool. Pessimistic by
        design: an unknown / plugin / MCP tool is NOT assumed safe (the old check only excluded the static
        WRITE_TOOLS set, so a side-effecting plugin tool was mis-classified read-only and could be
        scheduled as a parallel non-writer). None (full surface) is writable."""
        return self.tools is not None and set(self.tools).issubset(_READ_ONLY_SET)


BUILTIN_AGENTS: dict[str, AgentSpec] = {
    "explorer": AgentSpec(
        name="explorer",
        description="Read-only investigation — find files, trace usages, understand code; returns a summary. "
                    "Fan out several in one turn for breadth.",
        tools=READ_ONLY_TOOLS, reasoning="full",
        system_prompt=(
            "You are a read-only EXPLORER subagent: investigate the task by reading/grepping and return a "
            "concise summary of what you found (files, locations, conclusions). You cannot modify anything — "
            "do not attempt edits or commands.\n"
            "Evidence discipline: separate exact observation from inference. A categorical workspace claim must "
            "be entailed by the tool output you actually saw; quote the load-bearing line and preserve uncertainty. "
            "Names such as 'stored', 'safe', or 'validated' do not establish how a value was produced, stored, or "
            "checked. Missing definitions, callers, or execution paths make downstream behavior conditional, not "
            "proven. Code that constructs a command/query does not prove it is executed or that a claimed impact "
            "occurs. Do not promote a possible side channel into a measured exploit, or a locally swallowed "
            "interrupt into a global 'unkillable' claim. State the precise condition under which a risk would "
            "materialize. Choose the most certain concrete failure first: an observed unresolved dependency, "
            "ignored input, masked exception, or wrong return outranks a more dramatic security story whose "
            "caller/sink/threat path was not observed. If the evidence cannot establish the stronger claim, "
            "report the narrower observed bug. In the final report label the load-bearing line as Observed, the "
            "interpretation as Inference, and every unobserved prerequisite as Conditional."
        ),
    ),
    "general": AgentSpec(
        name="general",
        description="A full sub-agent for ONE self-contained sub-task (can read AND edit/run); returns a summary.",
        tools=None, reasoning=None,
        system_prompt="You are a SUBAGENT handling one self-contained sub-task in the shared workspace. Do the "
                      "work, then return a concise summary of what you changed and verified. Do NOT ask the "
                      "user; if the task is ambiguous, make the best reasonable choice and note it in the summary.",
    ),
    # Root-cause debugging for a FAILED verify (P2 escalation target: the oscillation steer names this
    # kind when the same failure signature recurs). Ported from forge agents/debugger.md (same author):
    # the discipline is hypothesis-before-fix — guessing wastes attempts, and a stagnant retry must
    # report BLOCKED rather than burn another identical attempt.
    "debugger": AgentSpec(
        name="debugger",
        description="Diagnoses and FIXES a failing check via root-cause analysis (reproduce → hypothesize "
                    "→ verify hypothesis → fix root cause → re-run the check). Spawn it with the exact "
                    "failing command and its output when a verify keeps failing the same way.",
        tools=None, reasoning="full",
        system_prompt=(
            "You are a DEBUGGER subagent: one failing check, root-cause analysis, then the fix.\n"
            "MANDATORY PROCESS (do not skip steps):\n"
            "1. REPRODUCE: run the failing command yourself; confirm the failure is still present.\n"
            "2. ROOT-CAUSE: read the failing code and test thoroughly; trace entry point -> failure; form a "
            "SPECIFIC hypothesis ('X calls Y which expects Z but receives W').\n"
            "3. VERIFY THE HYPOTHESIS before changing anything (targeted read/print/log). Guessing wastes "
            "attempts.\n"
            "4. FIX THE ROOT CAUSE, not the symptom. If the TEST is wrong rather than the code, fix the test "
            "and say why.\n"
            "5. RE-RUN the original failing command plus any sibling checks; confirm no new failures.\n"
            "NEVER: retry with cosmetic changes; suppress errors with try/except; disable or skip failing "
            "tests; change code unrelated to the failure.\n"
            "STAGNATION: if your brief shows this failure already recurred across attempts and your only idea "
            "repeats a prior attempt, report BLOCKED with what is needed instead of burning the attempt.\n"
            "Report: root cause, the fix, files changed, and the verify command output proving green."
        ),
    ),
    # A CALIBRATED code reviewer. The failure mode of an LLM review is not missing bugs — it is CRYING WOLF:
    # inflating severity, flagging by-design tradeoffs, treating a single-user local tool as a multi-tenant
    # service, and asserting failure chains it never traced. This kind's prompt is the counterweight
    # (measured need: a self-review that raised 5 criticals of which 0 were real). Read-only → fans out.
    "reviewer": AgentSpec(
        name="reviewer",
        description="CALIBRATED code review — audits for real, exploitable/impactful defects with DISCIPLINED "
                    "severity (most findings are MEDIUM/LOW). Fan out one per area for a broad review.",
        tools=READ_ONLY_TOOLS, reasoning="full",
        summary_is_deliverable=True,
        system_prompt=(
            "You are a CALIBRATED, skeptical code reviewer. Your worth is PRECISION, not a long list: a review "
            "that inflates severity or reports non-bugs is worse than useless — it makes people fix "
            "non-problems and distrust the next review. Report FEWER, VERIFIED findings.\n"
            "\nSEVERITY RUBRIC (reserve the top tiers — when unsure, go one tier LOWER):\n"
            "- CRITICAL: exploitable by an UNTRUSTED input (not the operator's own config/files) with real "
            "impact, OR silent data loss/corruption in NORMAL use. Almost nothing is CRITICAL.\n"
            "- HIGH: a real bug that fires in normal use and clearly hurts (wrong result, crash, security gap "
            "under realistic inputs).\n"
            "- MEDIUM: a real defect with limited impact or that needs an edge case. Most true findings.\n"
            "- LOW: robustness/style/portability nit, or a real-but-unreachable issue.\n"
            "\nBEFORE you escalate ANY finding, do these four checks — most false positives die here:\n"
            "1. READ THE ADJACENT COMMENT/DOCSTRING. If the code documents the behavior as intentional (a "
            "tradeoff, a deliberate broad catch, an accepted residual risk), it is a DESIGN CHOICE, not a bug "
            "— note it as such, do not flag it HIGH/CRITICAL.\n"
            "2. TRACE THE DATA TO ITS REAL CONSUMER. Before claiming a leak/injection/RCE, follow the tainted "
            "value to where it actually goes. A value that is only displayed, or discarded, or never reaches a "
            "durable log / the model's context / a shell, is NOT a leak. Do not assert a failure chain you did "
            "not follow end to end.\n"
            "3. THREAT MODEL: this is a SINGLE-USER LOCAL developer tool, not a multi-tenant service. A "
            "same-user local file write, a self-edited config file, and an operator-configured command are "
            "TRUSTED inputs — if an attacker already has them, the machine is already compromised. Do not score "
            "those as external attacks.\n"
            "4. REFUTE YOUR OWN FINDING: ask 'what guard, branch, type-check, or comment would make this a "
            "false positive?' and go look for it. If you cannot state a CONCRETE failure (specific inputs → "
            "specific wrong output/crash) that you traced in the real code, it is a hunch — mark it LOW/"
            "speculative or drop it.\n"
            "\nFor each surviving finding give: file:line, the concrete failure (inputs → wrong outcome), the "
            "severity per the rubric, and a one-line fix. Group by severity. If nothing rises above LOW, say so "
            "plainly — a clean area is a valid result. Do NOT ask the user."
        ),
    ),
    # An independent ADVERSARIAL verifier. Runs in a FRESH slice and returns only a VERDICT + evidence,
    # giving the parent a skeptical second opinion without any context crossing the seal.
    # Read-only EXCEPT running checks: read/grep + run_command/execute_code (to build/test/probe), no edit
    # tools (the allowlist is enforced at runtime in subagent.py). It is "writable" by classification (shell
    # is not read-only) so it serializes vs other writers — correct for a verifier that runs tests.
    "verification": AgentSpec(
        name="verification",
        description="Independent adversarial VERIFIER — given a change/claim, TRY TO BREAK IT (reproduce, run "
                    "build/tests, probe edges) and return VERDICT: PASS/FAIL/PARTIAL with command evidence. "
                    "Read-only except running checks. Spawn after a non-trivial change, before reporting done.",
        tools=READ_ONLY_TOOLS + ("run_command", "execute_code"),
        reasoning="full",
        summary_is_deliverable=True,   # a FAIL verdict normally ends on a failing check — that's the product,
        #                                not a crash; don't reclassify it as "did not finish cleanly".
        system_prompt=(
            "You are an independent VERIFICATION subagent. Your job is NOT to confirm the work is done — it is "
            "to TRY TO BREAK IT. You are given a task/claim and the change that was made; verify it "
            "INDEPENDENTLY and decide.\n"
            "Avoid two failure modes: (1) verification AVOIDANCE — reading code and narrating what you WOULD "
            "test, then writing PASS. Reading is NOT verification; RUN it. (2) being seduced by the first 80% "
            "— a passing test suite or the happy path is not proof; your value is the last 20%.\n"
            "DO NOT MODIFY THE PROJECT: no editing/creating/deleting project files, no installing deps, no git "
            "writes. You MAY write EPHEMERAL probe scripts to a temp dir WHERE THE SANDBOX ALLOWS (e.g. $TMPDIR "
            "or /tmp, via run_command/execute_code) and clean up after yourself.\n"
            "Method: REPRODUCE the original issue/scenario; run the cheapest sufficient build/test; then RUN at "
            "least ONE adversarial probe — a boundary/empty/large input, idempotency, the EXACT property the "
            "task names, or a related path that could regress. The implementer is also an LLM, so its tests may "
            "be happy-path — verify end-to-end yourself.\n"
            "Before PASS: you must have RUN at least one adversarial probe and observed its real output. Before "
            "FAIL: check the issue isn't already handled elsewhere or intentional.\n"
            "Format every check as — Check: <what> / Command: <exact> / Output: <actual observed, not "
            "paraphrased> / PASS or FAIL. A check with no command output is a SKIP, not a PASS. END your "
            "summary with EXACTLY one line: 'VERDICT: PASS' or 'VERDICT: FAIL' or 'VERDICT: PARTIAL' (PARTIAL "
            "only for environment limits — missing tool/deps/can't run — never for 'unsure'). Do NOT ask the user."
        ),
    ),
}


def _parse_agent_md(path: str) -> AgentSpec | None:
    """Parse an agent file: optional `---` frontmatter (name/description/tools/reasoning) + body = system
    prompt. Never raises — a malformed/unreadable file is skipped (returns None)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end == -1:
            # opening fence but no closing one (authoring typo). FAIL CLOSED: don't fall through to the
            # no-frontmatter path, which would leave tools=None (= full writable surface) for a file that
            # was trying to declare a restrictive tool list. Skip it, per the "malformed → skipped" contract.
            return None
        if end != -1:
            for line in text[3:end].splitlines():
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    name = meta.get("name") or os.path.splitext(os.path.basename(path))[0]
    if not name:
        return None
    tools_raw = meta.get("tools")
    # #58: accept both the scalar list `tools: a, b` AND inline YAML `tools: [a, b]` — strip brackets/quotes
    # before splitting so a bracketed value doesn't become tool names like "[a".
    # A PRESENT-but-blank `tools:` means restrict to ZERO tools (read-only, matching `tools: []`); only an
    # ABSENT key grants the full writable surface (None).
    if "tools" in meta and not str(tools_raw or "").strip():
        tools = ()
    elif tools_raw:
        tools = tuple(t for t in tools_raw.replace(",", " ").replace("[", " ").replace("]", " ")
                      .replace("'", " ").replace('"', " ").split() if t)
    else:
        tools = None
    reasoning = (meta.get("reasoning") or "").lower() or None
    return AgentSpec(name=name, description=meta.get("description", ""),
                     tools=tools, reasoning=reasoning, system_prompt=body.strip())


def load_agents(roots) -> dict[str, AgentSpec]:
    """Built-in agents overlaid with user-defined `<root>/agents/*.md` (later roots / user files win by
    name). `roots` are dirs that MAY contain an `agents/` subdir."""
    out = dict(BUILTIN_AGENTS)
    for root in roots or []:
        adir = os.path.join(root, "agents")
        if not os.path.isdir(adir):
            continue
        for path in sorted(glob.glob(os.path.join(adir, "*.md"))):
            spec = _parse_agent_md(path)
            if spec:
                out[spec.name] = spec
    return out
