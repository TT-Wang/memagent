"""Independent behavioral oracle for the revert-by-reference notes-CLI scenario.

This file is NOT given to the benchmarked agent to edit. It drives the agent's
final CLI (``python -m notes.cli``) in fresh subprocesses inside the agent's
workdir and checks the CUMULATIVE behavior demanded by all eight turns, with
special weight on the two history-addressed edits:

  * the turn-6 revert: tags must be stored verbatim again, but the turn-5
    case-insensitive search (including its tag matching) must have SURVIVED
    the removal of the shared normalization;
  * the turn-8 opt-in ``--normalize-tags`` flag must reproduce the EXACT
    removed semantics: lowercase + strip + internal-whitespace-runs -> '-'
    (probe: 'My  Tag ' -> 'my-tag').

The agent's code is never imported in-process; every probe is a subprocess,
and storage checks read the JSONL file directly (the storage format is part
of the seed's documented contract). Each check carries a tag.
"""
import json
import os
import subprocess
import sys
from datetime import datetime

PY = sys.executable
TIMEOUT = 30


class _Probe:
    def __init__(self, workdir):
        self.workdir = workdir
        self.fails = []
        self.last_err = ""

    def check(self, cond, tag):
        if not cond:
            self.fails.append(tag)

    def cli(self, notes_file, args, tag):
        """Run the CLI; a nonzero exit or timeout records ``tag`` + '_rc'.

        Returns stdout ('' on failure). Every probe call demands rc == 0 so
        that an unsupported flag (argparse exit 2, empty stdout) can never
        masquerade as a passing negative check.
        """
        try:
            proc = subprocess.run(
                [PY, "-m", "notes.cli", "--file", notes_file] + list(args),
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            self.fails.append(tag + "_timeout")
            return ""
        if proc.returncode != 0:
            self.fails.append(tag + "_rc")
            self.last_err = (proc.stderr or proc.stdout or "").strip()
            return ""
        return proc.stdout or ""

    def records(self, notes_file):
        path = os.path.join(self.workdir, notes_file)
        if not os.path.exists(path):
            self.fails.append("missing_file_" + notes_file)
            return []
        recs = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    self.fails.append("jsonl_format_" + notes_file)
                    return []
        return recs

    def rec(self, notes_file, title):
        for r in self.records(notes_file):
            if r.get("title") == title:
                return r
        return None

    def inject(self, notes_file, record):
        path = os.path.join(self.workdir, notes_file)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def _iso_parses(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def verify(workdir):
    for rel in (os.path.join("notes", "store.py"), os.path.join("notes", "cli.py")):
        if not os.path.isfile(os.path.join(workdir, rel)):
            return (False, rel + " is missing")

    p = _Probe(workdir)
    check = p.check

    # ---- scenario A: seed behavior + tags (t1) + created_at/--since (t2)
    #      + exact-match list --tag after the revert (t6) -------------------
    fa = "verify_a.jsonl"
    p.cli(fa, ["add", "--tag", "Work", "--tag", "Home", "ZQALPHA", "alpha body text"], "t1_add_tagged")
    p.cli(fa, ["add", "ZQBETA", "beta body text"], "seed_add")
    ra = p.rec(fa, "ZQALPHA")
    rb = p.rec(fa, "ZQBETA")
    check(ra is not None and rb is not None, "seed_records_written")
    if ra is not None:
        check(ra.get("tags") == ["Work", "Home"], "t1_tags_field_raw")
        check(_iso_parses(ra.get("created_at")), "t2_created_at_iso")
    if rb is not None:
        check(rb.get("tags") == [], "t1_tags_default_empty")

    out = p.cli(fa, ["list"], "seed_list")
    check("ZQALPHA" in out and "ZQBETA" in out, "seed_list_shows_all")
    out = p.cli(fa, ["find", "beta body"], "seed_find")
    check("ZQBETA" in out and "ZQALPHA" not in out, "seed_find_substring")
    out = p.cli(fa, ["find", "no-such-text-zz"], "seed_find_neg")
    check("ZQALPHA" not in out and "ZQBETA" not in out, "seed_find_negative")

    out = p.cli(fa, ["list", "--tag", "Work"], "t1_list_tag")
    check("ZQALPHA" in out and "ZQBETA" not in out, "t1_list_tag_filter")
    out = p.cli(fa, ["list", "--tag", "work"], "t6_list_tag_case")
    check("ZQALPHA" not in out, "t6_list_tag_exact_case")

    out = p.cli(fa, ["list", "--since", "2000-01-01"], "t2_since_past")
    check("ZQALPHA" in out and "ZQBETA" in out, "t2_since_past_includes")
    out = p.cli(fa, ["list", "--since", "2999-01-01"], "t2_since_future")
    check("ZQALPHA" not in out and "ZQBETA" not in out, "t2_since_future_excludes")

    # ---- scenario B: archive / unarchive / --all (t4) --------------------
    fb = "verify_b.jsonl"
    p.cli(fb, ["add", "ZQARCH1", "first archive test body"], "t4_add1")
    p.cli(fb, ["add", "ZQARCH2", "second body here"], "t4_add2")
    r1 = p.rec(fb, "ZQARCH1")
    id1 = r1.get("id") if r1 else None
    check(id1 is not None, "t4_id_present")
    if id1 is not None:
        p.cli(fb, ["archive", str(id1)], "t4_archive")
        r1 = p.rec(fb, "ZQARCH1")
        check(r1 is not None and r1.get("archived") is True, "t4_archived_flag_true")
        out = p.cli(fb, ["list"], "t4_list")
        check("ZQARCH1" not in out and "ZQARCH2" in out, "t4_archived_hidden_from_list")
        out = p.cli(fb, ["list", "--all"], "t4_list_all")
        check("ZQARCH1" in out and "ZQARCH2" in out, "t4_list_all_shows")
        out = p.cli(fb, ["find", "first archive"], "t4_find")
        check("ZQARCH1" not in out, "t4_archived_hidden_from_find")
        out = p.cli(fb, ["find", "first archive", "--all"], "t4_find_all")
        check("ZQARCH1" in out, "t4_find_all_shows")
        p.cli(fb, ["unarchive", str(id1)], "t4_unarchive")
        out = p.cli(fb, ["list"], "t4_list_after_unarchive")
        check("ZQARCH1" in out, "t4_unarchive_restores")

    # ---- scenario C: search (t5) surviving the revert (t6) ---------------
    fc = "verify_c.jsonl"
    p.cli(fc, ["add", "--tag", "DevOps", "ZQINFRA", "pipelines and clusters"], "t5_add_tagged")
    p.cli(fc, ["add", "ZQMEET", "Quarterly Planning agenda"], "t5_add_plain")
    p.cli(fc, ["add", "ZQNOPE", "nothing relevant here"], "t5_add_noise")
    out = p.cli(fc, ["search", "quarterly"], "t5_search_body")
    check("ZQMEET" in out and "ZQINFRA" not in out, "t5_search_ci_body")
    out = p.cli(fc, ["search", "zqmeet"], "t5_search_title")
    check("ZQMEET" in out, "t5_search_ci_title")
    out = p.cli(fc, ["search", "devops"], "t5_search_tag_lower")
    check("ZQINFRA" in out and "ZQNOPE" not in out, "t6_search_tag_survives_revert")
    out = p.cli(fc, ["search", "DEVOPS"], "t5_search_tag_upper")
    check("ZQINFRA" in out, "t6_search_tag_ci_upper")
    out = p.cli(fc, ["search", "qqqqq-no-match"], "t5_search_neg")
    check("ZQINFRA" not in out and "ZQMEET" not in out and "ZQNOPE" not in out, "t5_search_negative")
    ri = p.rec(fc, "ZQINFRA")
    if ri is not None and ri.get("id") is not None:
        p.cli(fc, ["archive", str(ri["id"])], "t5_archive_infra")
        out = p.cli(fc, ["search", "devops"], "t5_search_archived")
        check("ZQINFRA" not in out, "t5_search_archived_hidden")
        out = p.cli(fc, ["search", "devops", "--all"], "t5_search_all")
        check("ZQINFRA" in out, "t5_search_all_shows")

    # ---- scenario D: raw storage after the revert (t6) + legacy data -----
    fd = "verify_d.jsonl"
    p.cli(fd, ["add", "--tag", "API Design", "ZQRAW1", "raw tag body"], "t6_add_raw")
    rr = p.rec(fd, "ZQRAW1")
    check(rr is not None and rr.get("tags") == ["API Design"], "t6_raw_stored_verbatim")
    p.cli(fd, ["add", "--tag", " Spaced  Out ", "ZQRAW2", "spaces kept"], "t6_add_spaced")
    rs = p.rec(fd, "ZQRAW2")
    check(rs is not None and rs.get("tags") == [" Spaced  Out "], "t6_raw_whitespace_kept")
    out = p.cli(fd, ["list", "--tag", "API Design"], "t6_list_raw")
    check("ZQRAW1" in out and "ZQRAW2" not in out, "t6_list_exact_raw")
    # A record written back when normalization was live must stay reachable.
    p.inject(fd, {
        "id": 999,
        "title": "ZQLEGACY",
        "body": "from the old days",
        "tags": ["my-tag"],
        "created_at": "2024-01-01T00:00:00+00:00",
        "archived": False,
    })
    out = p.cli(fd, ["list", "--tag", "my-tag"], "t6_legacy_list")
    check("ZQLEGACY" in out, "t6_legacy_listable_by_stored_form")
    out = p.cli(fd, ["search", "MY-TAG"], "t6_legacy_search")
    check("ZQLEGACY" in out, "t6_legacy_searchable_ci")

    # ---- scenario E: rename-tag on raw tags (t7) -------------------------
    fe = "verify_e.jsonl"
    p.cli(fe, ["add", "--tag", "My Tag", "ZQRN1", "b1"], "t7_add1")
    p.cli(fe, ["add", "--tag", "My Tag", "--tag", "Other", "ZQRN2", "b2"], "t7_add2")
    p.cli(fe, ["add", "--tag", "my tag", "ZQRN3", "b3"], "t7_add3")
    r2 = p.rec(fe, "ZQRN2")
    if r2 is not None and r2.get("id") is not None:
        p.cli(fe, ["archive", str(r2["id"])], "t7_archive2")
    p.cli(fe, ["rename-tag", "My Tag", "Project X"], "t7_rename")
    r1 = p.rec(fe, "ZQRN1")
    r2 = p.rec(fe, "ZQRN2")
    r3 = p.rec(fe, "ZQRN3")
    check(r1 is not None and r1.get("tags") == ["Project X"], "t7_rename_applied_raw")
    if r2 is not None:
        tags2 = r2.get("tags", [])
        check("Project X" in tags2 and "My Tag" not in tags2, "t7_rename_includes_archived")
        check("Other" in tags2, "t7_rename_preserves_other_tags")
    else:
        check(False, "t7_rename_includes_archived")
    check(r3 is not None and r3.get("tags") == ["my tag"], "t7_rename_exact_match_only")
    p.cli(fe, ["rename-tag", "Other", "New  Thing"], "t7_rename2")
    r2 = p.rec(fe, "ZQRN2")
    if r2 is not None:
        tags2 = r2.get("tags", [])
        check("New  Thing" in tags2, "t7_rename_new_name_verbatim")
        check("new-thing" not in tags2, "t7_rename_no_normalization")

    # ---- scenario F: opt-in --normalize-tags (t8) ------------------------
    ff = "verify_f.jsonl"
    p.cli(ff, ["add", "--normalize-tags", "--tag", "My  Tag ", "ZQNORM1", "b"], "t8_add_norm")
    r1 = p.rec(ff, "ZQNORM1")
    check(r1 is not None and r1.get("tags") == ["my-tag"], "t8_norm_exact_mapping")
    p.cli(ff, ["add", "--normalize-tags", "--tag", " Deep   Work ", "--tag", "PYTHON", "ZQNORM2", "b"], "t8_add_norm_multi")
    r2 = p.rec(ff, "ZQNORM2")
    check(r2 is not None and sorted(r2.get("tags", [])) == ["deep-work", "python"], "t8_norm_multi_mapping")
    p.cli(ff, ["add", "--tag", "My  Tag ", "ZQNORM3", "b"], "t8_add_plain")
    r3 = p.rec(ff, "ZQNORM3")
    check(r3 is not None and r3.get("tags") == ["My  Tag "], "t8_flagless_stays_raw")
    out = p.cli(ff, ["list", "--normalize-tags", "--tag", "MY  TAG"], "t8_list_norm")
    check("ZQNORM1" in out, "t8_norm_list_query_matches")
    out = p.cli(ff, ["list", "--tag", "my-tag"], "t8_list_plain")
    check("ZQNORM1" in out, "t8_flagless_exact_still_works")
    check("ZQNORM3" not in out, "t8_flagless_no_hidden_normalization")

    if p.fails:
        detail = ",".join(p.fails)
        msg = "FAILS: " + detail
        if p.last_err:
            msg += "\nlast cli error: " + p.last_err.splitlines()[-1][:200]
        return (False, msg)
    return (True, "all cumulative + revert/recall checks passed")
