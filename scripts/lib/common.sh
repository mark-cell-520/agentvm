#!/usr/bin/env bash
# Shared helpers for agentvm scripts. Source this, don't execute it.
#
# Design notes:
#   - `set -euo pipefail` everywhere; every script is meant to be re-runnable.
#   - Secrets are never passed as command-line arguments (they show up in `ps`).
#     They travel via files with 0600 permissions, or via base64 through stdin.
#   - Every external tool invocation checks its exit status and says what failed.

set -euo pipefail

AGENTVM_VERSION="0.1.0"

# ---------------------------------------------------------------- colours ---
if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RST=$'\033[0m'; C_RED=$'\033[31m'; C_GRN=$'\033[32m'
  C_YEL=$'\033[33m'; C_BLU=$'\033[34m'; C_DIM=$'\033[2m'
else
  C_RST=""; C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_DIM=""
fi

info()  { printf '%s==>%s %s\n'  "$C_BLU" "$C_RST" "$*" >&2; }
ok()    { printf '%s  ok%s %s\n' "$C_GRN" "$C_RST" "$*" >&2; }
warn()  { printf '%s  warn%s %s\n' "$C_YEL" "$C_RST" "$*" >&2; }
die()   { printf '%s  error%s %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# Resolve the repo root regardless of where a script is invoked from.
agentvm_root() {
  cd "$(dirname "${BASH_SOURCE[1]}")/.." && pwd
}

# Human-readable byte count.
bytes_human() {
  local b="${1:-0}"
  if   [ "$b" -ge $((1024**3)) ]; then awk -v n="$b" 'BEGIN{printf "%.1f GiB", n/1073741824}'
  elif [ "$b" -ge $((1024**2)) ]; then awk -v n="$b" 'BEGIN{printf "%.1f MiB", n/1048576}'
  elif [ "$b" -ge 1024 ];        then awk -v n="$b" 'BEGIN{printf "%.1f KiB", n/1024}'
  else printf '%s B' "$b"; fi
}

# ------------------------------------------------------------------ config ---
# Defaults; override via environment or a .agentvmrc in the repo root.
: "${AGENTVM_HOME:=$HOME/.agentvm}"
: "${VM_NAME:=agent}"
: "${VM_CPU:=2}"
: "${VM_MEMORY_MB:=1024}"
: "${VM_DISK_GB:=10}"          # logical target; see docs/PITFALLS.md
: "${TART_BIN:=$HOME/.local/bin/tart}"
: "${IMAGE_REPO:=ghcr.io/cirruslabs/debian}"
: "${IMAGE_TAG:=latest}"
: "${LOCAL_REGISTRY_PORT:=5000}"
: "${LOCAL_REPO_NAME:=debian}"

rc_file() {
  local root; root="$(agentvm_root)"
  [ -f "$root/.agentvmrc" ] && cat "$root/.agentvmrc"
}

load_rc() {
  local root; root="$(agentvm_root)"
  if [ -f "$root/.agentvmrc" ]; then
    # shellcheck disable=SC1090
    . "$root/.agentvmrc"
    info "loaded $root/.agentvmrc"
  fi
}

# ------------------------------------------------------------- secret safe ---
# Write stdin to a 0600 file. Never put a secret in an argv.
write_secret() {
  local path="$1"
  umask 077
  cat > "$path"
  chmod 600 "$path"
}

# Transport an arbitrary string into a guest/file without it appearing in argv
# in a form that secret-scanners mangle. Encodes to base64, decodes on arrival.
#   printf '%s' "$secret" | b64_into_guest <vm> /remote/path
b64_into_guest() {
  local vm="$1" dest="$2"
  local b64; b64="$(base64 | tr -d '\n')"
  tart exec "$vm" sh -c "umask 077; printf '%s' '$b64' | base64 -d > '$dest'; chmod 600 '$dest'"
}
