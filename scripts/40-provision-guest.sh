#!/usr/bin/env bash
# agentvm-provision-guest — run INSIDE the guest to make it usable.
#
# Copy in with:  tart exec <vm> sh -s < scripts/40-provision-guest.sh
# or push it and run it: see `make provision`.
#
# What this fixes, and why each step exists:
#
#  * apt mirror: the default deb.debian.org CDN is unusably slow from some
#    networks (measured: 38 KB/s). A regional mirror is 300x faster. Pointing
#    /etc/apt/mirrors/*.list at one turns a 2.5 hour build-essential download
#    into ~20 seconds.
#
#  * build-essential + python3-dev + libffi-dev: any Python package with a C
#    extension fails to build without them, and the failure message usually
#    blames the package rather than the missing compiler.

set -euo pipefail

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

say() { printf '==> %s\n' "$*" >&2; }

say "guest: $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -sr)"

# ---------------------------------------------------------------- apt mirror --
MIRROR="${AGENTVM_APT_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/debian/}"
SEC_MIRROR="${AGENTVM_APT_SEC_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/debian-security/}"

if ls /etc/apt/mirrors/*.list >/dev/null 2>&1; then
  say "switching apt mirror to $MIRROR"
  for f in /etc/apt/mirrors/*.list; do
    case "$f" in
      *security*) $SUDO cp "$f" "$f.bak"; printf '%s\n' "$SEC_MIRROR" | $SUDO tee "$f" >/dev/null ;;
      *)          $SUDO cp "$f" "$f.bak"; printf '%s\n' "$MIRROR"    | $SUDO tee "$f" >/dev/null ;;
    esac
  done
elif [ -f /etc/apt/sources.list ]; then
  say "rewriting /etc/apt/sources.list"
  $SUDO cp /etc/apt/sources.list /etc/apt/sources.list.bak
  $SUDO sed -i "s|http://deb.debian.org/debian|$MIRROR|g; s|https://deb.debian.org/debian|$MIRROR|g" /etc/apt/sources.list
else
  say "no apt source files found; skipping mirror switch"
fi

say "apt-get update"
$SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq

say "installing build toolchain"
$SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
  apt-get install -y -qq build-essential python3-dev libffi-dev curl ca-certificates

say "verifying toolchain"
gcc --version | head -1
python3 --version

say "guest provisioning complete"
