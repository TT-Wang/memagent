#!/bin/sh
# sliceagent installer — one command, isolated install via uv.
#
#   curl -fsSL https://raw.githubusercontent.com/TT-Wang/sliceagent/main/install.sh | sh
#
# It installs `uv` (a fast Python tool manager) if missing, then installs sliceagent into its own
# isolated environment and puts the `sliceagent` command on your PATH. Re-running upgrades in place.
#   Uninstall:  sh install.sh --uninstall
#
# As with any `curl … | sh`, you are welcome to read this script first — it does exactly the above.
set -eu

PKG="sliceagent[tui]"          # the PUBLISHED PyPI release; [tui] = rich terminal UI. This installer
                               # tracks PyPI stable (not git main) — one canonical, reproducible path.

info() { printf '\033[36m▸ %s\033[0m\n' "$1"; }
warn() { printf '\033[33m! %s\033[0m\n' "$1" >&2; }
err()  { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; }

# Do not discover installer executables from the repository (for example PATH=.:...). These are the
# standard user/system package-manager locations used by uv, Homebrew, Nix, macOS, and Linux.
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.nix-profile/bin:/opt/homebrew/bin:/opt/local/bin:/usr/local/bin:/usr/bin:/bin:/run/current-system/sw/bin"
export PATH

if [ "${1:-}" = "--uninstall" ]; then
  if command -v uv >/dev/null 2>&1; then uv tool uninstall sliceagent 2>/dev/null || true; fi
  info "sliceagent uninstalled."
  exit 0
fi

# 1. ensure uv
if ! command -v uv >/dev/null 2>&1; then
  UV_VERSION="0.11.26"
  UV_SHA256="92fa9085d24c214bb4445cc1da8c15ca9cca8cffb34726240fa08c5302e94ccc"
  UV_URL="https://astral.sh/uv/${UV_VERSION}/install.sh"
  UV_TMP="$(mktemp -d)"
  trap 'rm -rf "$UV_TMP"' EXIT HUP INT TERM
  info "Installing uv ${UV_VERSION} (SHA256 verified) …"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$UV_URL" -o "$UV_TMP/install.sh"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$UV_TMP/install.sh" "$UV_URL"
  else
    err "Need curl or wget to bootstrap uv. Install uv manually, then re-run."
    exit 1
  fi
  if command -v shasum >/dev/null 2>&1; then UV_ACTUAL="$(shasum -a 256 "$UV_TMP/install.sh")"
  elif command -v sha256sum >/dev/null 2>&1; then UV_ACTUAL="$(sha256sum "$UV_TMP/install.sh")"
  else UV_ACTUAL=""; fi
  UV_ACTUAL="${UV_ACTUAL%% *}"
  if [ "$UV_ACTUAL" != "$UV_SHA256" ]; then
    err "uv installer SHA256 mismatch (got ${UV_ACTUAL:-unverifiable}); refusing to execute it."
    exit 1
  fi
  sh "$UV_TMP/install.sh"
  rm -rf "$UV_TMP"
  trap - EXIT HUP INT TERM
  # uv lands in ~/.local/bin (or ~/.cargo/bin); make it visible for THIS shell
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  err "uv still not on PATH. Open a new terminal and re-run this installer."
  exit 1
fi
UV_BIN="$(command -v uv)"

# Run package resolution without ambient uv/pip overrides or exported provider tokens. Keep only explicit
# custom tool locations; this installer always resolves SliceAgent itself from the public PyPI index below.
run_uv_clean() (
  for _name in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'); do
    case "$_name" in
      UV_TOOL_DIR|UV_TOOL_BIN_DIR) ;;
      UV_*|PIP_*|PYTHONPATH|PYTHONHOME|VIRTUAL_ENV|AWS_SECRET_ACCESS_KEY|*_API_KEY|*_TOKEN|*_KEY|*_SECRET|*_PASSWORD|*_PASSWD|*_PWD|*_PASSPHRASE|*_CREDENTIAL|*_AUTH|*_ACCESS_KEY|*_WEBHOOK|*_DSN|*_URL)
        unset "$_name"
        ;;
    esac
  done
  "$UV_BIN" "$@"
)

# 2. install (or upgrade) sliceagent as an isolated uv tool
# --python 3.12: don't inherit whatever python happens to be on PATH (conda base = 3.10,
# Ubuntu 22.04 = 3.10, macOS system = 3.9 — all below the >=3.11 floor). uv fetches a managed
# CPython 3.12 automatically when none is installed, so the installer has zero prerequisites.
info "Installing sliceagent …"
run_uv_clean tool install --force --upgrade --python 3.12 --no-config \
  --default-index https://pypi.org/simple "$PKG"

# 3. make sure uv's tool bin is on PATH for future shells
run_uv_clean tool update-shell >/dev/null 2>&1 || warn "Could not auto-update PATH — you may need to add uv's tool bin (see 'uv tool dir') to your PATH."

# 4. ripgrep powers the code index — install it too (brew when available, else a ~2 MB static
# binary from GitHub into uv's tool bin: no sudo, isolated, removable with the rest).
if ! command -v rg >/dev/null 2>&1; then
  info "Installing ripgrep (code search) …"
  if command -v brew >/dev/null 2>&1; then
    brew install ripgrep >/dev/null 2>&1 || true
  fi
fi
if ! command -v rg >/dev/null 2>&1; then
  RG_VER="15.1.0"   # same pinned version the Windows installer verifies (install.ps1)
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)              RG_TARGET="aarch64-apple-darwin";              RG_SHA256="378e973289176ca0c6054054ee7f631a065874a352bf43f0fa60ef079b6ba715" ;;
    Darwin-x86_64)             RG_TARGET="x86_64-apple-darwin";               RG_SHA256="64811cb24e77cac3057d6c40b63ac9becf9082eedd54ca411b475b755d334882" ;;
    Linux-x86_64)              RG_TARGET="x86_64-unknown-linux-musl";         RG_SHA256="1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599" ;;
    Linux-aarch64|Linux-arm64) RG_TARGET="aarch64-unknown-linux-gnu";         RG_SHA256="2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e" ;;
    *)                         RG_TARGET=""; RG_SHA256="" ;;
  esac
  BIN_DIR="$(run_uv_clean tool dir --bin 2>/dev/null || true)"
  [ -n "$BIN_DIR" ] || BIN_DIR="$HOME/.local/bin"
  if [ -n "$RG_TARGET" ]; then
    RG_TMP="$(mktemp -d)"
    RG_URL="https://github.com/BurntSushi/ripgrep/releases/download/${RG_VER}/ripgrep-${RG_VER}-${RG_TARGET}.tar.gz"
    if { command -v curl >/dev/null 2>&1 && curl -fsSL "$RG_URL" -o "$RG_TMP/rg.tgz"; } \
       || { command -v wget >/dev/null 2>&1 && wget -qO "$RG_TMP/rg.tgz" "$RG_URL"; }; then
      # SHA256-verify like the Windows installer does (supply-chain pinning; the same digests are
      # published by GitHub for every release asset). A mismatch refuses the binary — a weaker code
      # search is safer than an unverified download into the user's bin dir.
      if command -v shasum >/dev/null 2>&1; then _actual="$(shasum -a 256 "$RG_TMP/rg.tgz")"
      elif command -v sha256sum >/dev/null 2>&1; then _actual="$(sha256sum "$RG_TMP/rg.tgz")"
      else _actual=""; fi
      _actual="${_actual%% *}"
      if [ "$_actual" != "$RG_SHA256" ]; then
        warn "ripgrep SHA256 mismatch (got ${_actual:-unverifiable}) — refusing the download; code search falls back."
      else
        mkdir -p "$BIN_DIR" \
          && tar -xzf "$RG_TMP/rg.tgz" -C "$RG_TMP" \
          && mv "$RG_TMP/ripgrep-${RG_VER}-${RG_TARGET}/rg" "$BIN_DIR/rg" \
          && chmod +x "$BIN_DIR/rg" \
          && info "ripgrep installed to $BIN_DIR/rg (SHA256 verified)" \
          || warn "Could not unpack ripgrep — sliceagent still works, code search is just weaker without it."
      fi
    else
      warn "Could not download ripgrep — sliceagent still works, code search is just weaker without it."
    fi
    rm -rf "$RG_TMP"
  else
    warn "No prebuilt ripgrep for this platform — sliceagent works without it (weaker code search)."
  fi
fi

cat <<'EOF'

  ✓ sliceagent installed.

  Next — just one command:
    sliceagent          # first run walks you through setup (provider, API key), then you're chatting

  Update later:
    sliceagent update

  If 'sliceagent' isn't found, open a NEW terminal (PATH was just updated).
  Docs: https://github.com/TT-Wang/sliceagent
EOF
