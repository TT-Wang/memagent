"""Independent behavioral oracle for the standing-constraints scenario.

This file is NOT given to the benchmarked agent to edit. It imports the
agent's final ``auditlog.service.AccountService`` in a fresh subprocess and
replays a scripted session covering every turn, plus the probes an agent
that lost early context would fail:

  * the turn-2 standing 'reason' rule (introspected on every post-rule
    method, plus behavioral empty/whitespace rejection with no record),
  * C1 one-record-per-account log deltas (bulk_import must log per row,
    export_statement must log nothing),
  * C2 byte-identical log prefixes across compact and undo_last,
  * C3 strict ts monotonicity end to end,
  * cross-turn state: checkpoints exclude turn-1-closed accounts, rename
    carries the turn-2 frozen flag, reopen distinguishes closed vs unknown.

Run via subprocess so a crashing/looping import cannot take down the parent
and so import caching never masks a broken module.
"""
import os
import subprocess
import sys
import textwrap

PY = sys.executable


PROBE = textwrap.dedent('''
    import inspect
    import sys

    from auditlog.service import AccountService

    fails = []
    def check(cond, tag):
        if not cond:
            fails.append(tag)

    def raises(exc, fn, *a, **kw):
        try:
            fn(*a, **kw)
        except exc:
            return True
        except Exception:
            return False
        return False

    svc = AccountService()
    def recs():
        return svc.log.records()
    def nrec():
        return len(svc.log.records())

    # ---- seed behavior still intact -----------------------------------
    svc.open_account("alice")
    svc.deposit("alice", 100)
    svc.withdraw("alice", 30)
    check(svc.balance("alice") == 70, "seed_balance")
    check(nrec() == 3, "seed_c1_one_record_each")
    check([r.get("action") for r in recs()] ==
          ["open_account", "deposit", "withdraw"], "seed_actions")
    check(raises(KeyError, svc.deposit, "ghost", 1), "seed_unknown_keyerror")
    check(raises(ValueError, svc.withdraw, "alice", 9999), "seed_overdraft")
    check(raises(ValueError, svc.deposit, "alice", 0), "seed_bad_amount")
    check(raises(ValueError, svc.open_account, "alice"), "seed_dup_open")
    check(nrec() == 3, "seed_errors_append_nothing")
    r0 = recs()
    r0[0]["action"] = "HACK"
    check(recs()[0].get("action") == "open_account", "seed_records_are_copies")

    # ---- turn 1: close_account ----------------------------------------
    svc.open_account("tmp")
    svc.deposit("tmp", 5)
    check(raises(ValueError, svc.close_account, "tmp"), "t1_close_nonzero")
    svc.withdraw("tmp", 5)
    n0 = nrec()
    svc.close_account("tmp")
    check(nrec() == n0 + 1, "t1_close_one_record")
    last = recs()[-1]
    check(last.get("action") == "close_account" and
          last.get("account") == "tmp", "t1_close_record_shape")
    check(raises(ValueError, svc.deposit, "tmp", 1), "t1_closed_deposit")
    check(raises(ValueError, svc.withdraw, "tmp", 1), "t1_closed_withdraw")
    check(raises(ValueError, svc.close_account, "tmp"), "t1_close_twice")
    check(raises(KeyError, svc.close_account, "ghost"), "t1_close_unknown")

    # ---- turn 2: freeze/unfreeze + reason threading -------------------
    n0 = nrec()
    svc.freeze_account("alice", reason="fraud review")
    check(nrec() == n0 + 1, "t2_freeze_one_record")
    last = recs()[-1]
    check(last.get("action") == "freeze_account" and
          last.get("account") == "alice", "t2_freeze_record_shape")
    check(last.get("reason") == "fraud review", "t2_reason_threaded")
    check(raises(ValueError, svc.deposit, "alice", 1), "t2_frozen_deposit")
    check(raises(ValueError, svc.withdraw, "alice", 1), "t2_frozen_withdraw")
    check(svc.balance("alice") == 70, "t2_frozen_balance_readable")
    svc.unfreeze_account("alice", reason="cleared by compliance")
    last = recs()[-1]
    check(last.get("action") == "unfreeze_account", "t2_unfreeze_record")
    check(last.get("reason") == "cleared by compliance", "t2_unfreeze_reason")
    svc.deposit("alice", 10)
    check(svc.balance("alice") == 80, "t2_unfrozen_works")

    # ---- the standing rule: signatures ---------------------------------
    POST_RULE = ["freeze_account", "unfreeze_account", "bulk_import",
                 "adjust_balance", "compact", "undo_last",
                 "rename_account", "reopen_account"]
    for mname in POST_RULE:
        m = getattr(AccountService, mname, None)
        check(m is not None, "sig_missing_" + mname)
        if m is not None:
            try:
                params = inspect.signature(m).parameters
            except (TypeError, ValueError):
                params = {}
            check("reason" in params, "sig_no_reason_" + mname)
    check(getattr(AccountService, "close_account", None) is not None,
          "sig_missing_close_account")
    check(getattr(AccountService, "export_statement", None) is not None,
          "sig_missing_export_statement")

    # ---- the standing rule: empty/whitespace reason rejected, no record,
    # ---- no state change ----------------------------------------------
    n0 = nrec()
    check(raises(ValueError, svc.freeze_account, "alice", reason=""),
          "rule_empty_freeze")
    check(raises(ValueError, svc.freeze_account, "alice", reason="   "),
          "rule_ws_freeze")
    svc.freeze_account("alice", reason="temporary hold")
    check(raises(ValueError, svc.unfreeze_account, "alice", reason=" "),
          "rule_ws_unfreeze")
    svc.unfreeze_account("alice", reason="hold over")
    check(raises(ValueError, svc.bulk_import, [("zz", 1)], reason=""),
          "rule_empty_bulk_import")
    check(raises(KeyError, svc.balance, "zz"), "rule_bulk_reject_no_account")
    check(raises(ValueError, svc.adjust_balance, "alice", 1, reason="  "),
          "rule_ws_adjust")
    check(svc.balance("alice") == 80, "rule_adjust_reject_no_change")
    check(raises(ValueError, svc.compact, reason=""), "rule_empty_compact")
    check(raises(ValueError, svc.undo_last, reason="\\t"), "rule_ws_undo")
    check(svc.balance("alice") == 80, "rule_undo_reject_no_change")
    check(raises(ValueError, svc.rename_account, "alice", "alicia", reason=""),
          "rule_empty_rename")
    check(svc.balance("alice") == 80, "rule_rename_reject_intact")
    check(raises(ValueError, svc.reopen_account, "tmp", reason="   "),
          "rule_ws_reopen")
    check(raises(ValueError, svc.deposit, "tmp", 1),
          "rule_reopen_reject_still_closed")
    check(nrec() == n0 + 2, "rule_rejects_append_nothing")

    # ---- turn 3: bulk_import logs one record PER ROW (C1) --------------
    n0 = nrec()
    svc.bulk_import([("carol", 50), ("dave", 0), ("erin", 7)],
                    reason="migration from legacy core")
    new = recs()[n0:]
    check(len(new) == 3, "t3_c1_one_record_per_row")
    check(sorted(r.get("account") for r in new) == ["carol", "dave", "erin"],
          "t3_each_row_named")
    check(all(r.get("reason") == "migration from legacy core" for r in new),
          "t3_reason_threaded")
    check(svc.balance("carol") == 50 and svc.balance("dave") == 0 and
          svc.balance("erin") == 7, "t3_balances")
    n0 = nrec()
    check(raises(ValueError, svc.bulk_import,
                 [("fred", 1), ("carol", 2)], reason="retry"),
          "t3_existing_name_rejected")
    check(raises(KeyError, svc.balance, "fred"), "t3_atomic_nothing_imported")
    check(raises(ValueError, svc.bulk_import, [("gina", -1)], reason="oops"),
          "t3_negative_rejected")
    check(raises(ValueError, svc.bulk_import,
                 [("hank", 1), ("hank", 2)], reason="dup"),
          "t3_dup_within_rows")
    check(raises(ValueError, svc.bulk_import, [("tmp", 1)], reason="revive"),
          "t3_closed_name_rejected")
    check(raises(ValueError, svc.bulk_import, [("", 1)], reason="blank row"),
          "t3_empty_name_rejected")
    check(raises(ValueError, svc.bulk_import, [("ivan", 3), (42, 2)],
                 reason="typed row"),
          "t3_nonstring_name_rejected")
    check(raises(KeyError, svc.balance, "ivan"),
          "t3_bad_name_atomic_nothing_imported")
    check(nrec() == n0, "t3_rejects_append_nothing")

    # ---- turn 4: adjust_balance (works on frozen, never below zero) ----
    n0 = nrec()
    svc.adjust_balance("alice", 25, reason="audit correction")
    check(svc.balance("alice") == 105, "t4_adjust_applied")
    check(nrec() == n0 + 1, "t4_one_record")
    last = recs()[-1]
    check(last.get("reason") == "audit correction", "t4_reason_threaded")
    check(25 in list(last.values()), "t4_delta_in_record")
    check(raises(ValueError, svc.adjust_balance, "dave", -1, reason="test"),
          "t4_below_zero")
    check(svc.balance("dave") == 0, "t4_below_zero_no_change")
    check(raises(KeyError, svc.adjust_balance, "ghost", 1, reason="x"),
          "t4_unknown")
    svc.freeze_account("dave", reason="suspicious activity")
    svc.adjust_balance("dave", 5, reason="found missing cash")
    check(svc.balance("dave") == 5, "t4_works_on_frozen")
    check(raises(ValueError, svc.deposit, "dave", 1),
          "t4_freeze_still_blocks_deposits")
    svc.unfreeze_account("dave", reason="resolved")
    svc.adjust_balance("erin", -4, reason="ledger reconciliation")
    check(svc.balance("erin") == 3, "t4_negative_adjust_applied")
    check(-4 in list(recs()[-1].values()), "t4_negative_delta_in_record")
    svc.adjust_balance("dave", -5, reason="drain duplicate credit")
    check(svc.balance("dave") == 0, "t4_negative_adjust_to_zero_ok")
    svc.adjust_balance("dave", 5, reason="restore after review")
    svc.adjust_balance("erin", 4, reason="restore after review")
    check(svc.balance("dave") == 5 and svc.balance("erin") == 7,
          "t4_adjust_restore")
    check(nrec() == n0 + 8, "t4_record_accounting")

    # ---- turn 5: compact appends checkpoints, history untouched (C2) ---
    pre = recs()
    svc.compact(reason="quarterly log compaction")
    post = recs()
    check(post[:len(pre)] == pre, "t5_c2_prefix_untouched")
    check(len(post) > len(pre), "t5_appended_something")
    new = post[len(pre):]
    check(all(r.get("action") == "checkpoint" for r in new),
          "t5_checkpoint_action")
    open_now = {"alice": 105, "carol": 50, "dave": 5, "erin": 7}
    check(sorted(r.get("account") for r in new) == sorted(open_now),
          "t5_one_checkpoint_per_open_account")
    check(len(new) == 4, "t5_exactly_one_each")
    check(all(r.get("balance") == open_now.get(r.get("account")) for r in new),
          "t5_balance_field")
    check(all(r.get("reason") == "quarterly log compaction" for r in new),
          "t5_reason_threaded")
    check(not any(r.get("account") == "tmp" for r in new),
          "t5_no_checkpoint_for_closed")

    # ---- turn 6: undo_last appends a compensating record (C2) ----------
    svc.deposit("carol", 40)
    check(svc.balance("carol") == 90, "t6_setup_deposit")
    pre = recs()
    svc.undo_last(reason="teller fat-fingered the amount")
    check(svc.balance("carol") == 50, "t6_balance_reverted")
    post = recs()
    check(len(post) == len(pre) + 1, "t6_c2_compensating_record_not_deletion")
    check(post[:len(pre)] == pre, "t6_c2_prefix_untouched")
    check(post[-1].get("reason") == "teller fat-fingered the amount",
          "t6_reason_threaded")
    svc.withdraw("erin", 3)
    svc.undo_last(reason="wrong account entirely")
    check(svc.balance("erin") == 7, "t6_undo_withdraw_restores")
    svc.deposit("erin", 9)
    svc.freeze_account("dave", reason="routine spot check")
    pre = recs()
    svc.undo_last(reason="deposit keyed to the wrong customer")
    check(svc.balance("erin") == 7, "t6_undo_finds_buried_deposit")
    check(svc.balance("dave") == 5, "t6_buried_undo_leaves_others_alone")
    post = recs()
    check(post[:len(pre)] == pre, "t6_c2_buried_prefix_untouched")
    check(len(post) == len(pre) + 1, "t6_buried_compensating_record")
    svc.unfreeze_account("dave", reason="spot check clear")
    s2 = AccountService()
    s2.open_account("solo")
    check(raises(ValueError, s2.undo_last, reason="nothing here"),
          "t6_nothing_to_undo")

    # ---- turn 7: rename carries balance AND frozen flag ----------------
    svc.freeze_account("carol", reason="pending verification")
    n0 = nrec()
    svc.rename_account("carol", "caroline", reason="legal name change")
    check(nrec() == n0 + 1, "t7_one_record")
    last = recs()[-1]
    vals = list(last.values())
    check("carol" in vals and "caroline" in vals, "t7_record_names_both")
    check(last.get("reason") == "legal name change", "t7_reason_threaded")
    check(svc.balance("caroline") == 50, "t7_balance_carried")
    check(raises(ValueError, svc.deposit, "caroline", 1),
          "t7_frozen_state_carried")
    svc.unfreeze_account("caroline", reason="verified")
    svc.deposit("caroline", 10)
    check(svc.balance("caroline") == 60, "t7_new_name_live")
    check(raises(KeyError, svc.balance, "carol"), "t7_old_name_gone")
    check(raises(KeyError, svc.deposit, "carol", 1),
          "t7_old_name_gone_mutation")
    check(raises(ValueError, svc.rename_account, "alice", "caroline",
                 reason="x"), "t7_collision_open")
    check(raises(ValueError, svc.rename_account, "alice", "tmp", reason="x"),
          "t7_collision_closed")
    check(raises(KeyError, svc.rename_account, "ghost", "spirit", reason="x"),
          "t7_unknown")
    check(raises(ValueError, svc.rename_account, "tmp", "tmp2", reason="x"),
          "t7_closed_not_renamable")
    check(nrec() == n0 + 3, "t7_record_accounting")

    # ---- turn 8: reopen distinguishes closed vs unknown ----------------
    n0 = nrec()
    svc.reopen_account("tmp", reason="customer came back")
    check(nrec() == n0 + 1, "t8_one_record")
    check(recs()[-1].get("reason") == "customer came back",
          "t8_reason_threaded")
    check(svc.balance("tmp") == 0, "t8_reopened_with_zero")
    svc.deposit("tmp", 12)
    check(svc.balance("tmp") == 12, "t8_reopened_active")
    check(raises(ValueError, svc.reopen_account, "alice", reason="x"),
          "t8_open_rejected")
    check(raises(KeyError, svc.reopen_account, "ghost", reason="x"),
          "t8_unknown")

    # ---- turn 9: export_statement is a pure read (no reason, no record)
    n0 = nrec()
    try:
        st = svc.export_statement("alice")
    except Exception:
        st = None
    check(st is not None, "t9_export_callable_without_reason")
    check(nrec() == n0, "t9_read_only_no_record")
    if isinstance(st, dict):
        check(st.get("account") == "alice", "t9_account_key")
        check(st.get("balance") == 105, "t9_balance_key")
        entries = st.get("entries")
        expect = [r for r in recs() if r.get("account") == "alice"]
        check(isinstance(entries, list) and len(entries) == len(expect),
              "t9_entries_complete")
        if isinstance(entries, list):
            check(all(isinstance(e, dict) and e.get("account") == "alice"
                      for e in entries), "t9_entries_scoped")
            ts = [e.get("ts") for e in entries if isinstance(e, dict)]
            check(all(isinstance(t, int) for t in ts) and ts == sorted(ts),
                  "t9_entries_in_order")
    else:
        check(False, "t9_returns_dict")
    check(raises(KeyError, svc.export_statement, "ghost"), "t9_unknown")

    # ---- final sweep: C1 still one-per-mutation, C3 strictly monotonic -
    n0 = nrec()
    svc.open_account("zoe")
    svc.deposit("zoe", 5)
    svc.withdraw("zoe", 2)
    check(nrec() == n0 + 3, "final_c1_still_one_each")
    allts = [r.get("ts") for r in recs()]
    check(all(isinstance(t, int) for t in allts) and
          all(b > a for a, b in zip(allts, allts[1:])),
          "final_c3_strictly_monotonic")

    if fails:
        print("FAILS:" + ",".join(sorted(set(fails))))
        sys.exit(1)
    print("ALL_OK")
    sys.exit(0)
''')


def verify(workdir):
    service = os.path.join(workdir, "auditlog", "service.py")
    if not os.path.isfile(service):
        return (False, "auditlog/service.py is missing")
    try:
        proc = subprocess.run(
            [PY, "-c", PROBE],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return (False, "probe timed out (possible infinite loop in service)")
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0 and "ALL_OK" in out:
        return (True, "all cumulative + standing-rule + invariant checks passed")
    detail = out.strip().splitlines()
    tail = "\n".join(detail[-15:]) if detail else "(no output)"
    return (False, "probe failed:\n" + tail)
