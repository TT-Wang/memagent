"""Security screen for user-configured MCP server entries.

MCP stdio transports intentionally allow ARBITRARY local commands — a configured server is remote-code-
execution by design. We don't try to sandbox that. We refuse two high-signal ABUSE SHAPES that a real MCP
server never has, so a hand-edited or pre-planted config.toml is caught BEFORE `connect_mcp_servers` spawns it:

  1. a shell interpreter whose inline script performs NETWORK EGRESS (curl/wget/nc/socat, /dev/tcp,
     PowerShell web clients) — the exfiltration shape;
  2. a shell interpreter whose inline script writes to an OS PERSISTENCE surface (SSH keys, PAM, sudoers,
     cron, init units, shell rc files) — the backdoor shape.

This is intentionally NOT a whitelist: legitimate local MCPs using npx / uvx / python / a custom binary all
pass. General + task-agnostic — only the shell-interpreter-plus-egress/persistence combination is refused.
"""
from __future__ import annotations

import base64
import os
import re
import shlex
import shutil

_SHELL_INTERPRETERS = frozenset({
    "bash", "sh", "zsh", "dash", "fish", "ksh",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
})

_EGRESS = re.compile(
    r"(?<![\w.-])(?:curl|wget|nc|ncat|socat)(?![\w.-])"
    r"|/dev/tcp/"
    r"|\bInvoke-WebRequest\b|\bInvoke-RestMethod\b|\bSystem\.Net\.WebClient\b",
    re.IGNORECASE,
)

_PERSISTENCE = re.compile(
    r"authorized_keys|\.ssh/|/etc/ssh\b"
    r"|/etc/pam\.d\b|pam_[\w-]+\.so|/etc/sudoers"
    r"|/etc/cron|crontab\b|/etc/rc\.local|/etc/systemd"
    r"|\.bashrc\b|\.bash_profile\b|\.profile\b|\.zshrc\b"
    # verb-shaped persistence surfaces the path shapes above cannot see (no file path is named):
    r"|\bsystemctl\s+(?:enable|mask|preset)\b"
    r"|\bschtasks\s+(?:/create\b|\/create\b)"
    r"|\blaunchctl\s+(?:load|bootstrap)\b"
    r"|\breg\s+add\b|\bsc\.exe\s+create\b",
    re.IGNORECASE,
)

# Base64 blobs long enough to carry a real command (>= 16 chars, canonical alphabet, optional padding).
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def _deobfuscated(full: str) -> list[str]:
    """Return extra scan texts that strip the two cheap obfuscations from an inline shell script.

    ``wge"t"`` (quote-split executable) and ``echo <base64> | base64 -d | sh`` (encoded payload) both
    defeat a raw substring scan of the original command. Scanning the quote-stripped text and every
    decodable base64 blob closes both without a real shell parser; the screen only ever refuses a
    SHELL-interpreter entry on these shapes, so the residual false-positive cost is an over-refusal of
    a hand-typed shell MCP that quotes a keyword — an acceptable trade against RCE-by-design entries.
    """
    texts = [full.replace('"', "").replace("'", "")]
    for blob in _BASE64_BLOB.findall(full):
        padded = blob + "=" * (-len(blob) % 4)
        try:
            decoded = base64.b64decode(padded, validate=True).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — a non-decodable blob is not an encoded payload
            continue
        if decoded.strip():
            texts.append(decoded)
    return texts


def _script(args) -> str:
    if args is None:
        return ""
    if isinstance(args, (list, tuple)):
        return " ".join(str(a) for a in args)
    return str(args)


def validate_mcp_server_entry(name: str, conf) -> list[str]:
    """Return a list of security objections to spawning this MCP entry (empty list = clean).

    Only a shell interpreter (bash/sh/pwsh/…) carrying an inline script with network-egress OR
    OS-persistence content is refused; everything else (npx/uvx/python/custom binaries) passes.
    """
    if not isinstance(conf, dict):
        return []
    # Tokenize command + args together and check whether ANY token is a shell interpreter — so a wrapped
    # interpreter (env bash -c …, /usr/bin/timeout 5 sh -c …, a full path) is screened, not just a bare
    # `command: bash`. Then scan the FULL command+args text for the egress/persistence shapes.
    full = (str(conf.get("command") or "") + " " + _script(conf.get("args"))).strip()
    if not full:
        return []
    try:
        tokens = shlex.split(full, posix=(os.name != "nt"))
    except ValueError:
        tokens = full.split()
    def _is_shell(token: str) -> bool:
        raw = os.path.expandvars(os.path.expanduser(str(token)))
        names = {os.path.basename(raw).lower()}
        resolved = shutil.which(raw) if not os.path.dirname(raw) else raw
        if resolved:
            names.add(os.path.basename(os.path.realpath(resolved)).lower())
        return bool(names & _SHELL_INTERPRETERS)

    if not any(_is_shell(t) for t in tokens):
        return []
    issues: list[str] = []
    env = conf.get("env")
    env_text = ""
    if isinstance(env, dict):
        env_text = " ".join(f"{key}={value}" for key, value in env.items())
    effective = (full + " " + env_text).strip()
    scan_texts = [effective] + _deobfuscated(effective)
    if any(_EGRESS.search(text) for text in scan_texts):
        issues.append(f"MCP server '{name}': a shell interpreter with network-egress arguments "
                      "(exfiltration shape — not a real MCP server)")
    if any(_PERSISTENCE.search(text) for text in scan_texts):
        issues.append(f"MCP server '{name}': a shell interpreter writing to an OS persistence surface "
                      "(SSH keys / PAM / sudoers / cron / shell rc — backdoor shape, not a real MCP server)")
    return issues
