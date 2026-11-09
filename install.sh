#!/bin/sh
# agentvm 一键安装：把 AgentVM.app 装到 /Applications 并打开。
#
# 用法：
#     ./install.sh
#
# 做四件事，每件都会说清成没成：
#   1. 检查是不是 Apple Silicon 的 macOS（这是硬性要求，不是建议）
#   2. 装 QEMU（没有才装）
#   3. 编 AgentVM.app 并拷到 /Applications
#   4. 打开 app
#
# 不碰 sudo、不改 shell 配置、不往别处写东西。装完想卸，把
# /Applications/AgentVM.app 删掉即可，虚拟机数据在 ~/.agentvm，要一起删
# 就删那个目录。
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
APP_SRC="$ROOT/AgentVM.app"
APP_DST="/Applications/AgentVM.app"

log() { printf '\n\033[36m==>\033[0m %s\n' "$*"; }
ok()  { printf 'ok  %s\n' "$*"; }
warn() { printf 'warn %s\n' "$*"; }
die()  { printf '\n错误：%s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------ 1. 机型检查 --

log "检查机器"
[ "$(uname -s)" = "Darwin" ] || die "只在 macOS 上有效。"
[ "$(uname -m)" = "arm64" ] || die "只在 Apple Silicon（arm64）上有效，当前是 $(uname -m)。"
SW=$(sw_vers -productVersion)
ok "macOS $SW / $(uname -m)"

# ------------------------------------------------------------ 2. QEMU --

log "检查虚拟化引擎"
sh "$ROOT/scripts/install-qemu.sh"

# ------------------------------------------------------------ 3. 装 app --

log "安装 AgentVM.app"
if [ -d "$APP_DST" ]; then
    warn "已存在 $APP_DST，覆盖"
    rm -rf "$APP_DST"
fi

if [ -d "$APP_SRC" ] && [ -x "$APP_SRC/Contents/MacOS/AgentVM" ]; then
    # 仓库里已经带编好的 app，直接用
    cp -R "$APP_SRC" /Applications/
    ok "已从仓库内置的构建拷贝"
else
    # 没有现成的，现场编。需要 Xcode 命令行工具（自带 swiftc）。
    if ! command -v swiftc >/dev/null 2>&1; then
        cat >&2 <<'MSG'

编 app 需要 swiftc，它随 Xcode 命令行工具提供。装它：

    xcode-select --install

装完重跑 ./install.sh。这一步会弹系统对话框，要你点确认，脚本没法替你点。

MSG
        die "没有 swiftc，编不了 app"
    fi
    sh "$ROOT/app/build.sh"
    cp -R "$APP_SRC" /Applications/
    ok "已编译并拷贝"
fi

# app 是从别处 cp 过来的，ad-hoc 签名要重新打一次，否则 macOS 可能认成
# 「已损坏」。build.sh 里签过，但 cp 会带上 quarantine 属性。
xattr -dr com.apple.quarantine "$APP_DST" 2>/dev/null || true
codesign --force --deep -s - "$APP_DST" 2>/dev/null || warn "签名失败，app 可能被 Gatekeeper 拦"

[ -x "$APP_DST/Contents/MacOS/AgentVM" ] || die "装完了但二进制不在 $APP_DST"

# ------------------------------------------------------------ 4. 打开 --

log "打开 AgentVM"
open "$APP_DST"

cat <<MSG

装好了。

  app 位置    /Applications/AgentVM.app
  虚拟机数据  ~/.agentvm
  卸载        删掉 /Applications/AgentVM.app（要连数据一起删就删 ~/.agentvm）

打开后点左上角的 + 新建虚拟机：选 Windows 11 或者 Linux，选一个版本，
点开始安装，剩下的它自己跑完。Windows 要下载 4.3 GB 并跑一遍安装程序，
约 10 到 15 分钟；Linux 用的是装好的云镜像，下载完就能登，约 1 到 3 分钟。

MSG
