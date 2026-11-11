#!/usr/bin/env bash
# agentvm-provision-guest — run INSIDE the guest to make it usable.
#
# Copy in with:  tart exec <vm> bash -s < scripts/40-provision-guest.sh
# or push it and run it: see `make provision`.
#
# 必须是 bash 不是 sh：第 21 行是 `set -euo pipefail`，而 Debian 的 /bin/sh 是
# dash，dash 不支持 pipefail，整段脚本第一行就退，换源和装编译器一样都不做。
# 2026-11-11 实测（/bin/dash -s）：set: Illegal option -o pipefail，退出码 2。
# 所以三个入口必须都送 bash：Makefile 的 provision 目标、backends/tart.py 的
# exec_vm_script、以及直接 `tart exec <vm> bash -s < 本文件`。
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
# 只在实际会换源时才 update。原来无条件跑一次：apt-get update 要把整个
# Packages 索引拉一遍（换成本地镜像后是几十 MB），而这一步在「源已经是镜像、
# 包也装好了」的重跑里纯属白下。2026-11-11 审计补上。
NEED_UPDATE=0
case "${AGENTVM_APT_MIRROR:-}" in
  "") [ -f /etc/apt/mirrors/.agentvm-mirror-applied ] || NEED_UPDATE=1 ;;
  *)  NEED_UPDATE=1 ;;   # 显式指定了新镜像，按用户的要求重新换源
esac
# 上面那个标记文件本身不可靠（客人机重建就没了），所以再兜一个：镜像列表里
# 已经没有 deb.debian.org 就说明换过源了。
if [ "$NEED_UPDATE" -eq 0 ] && grep -rqs "deb.debian.org" /etc/apt/ 2>/dev/null; then
  NEED_UPDATE=1
fi
if [ "$NEED_UPDATE" -eq 1 ]; then
  $SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq
  $SUDO touch /etc/apt/mirrors/.agentvm-mirror-applied 2>/dev/null || true
else
  say "apt 源已经是镜像且索引没过期，跳过 apt-get update"
fi

say "installing build toolchain"
# 两个改动，都是省客人机盘和下载量——而客人机盘落在外接盘上（实测写入只有
# 14.5 MB/s）：
#  1. --no-install-recommends。build-essential 的 recommends 链会多拉一批包，
#     常见量级几十 MB 起，而编译工具链一个都用不上。
#  2. 装完 clean + 清 lists。apt 的 Packages 索引常驻 /var/lib/apt/lists，
#     一个 Debian 基础系统上是几十 MB，装完包就没用了。
# 另外加一道「装好了就跳过」：dpkg -s 全中就不再跑 apt-get install。原来重跑
# provision 必然再走一次 apt-get install（有 -y 也仍然要解析依赖、下载校验）。
if $SUDO dpkg -s build-essential python3-dev libffi-dev curl ca-certificates \
        >/dev/null 2>&1; then
  say "build toolchain already installed, skipping apt-get install"
else
  $SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
    apt-get install -y -qq --no-install-recommends \
    build-essential python3-dev libffi-dev curl ca-certificates
  $SUDO apt-get clean
  $SUDO rm -rf /var/lib/apt/lists/*
fi

say "verifying toolchain"
gcc --version | head -1
python3 --version

say "guest provisioning complete"
