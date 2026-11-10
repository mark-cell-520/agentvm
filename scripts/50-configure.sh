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
# 路径是 $HERE/lib/，不是 $HERE/../lib/。$HERE 已经是 scripts/，
# 写成 ../lib 会解析到仓库根的 lib/——那儿什么都没有。而下面
# `$(cat "$CONFIG_PATCH")` 在未加引号的 heredoc 里，cat 失败只往 stderr
# 打一句，命令替换失败不会让 set -e 触发：脚本继续跑完、退出 0，
# 客人机只收到几行 export 和一个空的补丁段，宿主机打印
# 「ok configuration applied」。密钥已经推进 guest 而 patch 从未执行。
CONFIG_PATCH="$HERE/lib/patch-agent-config.py"
# -r 而不是 -f：文件在但权限 000 时 -f 为真，往下 $(cat ...) 失败又被未加引号的
# heredoc 吞掉，客人机收到空补丁段，宿主机还打印「ok configuration applied」。
# 直接测可读，堵住这个假绿。
[ -r "$CONFIG_PATCH" ] || die "读不到补丁脚本：$CONFIG_PATCH"

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
    --vm) [ $# -ge 2 ] || die "--vm 需要一个值"; VM_NAME="$2"; shift 2 ;;
    --llm-key-file) [ $# -ge 2 ] || die "--llm-key-file 需要一个值"; LLM_KEY_FILE="$2"; shift 2 ;;
    --llm-base-url) [ $# -ge 2 ] || die "--llm-base-url 需要一个值"; LLM_BASE_URL="$2"; shift 2 ;;
    --llm-model) [ $# -ge 2 ] || die "--llm-model 需要一个值"; LLM_MODEL="$2"; shift 2 ;;
    --feishu-app-id) [ $# -ge 2 ] || die "--feishu-app-id 需要一个值"; FEISHU_APP_ID="$2"; shift 2 ;;
    --feishu-app-secret-file) [ $# -ge 2 ] || die "--feishu-app-secret-file 需要一个值"; FEISHU_APP_SECRET_FILE="$2"; shift 2 ;;
    --feishu-domain) [ $# -ge 2 ] || die "--feishu-domain 需要一个值"; FEISHU_DOMAIN="$2"; shift 2 ;;
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
# 补丁脚本是 Python，必须用 python3 喂，不能用 sh。
#
# 原来走 `sh -s`：sh 逐行解释 Python 源码必然抛错，而清理 /tmp 里两份明文
# 密钥的代码就在那个跑不起来的脚本里——于是 configure 表面「成功」，guest 的
# /tmp 永久留下可读的 LLM key 和飞书 secret。agentvm.py 的 configure 有同一个
# 错，两边一起改。
#
# 两个注入面一起堵：
#   1. 环境变量经 env 直接进 argv，不经过任何 shell 解析，也就不需要 %q。
#      原来那套 printf %q 是为了让值能被 shell 安全解析一次，现在没有 shell
#      这一层了，转义反而多余。
#   2. 脚本用重定向喂而不是未加引号的 heredoc。heredoc 会把补丁里的 $ 和
#      反引号交给宿主机先展开一遍，那是原来就有的第二个注入面。
"$TART_BIN" exec "$VM_NAME" command -v python3 >/dev/null 2>&1 \
	|| die "客人机里没有 python3，配置装不进去。先在客人机里 apt install python3。"
"$TART_BIN" exec "$VM_NAME" env \
	AGENTVM_LLM_BASE_URL="$LLM_BASE_URL" \
	AGENTVM_LLM_MODEL="$LLM_MODEL" \
	AGENTVM_FEISHU_APP_ID="$FEISHU_APP_ID" \
	AGENTVM_FEISHU_DOMAIN="$FEISHU_DOMAIN" \
	AGENTVM_YOLO="$YOLO" \
	python3 - < "$CONFIG_PATCH"

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
