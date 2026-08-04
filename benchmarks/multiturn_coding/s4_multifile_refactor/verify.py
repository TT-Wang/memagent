"""Independent behavioral oracle for the multi-file refactor-cascade scenario.

This file is NOT given to the benchmarked agent to edit. It exercises the
agent's final ``pipeline`` package in a fresh subprocess: the Filter protocol
(turn 1), decorator registration (turn 2), the process->run rename (turn 3),
the filters/ subpackage layout (turn 4), config-driven CLI defaults (turn 5),
the tokens category (turn 6), the idempotent-registration fix (turn 7), and
the turn-8 removal of every deprecated path (old names must be truly GONE).

Run via subprocess so a crashing/looping import cannot take down the parent
and so import caching never masks a broken module.
"""
import os
import subprocess
import sys
import textwrap

PY = sys.executable


# The probe runs inside the agent's workdir (cwd=workdir), so ``import
# pipeline`` resolves to the agent's package and the CLI subprocesses it
# spawns inherit that cwd. Each check prints a tag on failure.
PROBE = textwrap.dedent(r'''
    import json
    import os
    import subprocess
    import sys
    import warnings

    fails = []
    def check(cond, tag):
        if not cond:
            fails.append(tag)

    # ---- turn 8: importing the package must be DeprecationWarning-clean ----
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import pipeline
        import pipeline.filters
        from pipeline.core import Pipeline
        from pipeline.registry import available, get_filter, register
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    check(not dep, "t8_import_warning_clean")

    from pipeline.base import Filter
    check(isinstance(Filter, type), "t1_filter_base_exists")

    ALL_NAMES = ["collapse_spaces", "dedupe_words", "lowercase",
                 "sort_words", "strip_edges", "uppercase"]

    # ---- turns 1+2: registry hands out conforming Filter instances --------
    for n in ALL_NAMES:
        try:
            f = get_filter(n)
        except Exception:
            fails.append("t2_registered_" + n)
            continue
        check(isinstance(f, Filter), "t1_instance_" + n)
        check(getattr(f, "name", None) == n, "t1_name_attr_" + n)
        check(callable(getattr(f, "apply", None)), "t1_apply_" + n)

    # ---- exact filter semantics (seed contract + turn 6) ------------------
    check(get_filter("strip_edges").apply("  a b  ") == "a b", "sem_strip_edges")
    check(get_filter("lowercase").apply("AbC") == "abc", "sem_lowercase")
    check(get_filter("uppercase").apply("abC") == "ABC", "sem_uppercase")
    check(get_filter("collapse_spaces").apply(" a\t\tb \n c ") == "a b c",
          "sem_collapse_spaces")
    check(get_filter("dedupe_words").apply("x y x z y") == "x y z", "t6_dedupe")
    check(get_filter("sort_words").apply("pear apple pear banana")
          == "apple banana pear pear", "t6_sort")

    # ---- turn 4 (+6): category layout is where the classes live -----------
    def mod(n):
        return type(get_filter(n)).__module__
    check(mod("lowercase") == "pipeline.filters.text", "t4_text_lowercase")
    check(mod("uppercase") == "pipeline.filters.text", "t4_text_uppercase")
    check(mod("strip_edges") == "pipeline.filters.spacing", "t4_spacing_strip")
    check(mod("collapse_spaces") == "pipeline.filters.spacing",
          "t4_spacing_collapse")
    check(mod("dedupe_words") == "pipeline.filters.tokens", "t6_tokens_dedupe")
    check(mod("sort_words") == "pipeline.filters.tokens", "t6_tokens_sort")

    # ---- turn 3 rename + turn 8 shim removal ------------------------------
    p = Pipeline([get_filter("strip_edges"), get_filter("lowercase")])
    check(p.run("  MiXeD  ") == "mixed", "t3_run_works")
    check(not hasattr(p, "process"), "t8_process_gone")

    # ---- turn 5: from_names -----------------------------------------------
    p2 = Pipeline.from_names(["collapse_spaces", "uppercase"])
    check(p2.run(" a  b ") == "A B", "t5_from_names")
    raised = False
    try:
        Pipeline.from_names(["zz_unknown"])
    except KeyError:
        raised = True
    check(raised, "t5_from_names_unknown")

    # ---- turn 2 available() + turn 7 no-duplicates ------------------------
    av = available()
    check(av == sorted(av), "t2_available_sorted")
    check(len(av) == len(set(av)), "t7_available_no_dups")
    for n in ALL_NAMES:
        check(n in av, "t2_available_has_" + n)

    # ---- turn 8: manual dict gone, fallback gone --------------------------
    import pipeline.registry as reg
    check(not hasattr(reg, "FILTERS"), "t8_manual_dict_gone")
    raised, msg = False, ""
    try:
        get_filter("zz_definitely_missing")
    except KeyError as e:
        raised, msg = True, str(e)
    check(raised, "t8_unknown_keyerror")
    check("lowercase" in msg, "t8_keyerror_lists_available")

    # ---- turn 8: legacy flat re-exports truly gone ------------------------
    gone = True
    try:
        from pipeline.filters import lowercase as _legacy  # noqa: F401
        gone = False
    except ImportError:
        pass
    check(gone, "t8_flat_reexport_gone")
    for old in ["strip_edges", "lowercase", "uppercase", "collapse_spaces"]:
        check(not hasattr(pipeline.filters, old), "t8_no_flat_attr_" + old)
    check(not os.path.isfile(os.path.join("pipeline", "filters.py")),
          "t4_flat_module_deleted")

    # ---- turn 7: idempotent registration ----------------------------------
    class ProbeA(Filter):
        name = "zz_probe_dummy"
        def apply(self, text):
            return text
    register(ProbeA)
    ok = True
    try:
        register(ProbeA)          # same class again: harmless no-op
    except Exception:
        ok = False
    check(ok, "t7_same_class_idempotent")

    class ProbeB(Filter):
        name = "zz_probe_dummy"   # different class, same name
        def apply(self, text):
            return text + "!"
    raised = False
    try:
        register(ProbeB)
    except ValueError:
        raised = True
    check(raised, "t7_conflict_valueerror")
    check(available().count("zz_probe_dummy") == 1, "t7_no_dup_after_rereg")

    # ---- turn 5: CLI reads the shipped config for its default chain -------
    try:
        with open("pipeline.json") as fh:
            cfg = json.load(fh)
        check(cfg.get("default_chain")
              == ["strip_edges", "collapse_spaces", "lowercase"],
              "t5_config_contents")
    except Exception:
        fails.append("t5_config_missing")

    def cli(*args, **kw):
        return subprocess.run([sys.executable, "-m", "pipeline.cli", *args],
                              capture_output=True, text=True, timeout=60, **kw)

    r = cli("  Hello   WORLD  ")
    check(r.returncode == 0, "t5_cli_default_rc")
    check(r.stdout.rstrip("\n") == "hello world", "t5_cli_default_output")

    # ---- turn 6 filters through the CLI, --filters overrides config -------
    r = cli("--filters", "sort_words,uppercase", "b a")
    check(r.returncode == 0, "t6_cli_filters_rc")
    check(r.stdout.rstrip("\n") == "A B", "t6_cli_filters_output")

    # ---- turn 5: explicit --config path -----------------------------------
    with open("_probe_cfg.json", "w") as fh:
        json.dump({"default_chain": ["dedupe_words"]}, fh)
    r = cli("--config", "_probe_cfg.json", "a a b c b")
    check(r.returncode == 0, "t5_cli_custom_config_rc")
    check(r.stdout.rstrip("\n") == "a b c", "t5_cli_custom_config_output")
    os.remove("_probe_cfg.json")

    # ---- turn 5: no --filters and no config file -> clean nonzero exit ----
    os.makedirs("_probe_empty", exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, "-m", "pipeline.cli", "x"],
                       capture_output=True, text=True, timeout=60,
                       cwd="_probe_empty", env=env)
    check(r.returncode != 0, "t5_cli_no_config_errors")
    os.rmdir("_probe_empty")

    # ---- turn 8: normal CLI use is warning-clean under -W error -----------
    r = subprocess.run([sys.executable, "-W", "error::DeprecationWarning",
                        "-m", "pipeline.cli", "abc"],
                       capture_output=True, text=True, timeout=60)
    check(r.returncode == 0, "t8_cli_warning_clean")

    # ---- turn 7: internal paths guaranteed clean under -W error -----------
    r = subprocess.run([sys.executable, "-W", "error::DeprecationWarning",
                        "-m", "pipeline.cli", "--filters",
                        "strip_edges,uppercase", "  ok  "],
                       capture_output=True, text=True, timeout=60)
    check(r.returncode == 0 and r.stdout.rstrip("\n") == "OK",
          "t7_cli_filters_warning_clean")
    r = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c",
         "from pipeline.core import Pipeline; "
         "print(Pipeline.from_names(['lowercase']).run('AbC'))"],
        capture_output=True, text=True, timeout=60)
    check(r.returncode == 0 and r.stdout.rstrip("\n") == "abc",
          "t7_from_names_warning_clean")

    if fails:
        print("FAILS:" + ",".join(fails))
        sys.exit(1)
    print("ALL_OK")
    sys.exit(0)
''')


def verify(workdir):
    pkg = os.path.join(workdir, "pipeline")
    if not os.path.isdir(pkg):
        return (False, "pipeline/ package is missing")
    if not os.path.isfile(os.path.join(pkg, "base.py")):
        return (False, "pipeline/base.py is missing (Filter protocol)")
    if not os.path.isdir(os.path.join(pkg, "filters")):
        return (False, "pipeline/filters/ subpackage is missing")
    for cat in ("text.py", "spacing.py", "tokens.py"):
        if not os.path.isfile(os.path.join(pkg, "filters", cat)):
            return (False, f"pipeline/filters/{cat} is missing")
    try:
        proc = subprocess.run(
            [PY, "-c", PROBE],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return (False, "probe timed out (possible infinite loop in package)")
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0 and "ALL_OK" in out:
        return (True, "all cumulative + regression checks passed")
    detail = out.strip().splitlines()
    tail = "\n".join(detail[-15:]) if detail else "(no output)"
    return (False, "probe failed:\n" + tail)
