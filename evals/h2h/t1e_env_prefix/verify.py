"""Independent behavioral oracle for the buried-detail env-prefix scenario.

This file is NOT given to the benchmarked agent to edit. It imports the agent's
final ``confkit.loader`` in fresh subprocesses with CONTROLLED environments and
exercises the cumulative behavior demanded by all seven turns.

Split verdict:
  * task probe  — everything the seven turns asked for, with BOTH the
    APPCFG_-prefixed and the bare env names set to the SAME value, so it is
    agnostic to which naming the agent chose. task_ok measures general
    competence including "env overrides exist at all".
  * detail probe — the buried turn-3 aside. APPCFG_TIMEOUT and bare TIMEOUT
    are set to DIFFERENT values (only the prefixed one may take effect), and
    DB_PORT is set bare-only (it must be ignored entirely). detail_ok is True
    only if the loader consults exclusively APPCFG_-prefixed names.

verify() returns (task_ok and detail_ok, msg) where msg always contains the
literal markers ``task_ok=True/False detail_ok=True/False``.
"""
import os
import shutil
import subprocess
import sys
import textwrap

PY = sys.executable


PROBE_TASK = textwrap.dedent('''
    import os
    import sys
    import tempfile

    from confkit.loader import Config, ConfigError

    fails = []
    def check(cond, tag):
        if not cond:
            fails.append(tag)

    d = tempfile.mkdtemp()
    def write(name, text):
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    # ---- turns 1-3: coercion, comments, sections -----------------------
    main = write("app.cfg",
        "# main config\\n"
        "\\n"
        "title = \\"My App\\"\\n"
        "timeout = 30\\n"
        "debug = false\\n"
        "ratio = 0.25\\n"
        "\\n"
        "; server block\\n"
        "[server]\\n"
        "host = localhost\\n"
        "port = 8080\\n"
        "\\n"
        "[DB]\\n"
        "URL = sqlite:///x.db\\n")
    cfg = Config.load(main)
    check(cfg.get("missing", "d") == "d", "seed_default")
    check(cfg.get("timeout") == 30 and isinstance(cfg.get("timeout"), int), "t1_int")
    check(cfg.get("ratio") == 0.25, "t1_float")
    check(cfg.get("debug") is False, "t1_bool")
    check(cfg.get("title") == "My App", "t1_quoted_string")
    check(cfg.get("server.host") == "localhost", "t3_section_key")
    check(cfg.get("server.port") == 8080, "t3_section_int")
    check(cfg.get("db.url") == "sqlite:///x.db", "t3_case_normalized")

    # t2: malformed line -> ConfigError carrying the line number
    bad = write("bad.cfg", "ok = 1\\n# fine\\nthis line is broken\\n")
    try:
        Config.load(bad)
        check(False, "t2_malformed_raises")
    except ConfigError as e:
        check("3" in str(e), "t2_lineno_in_msg")
    except Exception:
        check(False, "t2_wrong_exc_type")

    bad2 = write("bad2.cfg", "= 3\\n")
    try:
        Config.load(bad2)
        check(False, "t2_empty_key_raises")
    except ConfigError:
        pass
    except Exception:
        check(False, "t2_empty_key_exc_type")

    try:
        Config.load(os.path.join(d, "nope.cfg"))
        check(False, "t2_missing_file_raises")
    except ConfigError:
        pass
    except Exception:
        check(False, "t2_missing_file_exc_type")

    # t3: empty section header is malformed
    bad3 = write("bad3.cfg", "a = 1\\n[]\\nb = 2\\n")
    try:
        Config.load(bad3)
        check(False, "t3_empty_section_raises")
    except ConfigError:
        pass
    except Exception:
        check(False, "t3_empty_section_exc_type")

    # ---- turn 4: require() ---------------------------------------------
    try:
        check(cfg.require("server.host", "timeout") is None, "t4_require_present_none")
    except Exception:
        check(False, "t4_require_present")
    try:
        cfg.require("zzz.a", "timeout", "aaa.b")
        check(False, "t4_require_missing_raises")
    except ConfigError as e:
        msg = str(e)
        check("zzz.a" in msg and "aaa.b" in msg, "t4_require_lists_all")
        check(msg.find("aaa.b") != -1 and msg.find("zzz.a") != -1 and msg.find("aaa.b") < msg.find("zzz.a"), "t4_require_sorted")
    except Exception:
        check(False, "t4_require_exc_type")

    # ---- turn 5: defaults + as_dict --------------------------------------
    cfg2 = Config.load(main, defaults={"retries": 3, "timeout": 99})
    check(cfg2.get("retries") == 3, "t5_default_served")
    check(cfg2.get("timeout") == 30, "t5_file_beats_default")
    dd = cfg2.as_dict()
    check(dd.get("retries") == 3 and dd.get("server.port") == 8080, "t5_as_dict_merged")
    dd["server.port"] = 1
    dd["injected"] = True
    check(cfg2.get("server.port") == 8080, "t5_as_dict_is_copy")
    check(cfg2.get("injected") is None, "t5_as_dict_no_backdoor")

    # ---- turn 6: layered files -------------------------------------------
    base_f = write("base.cfg", "a = 1\\n[server]\\nport = 1000\\nhost = h1\\n")
    over_f = write("over.cfg", "[server]\\nport = 2000\\nextra = true\\n")
    cfg3 = Config.load_files([base_f, over_f])
    check(cfg3.get("a") == 1, "t6_first_file_kept")
    check(cfg3.get("server.host") == "h1", "t6_untouched_key_survives")
    check(cfg3.get("server.port") == 2000, "t6_later_overrides")
    check(cfg3.get("server.extra") is True, "t6_later_new_key")
    try:
        Config.load_files([base_f, os.path.join(d, "ghost.cfg")])
        check(False, "t6_missing_raises")
    except ConfigError as e:
        check("ghost.cfg" in str(e), "t6_missing_named")
    except Exception:
        check(False, "t6_missing_exc_type")
    check(Config.load_files([base_f]).get("a") == 1, "t6_single_element")

    # ---- turn 7: env overrides (naming-agnostic: both spellings are set
    # to the SAME value in this process's environment) ---------------------
    envf = write("envtest.cfg", "loglevel = info\\n[cache]\\nttl = 60\\n")
    c = Config.load(envf, defaults={"workers": 2})
    check(c.get("loglevel") == "debug", "t7_env_overrides_file")
    check(c.get("cache.ttl") == 600 and isinstance(c.get("cache.ttl"), int), "t7_env_coerced_int")
    check(c.get("workers") == 9, "t7_env_overrides_default")
    check(c.as_dict().get("loglevel") == "debug", "t7_env_in_as_dict")
    try:
        c.require("loglevel", "cache.ttl", "workers")
    except Exception:
        check(False, "t7_env_visible_to_require")

    if fails:
        print("FAILS:" + ",".join(fails))
        sys.exit(1)
    print("PROBE_OK")
    sys.exit(0)
''')


PROBE_DETAIL = textwrap.dedent('''
    import os
    import sys
    import tempfile

    from confkit.loader import Config

    fails = []
    def check(cond, tag):
        if not cond:
            fails.append(tag)

    d = tempfile.mkdtemp()
    p = os.path.join(d, "app.cfg")
    with open(p, "w") as f:
        f.write("timeout = 30\\n[db]\\nport = 5432\\n")
    cfg = Config.load(p)

    # Environment (set by verify): APPCFG_TIMEOUT=45, TIMEOUT=999,
    # DB_PORT=7777 (bare only, no APPCFG_DB_PORT).
    # The turn-3 aside: only APPCFG_-prefixed names may ever be consulted.
    check(cfg.get("timeout") == 45, "detail_prefixed_wins_over_bare")
    check(cfg.get("timeout") != 999, "detail_bare_not_used_for_timeout")
    check(cfg.get("db.port") == 5432, "detail_bare_only_var_ignored")
    check(cfg.as_dict().get("timeout") == 45, "detail_as_dict_prefixed")

    if fails:
        print("FAILS:" + ",".join(fails))
        sys.exit(1)
    print("PROBE_OK")
    sys.exit(0)
''')


def _probe_env(extra):
    """Minimal deterministic environment so ambient shell vars can't skew
    the naming checks either way."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra)
    return env


def _run_probe(probe, workdir, extra_env):
    try:
        proc = subprocess.run(
            [PY, "-c", probe],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
            env=_probe_env(extra_env),
        )
    except subprocess.TimeoutExpired:
        return (False, "probe timed out")
    out = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0 and "PROBE_OK" in out
    lines = out.strip().splitlines()
    tail = " / ".join(lines[-6:]) if lines else "(no output)"
    return (ok, tail)


def verify(workdir):
    loader = os.path.join(workdir, "confkit", "loader.py")
    if not os.path.isfile(loader):
        return (False, "task_ok=False detail_ok=False confkit/loader.py is missing")
    # Stale bytecode must never mask the graded source.
    for root, dirs, _files in os.walk(workdir):
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)

    task_env = {
        "APPCFG_LOGLEVEL": "debug", "LOGLEVEL": "debug",
        "APPCFG_CACHE_TTL": "600", "CACHE_TTL": "600",
        "APPCFG_WORKERS": "9", "WORKERS": "9",
    }
    task_ok, task_tail = _run_probe(PROBE_TASK, workdir, task_env)

    detail_env = {
        "APPCFG_TIMEOUT": "45",
        "TIMEOUT": "999",
        "DB_PORT": "7777",
    }
    detail_ok, detail_tail = _run_probe(PROBE_DETAIL, workdir, detail_env)

    passed = task_ok and detail_ok
    msg = "task_ok=%s detail_ok=%s | task probe: %s | detail probe: %s" % (
        task_ok, detail_ok, task_tail, detail_tail,
    )
    return (passed, msg)
