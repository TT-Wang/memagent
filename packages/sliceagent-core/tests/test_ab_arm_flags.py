"""P2 A/B arm flags — registration + behavioral flip. No model/network.

Contract: with flags OFF nothing changes (A arm = today's behavior, incl. the analyze_turn
fallback); AGENT_EXPERIMENTAL_INTENT_MECHANICAL=1 makes every no-contract turn mechanical
(authority_spans=(), no effect grants — the production CLI shape); the overflow_simple flag is
registered and read LIVE from the environment. A typo'd flag id resolves False (the silent-A/A
trap the preflight guards)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent_core import flags  # noqa: E402
from sliceagent_core.pfc import Slice, record_user  # noqa: E402
from sliceagent_core.session import Session  # noqa: E402
from sliceagent_core.memory_null import NullMemory  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


class _env:
    def __init__(self, **kv):
        self.kv = kv
        self.saved = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *a):
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


@check
def both_arm_flags_are_registered_and_default_off():
    for flag_id in ("overflow_simple", "intent_mechanical"):
        assert flag_id in flags._FLAGS, flag_id
        assert flags.enabled(flag_id) is False, flag_id
    assert flags.enabled("overflow_simpel") is False   # typo → False, never crash (R4 trap)


@check
def a_arm_keeps_the_analyze_turn_fallback():
    s = Slice(); s.reset("t")
    record_user(s, "edit README")           # imperative: the fallback grammar derives grants
    admission = s.intent.turn_admission
    assert admission is not None and admission.effect_grants, \
        "A arm must still run analyze_turn on no-contract turns"


@check
def b_arm_synthesizes_the_mechanical_envelope():
    with _env(AGENT_EXPERIMENTAL_INTENT_MECHANICAL="1"):
        s = Slice(); s.reset("t")
        record_user(s, "edit README")
        admission = s.intent.turn_admission
        assert admission is not None
        assert admission.request_text == "edit README"
        assert admission.effect_grants == () and admission.authority_spans == (), \
            "B arm must be mechanical — no grammar-derived grants/spans"


@check
def b_arm_covers_continue_topic_too():
    with _env(AGENT_EXPERIMENTAL_INTENT_MECHANICAL="1"):
        session = Session(NullMemory(), "arm-test-session")
        session.new_topic("first request")
        session.continue_topic("now edit README")
        admission = session.active().intent.turn_admission
        assert admission is not None and admission.effect_grants == (), \
            "continue_topic must synthesize the mechanical envelope under the B arm"


@check
def explicit_contract_wins_in_both_arms():
    from sliceagent_core.intent import analyze_turn
    with _env(AGENT_EXPERIMENTAL_INTENT_MECHANICAL="1"):
        s = Slice(); s.reset("t")
        analyzed = analyze_turn("edit README")
        record_user(s, "edit README", contract=analyzed)
        assert s.intent.turn_admission.effect_grants, \
            "an explicitly passed contract must never be replaced by the arm flag"


@check
def overflow_simple_reads_env_live():
    assert flags.enabled("overflow_simple") is False
    with _env(AGENT_EXPERIMENTAL_OVERFLOW_SIMPLE="1"):
        assert flags.enabled("overflow_simple") is True
    assert flags.enabled("overflow_simple") is False


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
