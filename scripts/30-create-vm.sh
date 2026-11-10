#!/usr/bin/env bash
# agentvm-create-vm — clone the local image into a VM and size it.
#
# Two things worth knowing before you edit the resource numbers:
#
#  1. Tart's official images carry a fixed logical disk size in the manifest
#     annotation `org.cirruslabs.tart.uncompressed-disk-size` (20 GB for
#     cirruslabs/* images). `tart set --disk-size` can only *grow* a disk, never
#     shrink it, so you cannot make a cloned VM smaller than its image.
#     The disk is APFS-sparse, so logical size is not the same as bytes on disk:
#     a 20 GB logical Debian guest typically occupies 2-3 GB actually.
#
#  2. Memory and CPU are freely adjustable and take effect at next boot.
#
# Requires the local registry to be running (agentvm-serve in another terminal).

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

PORT="${LOCAL_REGISTRY_PORT}"
SOURCE="127.0.0.1:${PORT}/${LOCAL_REPO_NAME}:latest"

while [ $# -gt 0 ]; do
  case "$1" in
    --name) [ $# -ge 2 ] || die "--name 需要一个值"; VM_NAME="$2"; shift 2 ;;
    --cpu) [ $# -ge 2 ] || die "--cpu 需要一个值"; VM_CPU="$2"; shift 2 ;;
    --memory) [ $# -ge 2 ] || die "--memory 需要一个值"; VM_MEMORY_MB="$2"; shift 2 ;;
    --port) [ $# -ge 2 ] || die "--port 需要一个值"; PORT="$2"; SOURCE="127.0.0.1:${PORT}/${LOCAL_REPO_NAME}:latest"; shift 2 ;;
    --from) [ $# -ge 2 ] || die "--from 需要一个值"; SOURCE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -x "$TART_BIN" ] || die "tart not found at $TART_BIN — run scripts/10-install-tart.sh first"

# Fail before the (slow) clone if the registry isn't answering.
if ! curl -fsS "http://127.0.0.1:${PORT}/v2/" >/dev/null 2>&1; then
  die "no registry on 127.0.0.1:${PORT}. Start it first:
         scripts/agentvm-serve --port $PORT &"
fi
ok "registry reachable on 127.0.0.1:${PORT}"

# tart list 的第一列是 NAME，第二列是 SOURCE，所以先用 awk 取出每行第一列。
# 关键是不能再 `"$TART_BIN" list | awk | grep -Fqx`：set -o pipefail 下 grep -q
# 命中后立刻关掉管道读端，上游 tart/awk 接着写会吃到 SIGPIPE，整条管道返回 141
# ——tart list 输出超过管道缓冲时，明明命中「已存在」却返回非零而走 else 重新
# clone，tart clone 报 already exists，set -e 让脚本死在一句看不懂的错误上。
# 存进变量就没有这条管道了；tr 把换行也变成空格，好用 `" $VM_NAME "` 按整词匹配
# ——等价于原来的 grep -Fqx，VM 名里的 `.` 不会误匹配（`agent` 不对上 `agentx`）。
vms="$("$TART_BIN" list 2>/dev/null | awk '{print $1}' | tr '\n' ' ' || true)"
case " $vms " in
  *" $VM_NAME "*)
    info "VM '$VM_NAME' already exists; skipping clone" ;;
  *)
    info "cloning $SOURCE -> $VM_NAME (uses APFS copy-on-write, so this is fast)"
    # --insecure is required: Tart refuses plain-HTTP registries without it.
    "$TART_BIN" clone "$SOURCE" "$VM_NAME" --insecure --concurrency 16
    ok "cloned" ;;
esac

info "setting resources: ${VM_CPU} CPU / ${VM_MEMORY_MB} MB"
"$TART_BIN" set "$VM_NAME" --cpu "$VM_CPU" --memory "$VM_MEMORY_MB"

echo >&2
"$TART_BIN" get "$VM_NAME"
echo >&2
ok "VM '$VM_NAME' ready. Start it with: tart run $VM_NAME --no-graphics"
