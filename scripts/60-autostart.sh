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
. "$HERE/../lib/common.sh"

[ -x "$TART_BIN" ] || die "tart not found at $TART_BIN"
LABEL="com.agentvm.${VM_NAME}"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TART_REAL="$(readlink -f "$TART_BIN" 2>/dev/null || echo "$TART_BIN")"

# ------------------------------------------------------------- host side ----
info "installing launchd agent $LABEL"
mkdir -p "$HOME/Library/LaunchAgents"

# Unload first so re-running this script is safe.
launchctl unload "$PLIST" 2>/dev/null || true

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
    <true/>
    <key>StandardOutPath</key>
    <string>${HOME}/.tart/${VM_NAME}-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/.tart/${VM_NAME}-launchd.log</string>
</dict>
</plist>
EOF

launchctl load "$PLIST"
ok "launchd agent loaded: $PLIST"

# ------------------------------------------------------------ guest side ----
if "$TART_BIN" exec "$VM_NAME" true 2>/dev/null; then
  info "enabling guest systemd service"
  # `hermes gateway install --system` creates and enables the unit. Run it from
  # the agent's own directory so it resolves the venv correctly.
  "$TART_BIN" exec "$VM_NAME" sh -s <<'EOF'
set -eu
cd "${HERMES_HOME:-$HOME/.hermes}/hermes-agent" 2>/dev/null || { echo "warn: agent dir not found"; exit 0; }
sudo ./venv/bin/hermes gateway install --system --start-now --start-on-login || true
sudo systemctl enable hermes-gateway 2>/dev/null || true
sudo systemctl is-enabled hermes-gateway
EOF
  ok "guest service enabled"
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
MSG
