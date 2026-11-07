#!/usr/bin/env bash
# agentvm-check — verify the host can actually run an Apple-Silicon Linux VM.
#
# Fails loudly and early. Every check here corresponds to something that
# otherwise produces a confusing error much later in the process.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/../lib/common.sh"

fail=0
chk() { # chk <description> <command...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else warn "$desc"; fail=$((fail+1)); fi
}

info "host checks"

[ "$(uname -s)" = "Darwin" ] || die "agentvm targets macOS (got $(uname -s))"
ok "operating system: macOS"

[ "$(uname -m)" = "arm64" ] || die "agentvm requires Apple silicon (got $(uname -m))"
ok "architecture: arm64"

os_major="$(sw_vers -productVersion | cut -d. -f1)"
[ "$os_major" -ge 14 ] || die "macOS 14 or later required (got $(sw_vers -productVersion))"
ok "macOS $(sw_vers -productVersion)"

[ "$(sysctl -n kern.hv_support 2>/dev/null || echo 0)" = "1" ] \
  || die "Virtualization.framework unavailable on this Mac"
ok "virtualization framework supported"

mem_bytes="$(sysctl -n hw.memsize)"
mem_gib=$((mem_bytes / 1024 / 1024 / 1024))
[ "$mem_gib" -ge 8 ] || warn "only ${mem_gib} GiB RAM; a ${VM_MEMORY_MB} MB guest plus the host will be tight"
ok "RAM: ${mem_gib} GiB"

# Disk: the guest disk is sparse, but the image cache and installed agent are not.
avail_kb="$(df -k / | awk 'NR==2{print $4}')"
avail_gib=$((avail_kb / 1024 / 1024))
[ "$avail_gib" -ge 10 ] || warn "only ${avail_gib} GiB free on /; aim for 15+ GiB"
ok "free disk on /: ${avail_gib} GiB"

chk "curl present"            command -v curl
chk "python3 present"         command -v python3
chk "shasum present"          command -v shasum
chk "codesign present"        command -v codesign

if [ -x "$TART_BIN" ]; then
  ok "tart already installed at $TART_BIN"
else
  info "tart not installed yet — run 'make install-tart'"
fi

if tart_ver="$("$TART_BIN" --version 2>/dev/null)"; then
  ok "tart version: $tart_ver"
fi

echo >&2
if [ "$fail" -gt 0 ]; then
  die "$fail check(s) failed"
fi
ok "all host checks passed"
