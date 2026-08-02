#!/usr/bin/env python3
"""check_windows_footguns — grep-lint that kills the Windows-breakage bug CLASS (ported from
Hermes' scripts/check-windows-footguns.py, trimmed to sliceagent's actual surfaces).

Rules recursively cover the real product and test trees (suppress an intentional site with
`# windows-footgun: ok`):
  1. text-mode builtin open() without encoding=  (Windows defaults to cp1252 — mojibake)
  2. pathlib read_text()/write_text() without encoding=
  3. os.kill(pid, 0) existence-probe  (on Windows it CTRL_C's or kills the target — bpo-14484)
  4. bare os.setsid / os.killpg / os.getpgid / start_new_session outside the seam
  5. signal.SIGKILL by attribute outside the seam  (doesn't exist on win32)
  6. subprocess shell=True outside the seam  (agent commands must go through platform_compat.sh())
  7. unguarded top-level `import fcntl|pty|termios` outside the allowed guarded files

Exemptions: platform_compat.py IS the seam (all branches live there). terminal.py is POSIX-only BY
CONSTRUCTION (SessionManager.open() refuses on Windows before any of its killpg/PTY code can run) —
exempt from 4/5/6 until the Phase-2 pywinpty bridge. Docstrings are excluded via ast.

Exit 0 = clean, 1 = violations (printed one per line).
"""
from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOTS = (
    REPO_ROOT / "packages" / "sliceagent-core" / "src",
    REPO_ROOT / "packages" / "sliceagent-cli" / "src",
    REPO_ROOT / "src",
    REPO_ROOT / "tests",
)
SEAM = "platform_compat.py"
POSIX_ONLY = {"terminal.py", SEAM}            # gated at entry; Phase 2 adds the win bridge
GUARDED_UNIX_IMPORTS = {"terminal.py", SEAM}  # fcntl/pty imports sit in try/except there

_BINARY_MODE = re.compile(r"['\"][rwax+]*b[rwax+]*['\"]")
_BUILTIN_OPEN = re.compile(r"(?<![\w.])open\(")           # builtin only — not .open() methods
_RT_WT = re.compile(r"\.(read_text|write_text)\(")

RULES: list[tuple[str, re.Pattern, set[str]]] = [
    ("os.kill(pid, 0) existence probe (bpo-14484: kills the target on Windows)",
     re.compile(r"os\.kill\([^,\n]+,\s*0\s*\)"), set()),
    ("bare setsid/killpg/getpgid/start_new_session (win32: no-op or AttributeError)",
     re.compile(r"os\.(setsid|killpg|getpgid)\b|start_new_session\s*="), POSIX_ONLY),
    ("signal.SIGKILL attribute (missing on win32 — use platform_compat.SIG_KILL)",
     re.compile(r"signal\.SIGKILL\b"), POSIX_ONLY),
    ("shell=True (agent commands must route through platform_compat.sh())",
     re.compile(r"shell\s*=\s*True"), POSIX_ONLY),
    ("unguarded top-level import of fcntl/pty/termios",
     re.compile(r"^import (fcntl|pty|termios)\b|^from (fcntl|pty|termios) import"), GUARDED_UNIX_IMPORTS),
]

# Individually-reviewed sites the line rules can't see into:
ALLOW = {
    # tools.py holds the sandbox-toolkit TEMPLATE STRING for code-as-action workers; its inline
    # `shell=True` executes under the worker sandbox on the same host — tracked for Phase 2.
    ("tools.py", "shell=True (agent commands must route through platform_compat.sh())"),
}


def _docstring_lines(tree: ast.AST) -> set[int]:
    """Line numbers covered by docstrings (module/class/function) — excluded from linting."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                lines.update(range(body[0].value.lineno, (body[0].value.end_lineno or body[0].value.lineno) + 1))
    return lines


def _code_only_lines(text: str) -> list[str]:
    """Blank strings/comments so fixtures containing ``open(...)`` are not mistaken for calls."""
    lines = text.splitlines()
    code = list(lines)
    noncode_types = {tokenize.STRING, tokenize.COMMENT}
    noncode_types.update(
        value for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
        if (value := getattr(tokenize, name, None)) is not None
    )
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type not in noncode_types:
                continue
            (start_row, start_col), (end_row, end_col) = token.start, token.end
            for row in range(start_row, end_row + 1):
                line = code[row - 1]
                begin = start_col if row == start_row else 0
                end = end_col if row == end_row else len(line)
                code[row - 1] = line[:begin] + " " * max(0, end - begin) + line[end:]
    except (IndentationError, tokenize.TokenError):
        return lines
    return code


def _check_encoding_rules(
    fname: str,
    label: str,
    i: int,
    code: str,
    source: str,
    bad: list[str],
) -> None:
    stripped = source.strip()
    if _BUILTIN_OPEN.search(code) and not code.strip().startswith("def "):
        if "encoding=" not in code and not _BINARY_MODE.search(source) and "open()" not in source:
            bad.append(f"{label}:{i}: [open() without encoding= (cp1252 on Windows)] {stripped[:100]}")
    if _RT_WT.search(code) and "encoding=" not in code:
        # sliceagent's own tool-host read_text() methods are not pathlib — skip those receivers
        if not re.search(r"(self|tools|inner|host|h)\.(read_text|write_text)\(", code):
            bad.append(f"{label}:{i}: [read_text/write_text without encoding=] {stripped[:100]}")


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def _python_files(roots: list[Path] | tuple[Path, ...]) -> tuple[list[Path], list[str]]:
    files: set[Path] = set()
    errors: list[str] = []
    for root in roots:
        resolved = root.resolve()
        if not resolved.exists():
            errors.append(f"{resolved}: [scan root missing]")
        elif resolved.is_file():
            if resolved.suffix == ".py":
                files.add(resolved)
        else:
            files.update(resolved.rglob("*.py"))
    return sorted(files), errors


def violations(roots: list[Path] | tuple[Path, ...]) -> tuple[list[str], int]:
    bad: list[str] = []
    files, root_errors = _python_files(roots)
    bad.extend(root_errors)
    for f in files:
        label = _display_path(f)
        text = f.read_text(encoding="utf-8")
        try:
            doc_lines = _docstring_lines(ast.parse(text))
        except SyntaxError:
            doc_lines = set()
        source_lines = text.splitlines()
        code_lines = _code_only_lines(text)
        for i, (source, code) in enumerate(zip(source_lines, code_lines), 1):
            if i in doc_lines or "windows-footgun: ok" in source:
                continue
            stripped = source.strip()
            if not code.strip():
                continue
            _check_encoding_rules(f.name, label, i, code, source, bad)
            for rule_label, rx, exempt in RULES:
                if f.name in exempt or (f.name, rule_label) in ALLOW:
                    continue
                if rx.search(code):
                    bad.append(f"{_display_path(f)}:{i}: [{rule_label}] {stripped[:100]}")
    return bad, len(files)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    roots = [Path(item) for item in args] if args else list(DEFAULT_ROOTS)
    bad, scanned = violations(roots)
    if bad:
        print(f"{len(bad)} Windows footgun(s) across {scanned} Python files:")
        for b in bad:
            print(" ", b)
        return 1
    if scanned == 0:
        print("windows-footguns: no Python files scanned")
        return 1
    print(f"windows-footguns: clean ({scanned} Python files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
