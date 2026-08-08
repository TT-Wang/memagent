#!/usr/bin/env bash
# Run the offline test suite. Each tests/test_*.py is a standalone script with its own main() (no pytest),
# so this wrapper runs them all, tallies pass/fail, prints the tail of any failure, and EXITS NON-ZERO if
# anything fails — giving CI (and a local `bash scripts/run_tests.sh`) a single real signal.
set -u
cd "$(dirname "$0")/.." || exit 2

PY="${PYTHON:-.venv/bin/python}"
command -v "$PY" >/dev/null 2>&1 || PY="python3"   # CI installs the package, so a plain python3 works too
export PYTHONPATH="packages/sliceagent-core/src:packages/sliceagent-cli/src:src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUTF8=1   # Windows console defaults to cp1252; test output contains UTF-8 (no-op on POSIX)

# Registration guard FIRST: a test defined below its file's runner block never executes, so the
# suite would report green in both directions while covering nothing (the U2a/c dead-check).
"$PY" scripts/check_test_registration.py || exit 1

shopt -s nullglob
test_files=(tests/test_*.py packages/*/tests/test_*.py)
if [ "${#test_files[@]}" -eq 0 ]; then
  echo "suite: no test files discovered" >&2
  exit 2
fi

pass=0; skip=0; fail=0; failed=""; skipped=""
log="$(mktemp)"
for t in "${test_files[@]}"; do
  # A file with no __main__ block defines its tests and exits 0 without running ANY of them, so the
  # wrapper counted it green while it verified nothing (18 of 165 files were silently inert). Those are
  # pytest-style; hand them to pytest so every file in tests/ actually executes.
  if grep -q "if __name__" "$t"; then
    runner=("$PY" "$t")
  else
    runner=("$PY" -m pytest -q "$t")
  fi
  if "${runner[@]}" >"$log" 2>&1; then
    # The suite's skip convention is `print("SKIP …"); sys.exit(0)`: a whole file can legitimately
    # abstain (missing optional `tui` extra, no PTY, win32). Count those SEPARATELY — SKIP is not
    # PASS, and the tally must not silently hide abstaining coverage (2026-08-08 review M10).
    if grep -q "^SKIP" "$log"; then
      skip=$((skip + 1)); skipped="$skipped $t"
    else
      pass=$((pass + 1))
    fi
  else
    fail=$((fail + 1)); failed="$failed $t"
    echo "── FAIL: $t ─────────────────────────────"
    # the FAIL/Traceback lines first (a chatty file scrolls them out of a blind tail), then the tail
    grep -E "^FAIL |Traceback|^[A-Za-z]*Error" "$log" | head -15
    tail -12 "$log"
  fi
done
rm -f "$log"

echo "────────────────────────────────────────"
echo "suite: ${pass} passed, ${skip} skipped, ${fail} failed${failed:+  (${failed} )}${skipped:+  [skipped:${skipped} ]}"
[ "$fail" -eq 0 ]
