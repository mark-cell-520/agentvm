#!/usr/bin/env bash
# agentvm-check — verify the host can actually run an Apple-Silicon Linux VM.
#
# Fails loudly and early. Every check here corresponds to something that
# otherwise produces a confusing error much later in the process.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

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
#
# 量哪个卷很关键。原来写 `df -k /`——根卷。而现在有两份数据落在不同卷上：
#   · VM 数据（~/.agentvm/vms）在本地盘；
#   · 系统镜像在 ~/.agentvm/images/ 下，每个条目是指向 /Volumes/移动/... 的软链
#     （目录本身还在本地盘，所以 df 目录量不到外接盘，必须 df 一个具体条目——
#     df 跟随软链到底）。
# 只量根卷时两个都指不对：本机实测根卷 106 GiB、外接盘 3.2 TiB，差 30 倍；
# 而反过来的情形（外接盘快满）更要紧——那会等到 QEMU 写盘 ENOSPC 才暴露，
# 那时盘上可能已经写了几个小时。2026-11-11 审计改为两个卷都量。
mkdir -p "$AGENTVM_HOME" 2>/dev/null || true

# 探针：images 下第一个指得动东西的条目。全是悬空软链时退回 AGENTVM_HOME。
IMG_PROBE=""
for f in "${AGENTVM_HOME}/images"/*; do
  if [ -e "$f" ]; then IMG_PROBE="$f"; break; fi
done
[ -n "$IMG_PROBE" ] || IMG_PROBE="$AGENTVM_HOME"

check_free() {
  local probe="$1" label="$2" need="$3" fs kb gib
  fs="$(df -k "$probe" 2>/dev/null | awk 'NR==2{print $1}')"
  kb="$(df -k "$probe" 2>/dev/null | awk 'NR==2{print $4}')"
  [ -n "$kb" ] || { warn "读不到 ${label} 的剩余空间（${probe}）"; return 0; }
  gib=$((kb / 1024 / 1024))
  if [ "$gib" -lt "$need" ]; then
    warn "only ${gib} GiB free on $label ($fs); aim for $need+ GiB"
  else
    ok "$label ($fs): ${gib} GiB free"
  fi
}
check_free "$AGENTVM_HOME" "VM 数据所在卷" 15
check_free "$IMG_PROBE"   "系统镜像所在卷" 15

# 镜像软链悬空检查。镜像现在在外接盘上、本地只留软链，而 vmkit 判断「镜像已有」
# 用的是 is_file() 加 stat()——两者都跟随目标，悬空时它判「没有」，于是重下
# 24 GB。盘没挂在这儿早失败，比等到下载阶段才发现便宜得多。
if [ -d "${AGENTVM_HOME}/images" ]; then
  dangling=0
  for f in "${AGENTVM_HOME}/images"/*; do
    [ -L "$f" ] && [ ! -e "$f" ] && dangling=$((dangling + 1))
  done
  if [ "$dangling" -gt 0 ]; then
    die "$dangling 个镜像软链悬空——外接盘没挂上？挂好后重跑，否则会重下全部镜像"
  fi
  ok "镜像软链都指着东西（$(ls -1 "${AGENTVM_HOME}/images" 2>/dev/null | wc -l | tr -d ' ') 个条目）"
fi

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
