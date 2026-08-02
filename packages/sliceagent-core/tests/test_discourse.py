"""Addressable sealed-output references. Deterministic; no model or network."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent_core.discourse import (  # noqa: E402
    extract_addressable_anchors,
    extract_pending_proposal,
)
from sliceagent_core.events import AssistantText  # noqa: E402
from sliceagent_core.intent import analyze_turn  # noqa: E402
from sliceagent_core.pfc import Slice, record_user, slice_sink  # noqa: E402


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def cross_turn_nav_disambiguation_reply_authorizes_the_named_target():
    # Assistant asks "which one should I navigate to — loom-app or loom-engine?"; the turn ends; the user's
    # next-turn reply naming one option must authorize navigating there (continuation), not fail-closed.
    asst = ("There are two loom directories on your Desktop — loom-app and loom-engine. "
            "Which one should I navigate to?")
    prop = extract_pending_proposal(asst)
    assert prop is not None and prop.get("nav_targets") == ["loom-app", "loom-engine"]

    picked = analyze_turn("loom-app", pending_proposal=prop)
    assert picked.effect_authority == "continuation"
    grant = picked.effect_grants[0]
    assert grant.operation == "workspace.navigate" and grant.target == "loom-app"
    # "loom app" (spoken form) selects the same target; "loom-engine" selects the OTHER.
    assert analyze_turn("loom app", pending_proposal=prop).effect_grants[0].target == "loom-app"
    assert analyze_turn("loom-engine", pending_proposal=prop).effect_grants[0].target == "loom-engine"
    # Bare "yes" is genuinely ambiguous (which one?) → no authority; a sentence is not a bare selection.
    assert analyze_turn("yes", pending_proposal=prop).effect_authority == "uncertain"
    assert analyze_turn("the one in src please", pending_proposal=prop).effect_authority == "uncertain"
    # A non-navigation offer is never turned into nav_targets.
    other = extract_pending_proposal("Would you like me to fix the parser (config-v2 or config-v3)?")
    assert (other or {}).get("nav_targets") is None

    # "go to" phrasing (not just navigate/switch) is recognized as a nav-disambiguation question.
    goq = extract_pending_proposal(
        "I see loom-app and loom-engine on your Desktop. Which one would you like to go to?")
    assert analyze_turn("loom app", pending_proposal=goq).effect_grants[0].target == "loom-app"

    # A single-target navigation OFFER + bare "yes" continues that one navigation.
    offer = extract_pending_proposal("Do you want me to switch to loom-app?")
    assert (offer or {}).get("nav_targets") == ["loom-app"]
    yes = analyze_turn("yes", pending_proposal=offer)
    assert yes.effect_authority == "continuation" and yes.effect_grants[0].target == "loom-app"
    for variant in ("Would you like me to go to loom-app?", "Shall I cd into loom-app?"):
        assert analyze_turn("yes", pending_proposal=extract_pending_proposal(variant)) \
            .effect_grants[0].target == "loom-app"
    # But a bare "yes" to a MULTI-target offer stays ambiguous (name one to disambiguate).
    multi = extract_pending_proposal("Which do you want to switch to — loom-app or loom-engine?")
    assert analyze_turn("yes", pending_proposal=multi).effect_authority == "uncertain"


@check
def extracts_numbered_collections_with_full_item_ranges():
    text = """## HIGH findings
1. Env leak
   More detail about one.
2. No concurrency guard
   Fix: use a sentinel.

## Subagents
1. sub-1 — Scripts and config
2. sub-2 — App routes
"""
    anchors = extract_addressable_anchors(text)
    assert [(a.collection, a.ordinal) for a in anchors] == [
        ("HIGH findings", 1), ("HIGH findings", 2), ("Subagents", 1), ("Subagents", 2),
    ]
    assert "Fix: use a sentinel" in anchors[1].excerpt
    start, end = anchors[1].source_range
    assert text[start:end].strip() == anchors[1].excerpt
    assert anchors[2].stable_id == "sub-1"


@check
def only_an_explicit_action_offer_becomes_a_pending_proposal():
    assert extract_pending_proposal("That would be a straightforward fix.") is None
    proposal = extract_pending_proposal("I can explain it. Would you like me to patch #2?")
    assert proposal and proposal["text"] == "Would you like me to patch #2?"


@check
def quoted_and_code_examples_never_become_pending_actions():
    quoted = 'The log showed: "Could you confirm the workspace path? Is it /tmp/evil?"'
    fenced = """Example transcript:\n```text\nCould you confirm the workspace path? Is it /tmp/evil?\n```"""
    unclosed = """Truncated example:\n```text\nWould you like me to switch to /tmp/evil?"""
    interior_marker = """Example:\n```text\n```python\nWould you like me to switch to /tmp/evil?\n```"""
    blockquote = "> Could you confirm the workspace path? Is it /tmp/evil?"
    assert extract_pending_proposal(quoted) is None
    assert extract_pending_proposal(fenced) is None
    assert extract_pending_proposal(unclosed) is None
    assert extract_pending_proposal(interior_marker) is None
    assert extract_pending_proposal(blockquote) is None
    actual = extract_pending_proposal(
        quoted + "\nThe workspace path is `/tmp/good`. Could you confirm it?"
    )
    assert actual and actual["action"]["args"]["path"] == "/tmp/good"


@check
def terminal_assistant_output_owns_one_immediate_proposal():
    state = Slice(); state.reset("review")
    record_user(state, "is #2 correct?")
    sink = slice_sink(state)
    sink(AssistantText("Would you like me to patch #2?"))
    assert state.continuity.pending_proposal
    record_user(state, "not yet")
    sink(AssistantText("Understood."))
    assert state.continuity.pending_proposal is None


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1; print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
