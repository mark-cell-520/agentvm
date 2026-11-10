#!/usr/bin/env bash
# agentvm-install-tart — install Tart from its release tarball.
#
# Tart ships as a .app bundle containing the CLI binary. The supported layout is
# the app in /Applications (or ~/Applications) with a symlink to the binary on
# PATH, because Tart expects to find its own bundle for guest-tool signing.
#
# Homebrew is deliberately NOT used: it needs to clone Homebrew/brew and
# homebrew-core from github.com, which is exactly the host this project exists
# to work around.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

PREFIX="${AGENTVM_PREFIX:-$HOME/.local}"
APPDIR="${AGENTVM_APPDIR:-$HOME/Applications}"
VERSION="${TART_VERSION:-}"
# 用户自备的 tart.tar.gz 本地路径。给了它就连网都不上，直接解压。
TARBALL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --version) [ $# -ge 2 ] || die "--version 需要一个值"; VERSION="$2"; shift 2 ;;
    --prefix) [ $# -ge 2 ] || die "--prefix 需要一个值"; PREFIX="$2"; shift 2 ;;
    --appdir) [ $# -ge 2 ] || die "--appdir 需要一个值"; APPDIR="$2"; shift 2 ;;
    --from) [ $# -ge 2 ] || die "--from 需要一个值"; TARBALL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

need_cmd curl
need_cmd tar

mkdir -p "$PREFIX/bin" "$APPDIR"

# Resolve the release via api.github.com, which is reachable on networks where
# github.com itself is throttled. Fall back to the browser URL if that fails.
resolve_release() {
  local api="https://api.github.com/repos/openai/tart/releases"
  [ -n "$VERSION" ] && api="$api/tags/$VERSION" || api="$api/latest"
  curl -fsSL "$api" 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
tag = d.get("tag_name")
for a in d.get("assets", []):
    if a["name"] == "tart.tar.gz":
        print(tag, a["id"], a["size"])
        break
' || true
}

info "resolving Tart release${VERSION:+ $VERSION}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/agentvm-tart.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# --from 给的本地压缩包是 api.github.com 被墙时唯一的恢复路径。给了它就完全
# 不联网：跳过 resolve_release、下载和 checksum（包是用户自己下的，没有官方
# sums 可比），直接进解压。原来 case 里没有 --from 分支，58 行的 die 让人
# "re-run with --from" 却回一句 unknown argument——恢复路径是个死胡同。
if [ -n "$TARBALL" ]; then
    [ -f "$TARBALL" ] || die "找不到压缩包：$TARBALL"
    cp "$TARBALL" "$WORK/tart.tar.gz"
    ok "用本地压缩包 $TARBALL（跳过联网）"
else
    read -r TAG ASSET_ID ASSET_SIZE < <(resolve_release) || true

    if [ -z "${TAG:-}" ]; then
      die "could not resolve a Tart release via api.github.com.
           Set TART_VERSION explicitly, or download tart.tar.gz yourself and
           re-run with: $0 --from /path/to/tart.tar.gz"
    fi
    info "Tart $TAG (asset $ASSET_ID, $(bytes_human "$ASSET_SIZE"))"

    curl -fL --progress-bar \
      -H "Accept: application/octet-stream" \
      "https://api.github.com/repos/openai/tart/releases/assets/$ASSET_ID" \
      -o "$WORK/tart.tar.gz"

    # The release publishes a checksums file; verify before extracting anything.
    SUM_ASSET="$(curl -fsSL "https://api.github.com/repos/openai/tart/releases/tags/$TAG" 2>/dev/null \
      | python3 -c '
import sys, json
d = json.load(sys.stdin)
for a in d.get("assets", []):
    if a["name"].endswith("checksums.txt"):
        print(a["id"]); break
' || true)"

    if [ -n "$SUM_ASSET" ]; then
      curl -fsSL -H "Accept: application/octet-stream" \
        "https://api.github.com/repos/openai/tart/releases/assets/$SUM_ASSET" -o "$WORK/sums.txt"
      want="$(awk '/tart\.tar\.gz/{print $1}' "$WORK/sums.txt")"
      # sums.txt 里没有 tart.tar.gz 那一行时 want 为空，下一行会报
      # "checksum mismatch: got <hash>, want "——听起来像哈希不符（像安全事件），
      # 实际只是文件名没匹配上。先确认 want 非空再比。
      [ -n "$want" ] || die "在 sums.txt 里找不到 tart.tar.gz 那一行"
      got="$(shasum -a 256 "$WORK/tart.tar.gz" | cut -d' ' -f1)"
      [ "$want" = "$got" ] || die "checksum mismatch: got $got, want $want"
      ok "tarball verified against published checksum"
    else
      warn "no checksums asset found; proceeding unverified"
    fi
fi

info "extracting"
tar xzf "$WORK/tart.tar.gz" -C "$WORK"
[ -d "$WORK/tart.app" ] || die "tart.app missing from tarball"

rm -rf "$APPDIR/tart.app"
mv "$WORK/tart.app" "$APPDIR/tart.app"

# Tart refuses to run if its bundle signature is broken; verify before linking.
codesign --verify --deep --strict "$APPDIR/tart.app" 2>/dev/null \
  && ok "bundle signature valid" \
  || warn "bundle signature could not be verified (ad-hoc or restricted environment)"

# tart.app 的 bundle 布局未来可能变。符号链接目标写死 Contents/MacOS/tart，
# 布局一变链接就悬空，下面 tart --version 失败，末尾的 die 让人去查签名和
# quarantine——方向完全错。先确认链接目标真的在那儿。
[ -x "$APPDIR/tart.app/Contents/MacOS/tart" ] \
  || die "tart.app 布局变了：找不到 $APPDIR/tart.app/Contents/MacOS/tart"
ln -sfn "$APPDIR/tart.app/Contents/MacOS/tart" "$PREFIX/bin/tart"
ok "linked $PREFIX/bin/tart"

case ":$PATH:" in
  *":$PREFIX/bin:"*) ok "$PREFIX/bin already on PATH" ;;
  *) warn "add $PREFIX/bin to your PATH, or call $PREFIX/bin/tart directly" ;;
esac

# 原来写 `tart --version && ok "Tart installed"`。签名损坏时 tart 被 macOS
# SIGKILL（退出码 137），&& 短路，set -e 让脚本以 137 静默结束——上一行才刚刚
# warn 过「bundle signature could not be verified」，用户却只看到安装脚本莫名失败，
# 没有任何指向签名的诊断。
if "$PREFIX/bin/tart" --version; then
  ok "Tart installed"
else
  rc=$?
  die "tart 装好了但跑不起来（退出码 $rc）。通常是签名/重定位问题："
      "    xattr -dr com.apple.quarantine '$APPDIR/tart.app'"
      "    codesign --force --deep -s - '$APPDIR/tart.app'"
      "  详见 docs/WINDOWS-ARM-ON-MACOS.md 的「改完必须重签」。"
fi
