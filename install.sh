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

# ------------------------------------------------------------ 3. 编 vmz --

# vmz 是 macOS 客人机的后端：Virtualization.framework 只有 Swift/ObjC API，
# Python 调不了，所以那一条路是一个独立的 Swift 小程序。它必须带
# com.apple.security.virtualization entitlement 签名，否则 VZMacOSRestoreImage
# 返回「restore image catalog failed to load」——一句完全看不出原因的话。
# 编不出来不影响 Windows / Linux 两条路，所以这里只警告不退出。
log "编译 vmz（macOS 虚拟机后端）"
# 日志用 mktemp，不写死 /tmp/agentvm-vmz-build.log：固定路径在多用户机器上会被
# 有人预建的符号链接劫持，或两遍并发互相覆盖，swiftc 输出（含源码绝对路径）就
# 追加到攻击者指定的文件里。
VMZ_LOG="$(mktemp "${TMPDIR:-/tmp}/agentvm-vmz-build.XXXXXX")"
if command -v swiftc >/dev/null 2>&1; then
    VMBIN="$HOME/.agentvm/bin"
    mkdir -p "$VMBIN"
    SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null || true)
    # 「拿不到 SDK」和「编译失败」分开报。原来合成一个 else：有 swiftc 而
    # xcrun 不可用时 SDK 为空，日志文件是空的或上次残留的，却打印「vmz 编译
    # 失败」——用户对着一个空日志排查编译问题，方向从一开始就错了。
    if [ -z "$SDK" ]; then
        warn "拿不到 macOS SDK（xcrun 输出为空），跳过 vmz（macOS 虚拟机用不了，Windows/Linux 不受影响）"
        warn "多半是没装 Xcode 命令行工具：xcode-select --install"
    elif swiftc -O -sdk "$SDK" \
            -target arm64-apple-macos14.0 \
            -o "$VMBIN/vmz" \
            "$ROOT/app/Sources/MacVMEngine.swift" \
            "$ROOT/scripts/vmz/main.swift" 2>"$VMZ_LOG"; then
        if codesign -s - --entitlements "$ROOT/app/Resources/entitlements.plist" \
                --force "$VMBIN/vmz" 2>>"$VMZ_LOG"; then
            # 跑一次 check 确认它真的能起来。签名过的 Mach-O 也可能因为
            # 别的原故起不来（库缺失、架构不符），而「文件在那儿」不等于能用。
            if "$VMBIN/vmz" check >/dev/null 2>&1; then
                ok "vmz 已装到 $VMBIN/vmz（check 通过）"
            else
                warn "vmz 装好了但 check 跑不通，macOS 虚拟机可能用不了"
                warn "详见 $VMZ_LOG"
                rm -f "$VMBIN/vmz"
            fi
        else
            warn "vmz 编出来了但签名失败，macOS 虚拟机可能用不了（详见 ${VMZ_LOG}）"
            # 不能用的东西不留。macOS 上签错的 Mach-O 常见表现是进程能起但被
            # SIGKILL（137），而它在 PATH 能找到的位置上，vmkit 会认为装好了、
            # 一路走到安装中途才失败。install-qemu.sh 对 QEMU 就是跑一次
            # --version 实测的，这里照办。
            rm -f "$VMBIN/vmz"
            warn "已删掉未签名的 vmz（留着比没有更糟：vmkit 会以为装好了）"
        fi
    else
        warn "vmz 编译失败，macOS 虚拟机用不了。Windows 和 Linux 不受影响。"
        warn "详见 $VMZ_LOG"
        rm -f "$VMBIN/vmz"
    fi
else
    warn "没有 swiftc，跳过 vmz（macOS 虚拟机用不了，Windows/Linux 不受影响）"
fi

# ------------------------------------------------------------ 4. 装 app --

log "安装 AgentVM.app"
if [ -d "$APP_DST" ]; then
    warn "已存在 ${APP_DST}，覆盖"
    # 先把跑着的实例退掉再删。app 开着时 rm 掉旧 bundle，运行中的实例继续用旧
    # inode，新装的对外不可见——用户以为「装了没生效」。osascript 优雅退出，
    # pkill 兜底；两者在 app 没运行时都会返回非零，用 || true 挡住 set -e。
    osascript -e 'quit app "AgentVM"' 2>/dev/null || true
    pkill -x AgentVM 2>/dev/null || true
    sleep 1
    rm -rf "$APP_DST"
fi

# 本地那份 AgentVM.app 是不是比源码新。
#
# 原来只看「存在且可执行」就复用，而 AgentVM.app/ 在 .gitignore 里——
# 它永远只是上一次本地构建的产物，不是「仓库内置的构建」。改完 Swift
# 源码再跑 ./install.sh，装上去的还是旧 app，脚本还打印
# 「ok 已从仓库内置的构建拷贝」。实测踩过：app mtime 07:48 而最新源码
# 07:52，装的是旧的。
# 更糟的组合：旧 app 的 Resources/backend/ 里没有 vmz，而第 3 步刚编好的
# vmz 只装到了 ~/.agentvm/bin，app 里没有——用户拿到一个没有 macOS 后端的
# app，而日志里全是 ok。
app_is_stale() {
    [ -x "$APP_SRC/Contents/MacOS/AgentVM" ] || return 0
    # find 列表要覆盖 build.sh 真正拷进 bundle 的每个源：app/Resources
    # （entitlements.plist）、scripts/winarm-vm.py、scripts/autoinstall.py、
    # scripts/catalog.json，以及整个 scripts/lib/（qga.py 住这儿，vmkit.py
    # import 它）。
    # 原来只列 app/Sources、build.sh、vmz/main.swift、vmkit.py，改了后三样再跑
    # ./install.sh 会复用旧 app，而日志打印「源码没有比它新」。少列 lib/ 的
    # 后果更具体：改了客人机代理客户端，重装后 app 里还是旧的那份。少列
    # autoinstall.py 的后果同型：改了自动安装驱动器，app 里还是旧的那份，
    # 而症状是「一键安装还和以前一样」。
    newer=$(find "$ROOT/app/Sources" "$ROOT/app/Resources" "$ROOT/app/build.sh" \
                "$ROOT/scripts/vmz/main.swift" "$ROOT/scripts/vmkit.py" \
                "$ROOT/scripts/winarm-vm.py" "$ROOT/scripts/autoinstall.py" \
                "$ROOT/scripts/catalog.json" \
                "$ROOT/scripts/lib" \
                -newer "$APP_SRC/Contents/MacOS/AgentVM" 2>/dev/null | head -1)
    [ -n "$newer" ]
}

if [ -d "$APP_SRC" ] && [ -x "$APP_SRC/Contents/MacOS/AgentVM" ] \
        && ! app_is_stale; then
    cp -R "$APP_SRC" /Applications/
    ok "已从本地构建拷贝（源码没有比它新）"
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
# 必须带 --entitlements：Virtualization.framework 的所有 macOS 客人机 API
# 都要求 com.apple.security.virtualization，少了它 app 里的 macOS VM 全都起不来。
xattr -dr com.apple.quarantine "$APP_DST" 2>/dev/null || true
# 签名日志同样用 mktemp：写死 /tmp/agentvm-app-sign.log 会被符号链接劫持或并发覆盖。
APP_SIGN_LOG="$(mktemp "${TMPDIR:-/tmp}/agentvm-app-sign.XXXXXX")"
if ! codesign --force --deep --entitlements "$ROOT/app/Resources/entitlements.plist" \
        -s - "$APP_DST" 2>"$APP_SIGN_LOG"; then
    warn "签名失败，app 可能被 Gatekeeper 拦（详见 ${APP_SIGN_LOG}）"
fi

# --deep 不把 entitlement 传给嵌套二进制。install.sh 复用的可能是仓库里陈旧的
# AgentVM.app（老 build.sh 编的，嵌套 vmz 从没单独签过），这时 --deep 签完
# bundle，codesign -d 查 vmz 的 entitlement 是空的，而 --verify --deep --strict
# 仍返回 OK——签名校验全绿，macOS 虚拟机一启动就 NSInvalidArgumentException abort，
# 而这里一个字都不提。所以签完 bundle 再补一道嵌套 vmz 的 entitlement 校验。
if [ -f "$APP_DST/Contents/Resources/backend/vmz" ]; then
    if ! codesign -d --entitlements - "$APP_DST/Contents/Resources/backend/vmz" 2>/dev/null \
            | grep -q com.apple.security.virtualization; then
        warn "app 里的 vmz 缺 com.apple.security.virtualization entitlement，macOS 虚拟机会一启动就崩"
        warn "这是复用旧构建导致的：删掉 AgentVM.app 后重跑 ./install.sh 让它重新编译"
    fi
fi

[ -x "$APP_DST/Contents/MacOS/AgentVM" ] || die "装完了但二进制不在 $APP_DST"

# ------------------------------------------------------------ 4. 打开 --

log "打开 AgentVM"
open "$APP_DST"

cat <<MSG

装好了。

  app 位置    /Applications/AgentVM.app
  虚拟机数据  ~/.agentvm
  卸载        删掉 /Applications/AgentVM.app（要连数据一起删就删 ~/.agentvm）

打开后点左上角的 + 新建虚拟机：选 Windows 11、macOS 或者 Linux，点开始
安装，剩下的它自己跑完。
  Windows 11   下载 4.3 GB 并跑一遍安装程序，约 10 到 15 分钟
  Linux        用的是装好的云镜像，下载完就能登，约 1 到 3 分钟
  macOS        从 Apple 官方恢复镜像安装，下载 25.6 GB、装完三十多 GB，
               全程要 40 到 60 分钟，磁盘峰值 60 GB 以上

MSG
