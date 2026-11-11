#!/usr/bin/env bash
# Shared helpers for agentvm scripts. Source this, don't execute it.
#
# Design notes:
#   - `set -euo pipefail` everywhere; every script is meant to be re-runnable.
#   - Secrets are never passed as command-line arguments (they show up in `ps`).
#     They travel via files with 0600 permissions, or via base64 through stdin.
#   - Every external tool invocation checks its exit status and says what failed.

set -euo pipefail

AGENTVM_VERSION="0.1.0"

# ---------------------------------------------------------------- colours ---
if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RST=$'\033[0m'; C_RED=$'\033[31m'; C_GRN=$'\033[32m'
  C_YEL=$'\033[33m'; C_BLU=$'\033[34m'; C_DIM=$'\033[2m'
else
  C_RST=""; C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_DIM=""
fi

info()  { printf '%s==>%s %s\n'  "$C_BLU" "$C_RST" "$*" >&2; }
ok()    { printf '%s  ok%s %s\n' "$C_GRN" "$C_RST" "$*" >&2; }
warn()  { printf '%s  warn%s %s\n' "$C_YEL" "$C_RST" "$*" >&2; }
die()   { printf '%s  error%s %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# 打印脚本头部的用法注释：从第 2 行打到第一个非注释行为止，然后按给定的
# 退出码结束（默认 0，出错用法传 1）。
#
# 原来每个脚本都写 `sed -n '2,26p' "$0"`，行号是手数的。注释一改行数就漂，
# --help 开始把 `set -euo pipefail`、`HERE="$(cd ...)"` 这些真实代码一起打出来
# （实测 `bash scripts/10-install-tart.sh --help` 末尾混着三行代码）。用 awk
# 按「遇到非注释行就停」来切，改注释不会再破。
usage() {
  local f="${BASH_SOURCE[1]:-$0}"
  awk 'NR > 1 { if ($0 ~ /^#/) { sub(/^# ?/, ""); print } else { exit } }' "$f" >&2
  exit "${1:-0}"
}

# Resolve the repo root regardless of where a script is invoked from.
# 只在 bash 下有效：zsh 里没有 BASH_SOURCE。各脚本 shebang 都是 bash，正常
# 执行走不到这里；只有被人 `source` 进 zsh 时才中招，那时至少说清楚，别静默
# 返回空、再让 `cd ""` 给出一个错的根目录。
agentvm_root() {
  if [ -z "${BASH_SOURCE[1]:-}" ]; then
    die "agentvm_root 只在 bash 下有效（这个项目不支持 zsh source）"
  fi
  cd "$(dirname "${BASH_SOURCE[1]}")/.." && pwd
}

# Human-readable byte count.
bytes_human() {
  local b="${1:-0}"
  if   [ "$b" -ge $((1024**3)) ]; then awk -v n="$b" 'BEGIN{printf "%.1f GiB", n/1073741824}'
  elif [ "$b" -ge $((1024**2)) ]; then awk -v n="$b" 'BEGIN{printf "%.1f MiB", n/1048576}'
  elif [ "$b" -ge 1024 ];        then awk -v n="$b" 'BEGIN{printf "%.1f KiB", n/1024}'
  else printf '%s B' "$b"; fi
}

# ------------------------------------------------------------------ config ---
# Defaults; override via environment or a .agentvmrc in the repo root.
#
# 顺序要紧：先 load_rc 读 .agentvmrc，再用 `:=` 补默认值。反过来的话 rc 里
# 设的值会被 `:=` 当成「已经有人设过了」而保留默认值——用户以为自己的
# VM_NAME 生效了，实际用的还是 agent，而且没有任何提示。
# 原来 load_rc 定义了却七个脚本一个都没调用，rc_file 是死代码，
# config/agentvmrc.example 里那一整页开关全是摆设。现在 source 本文件就会读 rc。

load_rc() {
  local root
  root="$(cd "$(dirname "${BASH_SOURCE[1]:-$0}")/.." && pwd)"
  if [ -f "$root/.agentvmrc" ]; then
    # shellcheck disable=SC1090
    . "$root/.agentvmrc"
    info "loaded $root/.agentvmrc"
  fi
}

load_rc

: "${AGENTVM_HOME:=$HOME/.agentvm}"
: "${VM_NAME:=agent}"
: "${VM_CPU:=2}"
: "${VM_MEMORY_MB:=1024}"
: "${VM_DISK_GB:=10}"          # logical target; see docs/PITFALLS.md
: "${TART_BIN:=$HOME/.local/bin/tart}"
: "${IMAGE_REPO:=ghcr.io/cirruslabs/debian}"
: "${IMAGE_TAG:=latest}"
: "${LOCAL_REGISTRY_PORT:=5000}"
: "${LOCAL_REPO_NAME:=debian}"

# ------------------------------------------------------------- secret safe ---
# Write stdin to a 0600 file. Never put a secret in an argv.
write_secret() {
  local path="$1"
  local old_umask; old_umask="$(umask)"
  umask 077
  cat > "$path"
  chmod 600 "$path"
  # umask 是进程级的，改完必须还原。原来不还原，之后这个脚本建的所有目录和
  # 文件都变成 700/600——包括本该 755 的安装目录，症状是「装好了但别人读不到」。
  umask "$old_umask"
}

# Transport an arbitrary string into a guest/file without it appearing in argv
# in a form that secret-scanners mangle. Encodes to base64, decodes on arrival.
#
# 这里原来有一个 b64_into_guest()，2026-11-11 审计删掉了，理由是两条：
#   1. 全仓库没有任何调用方，是死代码；
#   2. 它做的正是 agentvm.py:278-286 的注释明确否定的那个做法——把 base64 后的
#      值拼进 tart exec 的 argv。base64 不是加密，进程表里可还原，而那处注释
#      写的是「原项目把 base64 后的值放进 tart exec 的 argv……所以这里严一档」。
# 秘密进客人机的正确通道是 stdin：agentvm.py 的 _push_secret 和
# backends/*.py 的 exec_vm_script（input=script）都是这么做的。
