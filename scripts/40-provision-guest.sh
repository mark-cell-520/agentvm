#!/usr/bin/env bash
# agentvm-provision-guest — run INSIDE the guest to make it usable.
#
# Copy in with:  tart exec <vm> bash -s < scripts/40-provision-guest.sh
# or push it and run it: see `make provision`.
#
# 必须是 bash 不是 sh：第 18 行是 `set -euo pipefail`，而 Debian 的 /bin/sh 是
# dash，dash 不支持 pipefail，整段脚本第一行就退，换源和装编译器一样都不做。
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

# 备份名带时间戳。原来固定 `$f.bak`：跑第二遍时 .bak 里存的已经是第一遍改过的
# 内容，原始 apt 源永久丢失，想改回去只能手输。
STAMP=$(date +%Y%m%d-%H%M%S)
backup() { $SUDO cp -n "$1" "$1.bak-$STAMP" 2>/dev/null || true; }

if ls /etc/apt/mirrors/*.list >/dev/null 2>&1; then
  say "switching apt mirror to $MIRROR"
  for f in /etc/apt/mirrors/*.list; do
    case "$f" in
      *security*) backup "$f"; printf '%s\n' "$SEC_MIRROR" | $SUDO tee "$f" >/dev/null ;;
      *)          backup "$f"; printf '%s\n' "$MIRROR"    | $SUDO tee "$f" >/dev/null ;;
    esac
  done
elif [ -f /etc/apt/sources.list ]; then
  say "rewriting /etc/apt/sources.list"
  backup /etc/apt/sources.list
  $SUDO sed -i "s|http://deb.debian.org/debian|$MIRROR|g; s|https://deb.debian.org/debian|$MIRROR|g" /etc/apt/sources.list
# Debian 13（trixie）起官方镜像改用 deb822 格式的 .sources，老的两支都不匹配。
# 原来遇到它只会打印一句人畜无害的 "skipping mirror switch" 然后继续用
# deb.debian.org 以几十 KB/s 下载——正是这个脚本要解决的那个问题，但用户
# 完全看不出换源没做。
elif ls /etc/apt/sources.list.d/*.sources >/dev/null 2>&1; then
  say "switching deb822 sources to $MIRROR"
  for f in /etc/apt/sources.list.d/*.sources; do
    backup "$f"
    $SUDO sed -i "s|http://deb.debian.org/debian|$MIRROR|g; s|https://deb.debian.org/debian|$MIRROR|g" "$f"
  done
else
  # 找不到任何源文件要 warn 而不是 say：这一步没做，后面 apt-get 会慢到
  # 几小时，而 say 的语气像是正常跳过。
  printf 'warn: no apt source files found; mirror NOT switched\n' >&2
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
