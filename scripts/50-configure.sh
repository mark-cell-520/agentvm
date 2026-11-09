#!/usr/bin/env bash
# agentvm-configure — push config into the guest without leaking secrets.
#
# Secrets are the hard part of automating this. Three rules, learned the hard way:
#
#  1. Never put a secret in argv. `tart exec vm sh -c "... $SECRET ..."` puts it
#     in the host's process table where anything can read it.
#
#  2. Never retype a secret you were handed in a chat/CI log. Redaction layers
#     silently replace the first characters of anything that looks like a key
#     with a placeholder token, and the result is written to disk verbatim. The
#     failure is silent: the file looks fine, the API says "invalid credentials".
#     Transport via base64 and assert the length before writing.
#
#  3. `.env` must be 0600 and live where the agent expects it (~/.hermes/.env).
#
# This script reads secrets from files you provide (never from argv), verifies
# their length, and writes them into the guest.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

[ -x "$TART_BIN" ] || die "tart not found at $TART_BIN"

# ENV_TEMPLATE 原来在这儿赋值之后再没被引用过——死代码。真正在用的是
# scripts/lib/patch-agent-config.py。
CONFIG_PATCH="$HERE/../lib/patch-agent-config.py"

# usage 由 lib/common.sh 提供（打到第一个非注释行为止），这里不重复定义。

LLM_KEY_FILE=""
LLM_BASE_URL=""
LLM_MODEL=""
FEISHU_APP_ID=""
FEISHU_APP_SECRET_FILE=""
FEISHU_DOMAIN="feishu"
YOLO=0

while [ $# -gt 0 ]; do
  case "$1" in
    --vm) VM_NAME="$2"; shift 2 ;;
    --llm-key-file) LLM_KEY_FILE="$2"; shift 2 ;;
    --llm-base-url) LLM_BASE_URL="$2"; shift 2 ;;
    --llm-model) LLM_MODEL="$2"; shift 2 ;;
    --feishu-app-id) FEISHU_APP_ID="$2"; shift 2 ;;
    --feishu-app-secret-file) FEISHU_APP_SECRET_FILE="$2"; shift 2 ;;
    --feishu-domain) FEISHU_DOMAIN="$2"; shift 2 ;;
    --yolo) YOLO=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[ -n "$LLM_KEY_FILE$FEISHU_APP_ID" ] || usage 1

# ---------------------------------------------------------------- sanity ----
[ -f "$LLM_KEY_FILE" ] || die "--llm-key-file not found: $LLM_KEY_FILE"
LLM_KEY_LEN="$(wc -c < "$LLM_KEY_FILE" | tr -d ' ')"
[ "$LLM_KEY_LEN" -ge 20 ] || die "LLM key looks too short ($LLM_KEY_LEN bytes)"
ok "llm key: $LLM_KEY_LEN bytes from $LLM_KEY_FILE"

if [ -n "$FEISHU_APP_SECRET_FILE" ]; then
  [ -f "$FEISHU_APP_SECRET_FILE" ] || die "--feishu-app-secret-file not found"
  FS_LEN="$(wc -c < "$FEISHU_APP_SECRET_FILE" | tr -d ' ')"
  [ "$FS_LEN" -ge 16 ] || die "Feishu secret looks too short ($FS_LEN bytes)"
  ok "feishu secret: $FS_LEN bytes"
fi

# Verify the guest is up before doing anything slow.
"$TART_BIN" exec "$VM_NAME" true 2>/dev/null || die "VM '$VM_NAME' is not reachable — start it first"

# ------------------------------------------------------------- push secrets --
# base64 keeps the value out of argv entirely; the guest decodes into a 0600 file.
info "pushing LLM key into guest"
LLM_B64="$(base64 < "$LLM_KEY_FILE" | tr -d '\n')"
"$TART_BIN" exec "$VM_NAME" sh -c \
  "umask 077; printf '%s' '$LLM_B64' | base64 -d > /tmp/.llmkey; chmod 600 /tmp/.llmkey; wc -c < /tmp/.llmkey" \
  | tr -d '\r' | grep -qx "$LLM_KEY_LEN" || die "LLM key length mismatch after transfer"
ok "llm key transferred and length-verified"

if [ -n "$FEISHU_APP_SECRET_FILE" ]; then
  info "pushing Feishu secret into guest"
  FS_B64="$(base64 < "$FEISHU_APP_SECRET_FILE" | tr -d '\n')"
  "$TART_BIN" exec "$VM_NAME" sh -c \
    "umask 077; printf '%s' '$FS_B64' | base64 -d > /tmp/.fssecret; chmod 600 /tmp/.fssecret; wc -c < /tmp/.fssecret" \
    | tr -d '\r' | grep -qx "$FS_LEN" || die "Feishu secret length mismatch after transfer"
  ok "feishu secret transferred and length-verified"
fi

# --------------------------------------------------------------- apply -------
# The guest-side patch script reads /tmp/.llmkey and /tmp/.fssecret, so the
# secrets never appear in any command line on either side.
#
# 非机密的那几个值（URL、模型名、app id、domain）也要先转义再插值。原来
# `<<EOF` 不加引号，`${LLM_BASE_URL}` 原样进客人机脚本：URL 里带一个双引号
# 就能在客人机里执行任意命令（`--llm-base-url 'http://x"; touch /tmp/pwn;
# echo "'`），普通值里的 `$` 和反引号也会被宿主机先展开一遍，报错指不回真因。
# printf %q 产出的串可以安全地再被 shell 解析一次，这是最短的正确做法。
info "applying configuration inside guest"
Q_BASE_URL="$(printf '%q' "$LLM_BASE_URL")"
Q_MODEL="$(printf '%q' "$LLM_MODEL")"
Q_APP_ID="$(printf '%q' "$FEISHU_APP_ID")"
Q_DOMAIN="$(printf '%q' "$FEISHU_DOMAIN")"
"$TART_BIN" exec "$VM_NAME" sh -s <<EOF
set -eu
export AGENTVM_LLM_BASE_URL=$Q_BASE_URL
export AGENTVM_LLM_MODEL=$Q_MODEL
export AGENTVM_FEISHU_APP_ID=$Q_APP_ID
export AGENTVM_FEISHU_DOMAIN=$Q_DOMAIN
export AGENTVM_YOLO=${YOLO}
$(cat "$CONFIG_PATCH")
EOF

ok "configuration applied"

cat >&2 <<MSG

Next:
  1. Top up / verify the LLM account has balance (a 402 means the key is fine
     but the account is empty — that is a billing problem, not a config one).
  2. Restart the guest agent service:
       tart exec $VM_NAME sudo systemctl restart hermes-gateway
  3. Send a message to the bot in Feishu and watch:
       tart exec $VM_NAME sudo journalctl -u hermes-gateway -f
MSG
