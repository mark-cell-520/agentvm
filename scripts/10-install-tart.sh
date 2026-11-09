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

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --appdir) APPDIR="$2"; shift 2 ;;
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
read -r TAG ASSET_ID ASSET_SIZE < <(resolve_release) || true

if [ -z "${TAG:-}" ]; then
  die "could not resolve a Tart release via api.github.com.
       Set TART_VERSION explicitly, or download tart.tar.gz yourself and
       re-run with: $0 --from /path/to/tart.tar.gz"
fi
info "Tart $TAG (asset $ASSET_ID, $(bytes_human "$ASSET_SIZE"))"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/agentvm-tart.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

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
  got="$(shasum -a 256 "$WORK/tart.tar.gz" | cut -d' ' -f1)"
  [ "$want" = "$got" ] || die "checksum mismatch: got $got, want $want"
  ok "tarball verified against published checksum"
else
  warn "no checksums asset found; proceeding unverified"
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
