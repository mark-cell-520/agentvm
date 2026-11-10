#!/usr/bin/env bash
# agentvm-autostart — make the whole chain survive a reboot.
#
# There are TWO independent things that must start, and it is easy to set up only
# one and then wonder why the bot is silent after a restart:
#
#   host:   the VM itself          -> launchd (RunAtLoad + KeepAlive)
#   guest:  the agent gateway      -> systemd (enabled)
#
# launchd starts the VM when you log in; the guest's systemd then starts the
# gateway as part of its normal boot. A LaunchAgent (not a LaunchDaemon) is used
# because the VM lives in your home directory and a root daemon would create
# root-owned files there.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

[ -x "$TART_BIN" ] || die "tart not found at $TART_BIN"
LABEL="com.agentvm.${VM_NAME}"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
# readlink -f 在老版本 macOS 的 BSD readlink 上没有，会静默走 `|| echo` 兜底，
# 于是 plist 里指向 ~/.local/bin/tart 这个符号链接。用户以后改用 Homebrew 的
# tart 并删掉符号链接，自启就静默失效了。用 python3 解析真实路径（脚本本来就
# 依赖 python3），解析不出再退回原值。
TART_REAL="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' \
  "$TART_BIN" 2>/dev/null || echo "$TART_BIN")"

# ------------------------------------------------------------- host side ----
info "installing launchd agent $LABEL"
mkdir -p "$HOME/Library/LaunchAgents"
# 日志目录必须先建。plist 里 StandardOutPath 指向 ~/.tart/，而全新 Mac 上 tart
# 一次都没跑过、这个目录不存在——launchd 打不开日志文件就直接放弃，RunAtLoad
# 加 KeepAlive 下一个字都不报。实测现象：脚本打印「Auto-start chain: ...」看着
# 成功，重启登录后虚拟机根本没起来。
mkdir -p "$HOME/.tart"

# Unload first so re-running this script is safe.
# bootout/unload 都可能因为「当前没加载」而失败，那不是错误。
if ! launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null; then
  launchctl unload "$PLIST" 2>/dev/null || true
fi

# VM_NAME 会原样进 plist 的 Label、ProgramArguments 和日志路径。含空格或 XML
# 特殊字符时写出的 plist 非法，launchctl bootstrap 和 load 都失败，set -e 让脚本
# 以一句裸 launchctl 错误退出，报错指不回 VM_NAME。写 plist 前先卡一道。
case "$VM_NAME" in
  *[!a-zA-Z0-9._-]*) die "VM_NAME 含非法字符（只允许字母数字和 . _ -）：$VM_NAME" ;;
esac

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${TART_REAL}</string>
        <string>run</string>
        <string>${VM_NAME}</string>
        <string>--no-graphics</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>${HOME}/.tart/${VM_NAME}-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/.tart/${VM_NAME}-launchd.log</string>
</dict>
</plist>
EOF

# KeepAlive 只接异常退出，不接「用户主动 tart stop」。
# 原来是无条件 true：用户跑 make stop 或 tart stop 想临时停一下，几秒后
# launchd 又把它拉起来，停一次起一次，用户以为 stop 命令坏了。
# 要真正停机用 launchctl bootout gui/$(id -u) $PLIST。
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
  || launchctl load "$PLIST"
ok "launchd agent loaded: $PLIST"

# ------------------------------------------------------------ guest side ----
# 客人机里这一步失败不能带走整个脚本：原来 `sudo systemctl is-enabled` 在
# set -eu 下返回非零，tart exec 失败会让宿主机脚本在这里静默退出，末尾的
# 「Auto-start chain」说明和验证命令一条都不打印——用户以为配好了。
if "$TART_BIN" exec "$VM_NAME" true 2>/dev/null; then
  info "enabling guest systemd service"
  # `hermes gateway install --system` creates and enables the unit. Run it from
  # the agent's own directory so it resolves the venv correctly.
  if "$TART_BIN" exec "$VM_NAME" sh -s <<'EOF'
set -eu
cd "${HERMES_HOME:-$HOME/.hermes}/hermes-agent" 2>/dev/null || { echo "warn: agent dir not found"; exit 0; }
sudo ./venv/bin/hermes gateway install --system --start-now --start-on-login || true
sudo systemctl enable hermes-gateway 2>/dev/null || true
sudo systemctl is-enabled hermes-gateway || echo "warn: hermes-gateway 没 enable 上，重启后不会自启"
EOF
  then
    ok "guest service enabled"
  else
    warn "guest service 没配好（见上一行），重启后网关不会自启"
  fi
else
  warn "VM not running; guest service will be configured on first boot"
fi

cat >&2 <<MSG

Auto-start chain:
  Mac login -> launchd ($LABEL) -> tart run $VM_NAME
            -> guest boot -> systemd hermes-gateway -> agent gateway -> Feishu

Verify after a reboot with:
  launchctl list | grep $LABEL
  tart list
  tart exec $VM_NAME systemctl is-active hermes-gateway
  tart exec $VM_NAME sudo journalctl -u hermes-gateway -n 30

注意 tart stop 停不掉它：KeepAlive 只管异常退出，主动 stop 之后 launchd 会在
下次登录或重启时再拉起来。要现在真正停掉：
  launchctl bootout gui/$(id -u) $PLIST
以后再也不自启：
  rm $PLIST && launchctl bootout gui/$(id -u) $PLIST
MSG
