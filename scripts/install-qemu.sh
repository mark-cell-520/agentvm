#!/bin/sh
# 把 QEMU 装上。app 自己没有虚拟化引擎，全靠 QEMU 加 HVF。
#
# 为什么需要单独一步：macOS 不自带 QEMU，而 Windows 在 Apple Silicon 上
# 不能用 Virtualization.framework 跑（没有 vTPM、virtio 设备集固定而 Windows
# 无对应驱动、EFI 启动链有缺口），所以 tart / lume / apple/container 全都跑不了
# Windows，QEMU 是唯一的选择。这一步没法省。
#
# 按顺序试四条路，前一条通了就不走后面的：
#   1. 已经装了            —— 什么都不做
#   2. PATH 里有 brew      —— brew install qemu
#   3. 已知位置有 brew     —— 直接用那个 brew（~/hb、/opt/homebrew）
#   4. /opt/homebrew 可写   —— 手工建一个最小 Homebrew 再装
#
# 四条都不通就明确告诉用户卡在哪，不假装成功。
set -eu

QEMU_BIN=qemu-system-aarch64

log() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()  { printf 'ok  %s\n' "$*"; }
warn() { printf 'warn %s\n' "$*"; }
die()  { printf '错误：%s\n' "$*" >&2; exit 1; }

# QEMU 在哪。目录列表和 scripts/vmkit.py 的 which() 保持一致——原来少了
# HOMEBREW_PREFIX，会出现「vmkit 找得到、安装脚本判成没装」，于是走
# brew install 再 die「brew 说装完了但找不到」，两边说的不是一回事。
qemu_path() {
    local d
    if command -v "$QEMU_BIN" >/dev/null 2>&1; then
        command -v "$QEMU_BIN"
        return 0
    fi
    for d in "$HOME/hb/bin" "${HOMEBREW_PREFIX:-/opt/homebrew}/bin" \
             /opt/homebrew/bin /usr/local/bin; do
        if [ -x "$d/$QEMU_BIN" ]; then
            echo "$d/$QEMU_BIN"
            return 0
        fi
    done
    return 1
}

# 光看文件在不在不够。docs/WINDOWS-ARM-ON-MACOS.md 的「改完必须重签」一节记着：
# 签名或重定位弄错的 Mach-O 表现是「进程能起但零输出、退出码 137」，-x 判断
# 照样通过。所以真的跑一次 --version。原来只看 -x，install.sh 会打印
# 「ok QEMU 已安装」，用户点新建虚拟机时才失败，报错离原因很远。
have_qemu() {
    local p
    p=$(qemu_path) || return 1
    "$p" --version >/dev/null 2>&1
}

# ---------------------------------------------------------------- 第一条路 --

if have_qemu; then
    # 报真实找到的路径。原来 command -v 失败时回显一句写死的
    # $HOME/hb/bin/$QEMU_BIN，实际命中 /opt/homebrew 时用户被指到一个
    # 不存在的路径去排查。
    ok "QEMU 已安装：$(qemu_path)"
    exit 0
fi

# ---------------------------------------------------------------- 找 brew --

BREW=""
if command -v brew >/dev/null 2>&1; then
    BREW=$(command -v brew)
elif [ -x "$HOME/hb/bin/brew" ]; then
    BREW="$HOME/hb/bin/brew"
elif [ -x /opt/homebrew/bin/brew ]; then
    BREW=/opt/homebrew/bin/brew
fi

if [ -n "$BREW" ]; then
    log "用 $BREW 装 QEMU"
    # HOMEBREW_NO_AUTO_UPDATE：自动更新会把一次安装变成几十分钟，而且
    # 经常在更新半途失败，把用户卡在一个说不清的界面上。
    HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
        "$BREW" install qemu || die "brew install qemu 失败。手工跑一遍看完整输出。"
    if have_qemu; then
        ok "QEMU 装好了"
        exit 0
    fi
    die "brew 说装完了但找不到 ${QEMU_BIN}。brew --prefix qemu 看看装哪了。"
fi

# -------------------------------------------- 第四条路：/opt/homebrew 可写 --
#
# 这一条在这台机器上从未实测过（QEMU 已经在 ~/hb/bin）。已知的风险：
#  - 只拉 Homebrew 本体、不拉 homebrew-core tap，`brew install qemu` 会自己去
#    github.com clone homebrew-core——正是本项目要绕开的被限速主机。
#  - /opt/homebrew 是 14 字符前缀，超过 bottle 重定位上限，会被迫源码编译。
# 所以下面显式 tap 一次，并把话说在前面：这条路很慢。

# 原来用 `mkdir -p /opt/homebrew/bin 2>/dev/null && [ -w ... ]` 探测，探测失败
# 也会留下一个空的 /opt/homebrew/bin。先判断再建，别留垃圾。
# 「目录存在」不等于「可写」：机器上有一个 root 所有的 /opt/homebrew 而 /opt
# 不可写时，`[ -d /opt/homebrew ]` 为真，脚本打印「/opt/homebrew 可写」这句假话，
# 往下 cp 权限失败，set -e 让脚本以一句裸 cp: Permission denied 退出，末尾给用户
# 指路的 MSG 一个字都不打印。改成直接测可写：不存在时靠下面 mkdir -p 创建，
# /opt 可写就够。
if [ -w /opt/homebrew ] || [ -w /opt ]; then
    log "/opt/homebrew 可写，装一个最小 Homebrew（慢，见注释）"
    # 只拉 Homebrew 本体，不跑官方安装脚本——那个脚本要 sudo 且会改 shell 配置。
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    # 先下到文件再解，不能 `curl | tar`。脚本是 /bin/sh 且没有 pipefail，
    # 管道退出码是 tar 的：curl 下不动时 tar 解个空目录、返回 0，脚本继续
    # cp、brew tap、brew install，最后报「装 Homebrew 成功但 QEMU 装失败」——
    # 而 Homebrew 一个字节都没下来。开头承诺的「明确告诉用户卡在哪一条」
    # 在这条路上是假的。
    curl -fsSL https://github.com/Homebrew/brew/tarball/master \
        -o "$tmp/brew.tgz" || die "下载 Homebrew 失败（网络问题）"
    tar -xzf "$tmp/brew.tgz" -C "$tmp" --strip-components=1 \
        || die "解包 Homebrew 失败"
    mkdir -p /opt/homebrew
    # 不覆盖已有内容：原来 `cp -R "$tmp"/. /opt/homebrew/`，重跑会把原来的
    # Homebrew 配置和已装的 formula 链接搅乱。
    cp -Rn "$tmp"/. /opt/homebrew/ 2>/dev/null || cp -R "$tmp"/. /opt/homebrew/
    warn "接下来 brew 要从 github.com clone homebrew-core，这一步可能几小时"
    HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
        /opt/homebrew/bin/brew tap homebrew/core \
        || warn "tap homebrew/core 失败；brew install 会自己去 clone"
    HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
        /opt/homebrew/bin/brew install qemu \
        || die "装 Homebrew 成功但 QEMU 装失败"
    if have_qemu; then
        ok "QEMU 装好了：/opt/homebrew/bin/$QEMU_BIN"
        exit 0
    fi
fi

# ---------------------------------------------------------------- 都没通 --

cat >&2 <<'MSG'

四条路都没走通，QEMU 装不上。原因是这台 Mac 上：

  1. 没有 QEMU；
  2. 没有 Homebrew（PATH、~/hb、/opt/homebrew 都没有）；
  3. /opt/homebrew 不可写，而 sudo 又要密码。

第三条是 macOS 的默认情况：/opt 属于 root。要打通，任选一个办法：

  办法一（推荐，最省事）
    先装官方 Homebrew，它会用 sudo 建 /opt/homebrew：
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    然后重跑本脚本。

  办法二（不要 sudo）
    把 Homebrew 装到 ~/hb。注意前缀必须不超过 13 个字符，否则 bottle
    无法重定位、会被迫源码编译：
      mkdir -p ~/hb
      curl -L https://github.com/Homebrew/brew/tarball/master -o /tmp/brew.tgz
      tar -xzf /tmp/brew.tgz -C ~/hb --strip-components=1
    然后重跑本脚本（它会找到 ~/hb/bin/brew）。

    这条路的坑和完整可复用做法见 docs/WINDOWS-ARM-ON-MACOS.md 的
    「依赖怎么装」一节：bottle 要从 ghcr.io 拉、改写 install name 必须用
    install_name_tool、改完必须重签否则 macOS 直接 SIGKILL。

MSG
exit 1
