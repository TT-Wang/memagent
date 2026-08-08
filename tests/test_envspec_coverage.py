"""envspec coverage guard — every AGENT_*/LLM_*/SLICEAGENT_* env var READ by the code must be
documented in envspec.REGISTRY (envspec.py's own docstring promises this test).

Without the guard, a knob can silently exist in code and nowhere in `sliceagent config --list`,
README's table, or docs/CONFIGURATION.md — exactly how the retired AGENT_EXPLORER_REASONING /
AGENT_EXPLORER_NAV_STEPS knobs lingered in .env.example after their code died (2026-08-08 review).
The scan matches the two read spellings in use (os.environ.get / os.getenv with a quoted literal);
dynamic names (f-strings, variables) are not read-anywhere knobs by definition.

Run: python tests/test_envspec_coverage.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for entry in ("src", "packages/sliceagent-core/src", "packages/sliceagent-cli/src"):
    sys.path.insert(0, os.path.join(ROOT, entry))

from sliceagent_cli.envspec import BY_NAME  # noqa: E402

CODE_ROOTS = (
    os.path.join(ROOT, "src"),
    os.path.join(ROOT, "packages", "sliceagent-core", "src"),
    os.path.join(ROOT, "packages", "sliceagent-cli", "src"),
)

# os.environ.get("VAR", …) / os.getenv("VAR", …) with a QUOTED LITERAL name.
_READ = re.compile(
    r'(?:os\.environ\.get|os\.getenv|environ\.get)\s*\(\s*["\']'
    r"(AGENT_[A-Z0-9_]+|LLM_[A-Z0-9_]+|SLICEAGENT_[A-Z0-9_]+)"
)


def find_reads() -> dict[str, str]:
    reads: dict[str, str] = {}
    for root in CODE_ROOTS:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                for lineno, line in enumerate(open(path, encoding="utf-8", errors="replace"), 1):
                    for match in _READ.finditer(line):
                        reads.setdefault(match.group(1), f"{path}:{lineno}")
    return reads


def main() -> int:
    reads = find_reads()
    missing = sorted(name for name in reads if name not in BY_NAME)
    if missing:
        for name in missing:
            print(f"UNDOCUMENTED ENV: {name} read at {reads[name]} but absent from envspec.REGISTRY")
        print(f"{len(missing)} env var(s) read by the code are missing from envspec.REGISTRY — "
              "register them (or delete the read).")
        return 1
    print(f"PASS envspec coverage: all {len(reads)} AGENT_*/LLM_*/SLICEAGENT_* vars read by the "
          "code are documented in envspec.REGISTRY")
    print(f"\n{len(reads)}/{len(reads)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
