#!/bin/sh
# 把 app/Sources 编成 AgentVM.app。
#
# 不用 Xcode 工程，直接 swiftc 一条命令。原因：这个 app 是给普通 Mac 用户
# 一键装的，不能要求对方先装 Xcode；而 macOS 自带的 Command Line Tools 里
# 就有 swiftc，编 SwiftUI 完全够。
#
# 三个必须点：
#   1. -parse-as-library。源码里有 @main，不写这个 Swift 会报
#      "'main' attribute cannot be used in a module that contains top-level code"。
#   2. 后端脚本要一起打进 Resources/backend/。app 是自包含的，不依赖
#      用户机器上有没有这个仓库。
#   3. 编完必须 ad-hoc 签名。不签的 app 在 macOS 上会被 Gatekeeper 拦，
#      表现为「已损坏，应移到废纸篓」，而实际上文件是好的。
set -eu

cd "$(dirname "$0")/.."
ROOT=$(pwd)
APP="$ROOT/AgentVM.app"
BIN="$APP/Contents/MacOS/AgentVM"
RES="$APP/Contents/Resources"

SDK=$(xcrun --sdk macosx --show-sdk-path)
# 部署目标取 14。NavigationSplitView 要 13，Grid 要 13，这里用的几个控件
# 都在 14 之前就有。定低一点覆盖面广，不追求新 API。
TARGET="arm64-apple-macos14.0"

echo "==> 清理旧构建"
rm -rf "$APP"

# 目录要先建。swiftc 的 -o 不会自己创建父目录，先 rm 再编会直接
# "open() failed, errno=2"。
mkdir -p "$APP/Contents/MacOS" "$RES/backend"

echo "==> 编译 Swift"
swiftc \
    -O \
    -parse-as-library \
    -sdk "$SDK" \
    -target "$TARGET" \
    -o "$BIN" \
    "$ROOT"/app/Sources/*.swift

echo "==> 建 bundle 结构"
mkdir -p "$APP/Contents/MacOS" "$RES/backend"

echo "==> 拷后端（vmkit.py + winarm-vm.py + catalog.json）"
cp "$ROOT/scripts/vmkit.py"          "$RES/backend/"
cp "$ROOT/scripts/winarm-vm.py"      "$RES/backend/"
cp "$ROOT/scripts/catalog.json"      "$RES/backend/"
chmod 755 "$RES/backend/vmkit.py" "$RES/backend/winarm-vm.py"

echo "==> 写 Info.plist"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>zh_CN</string>
    <key>CFBundleDisplayName</key>
    <string>AgentVM</string>
    <key>CFBundleExecutable</key>
    <string>AgentVM</string>
    <key>CFBundleIdentifier</key>
    <string>com.agentvm.app</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>AgentVM</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticTermination</key>
    <false/>
</dict>
</plist>
PLIST

echo "==> ad-hoc 签名"
# -s - 是 ad-hoc 签名，不上传、不联网。签名后 app 才能在本机直接打开。
# force 是因为 bundle 里的脚本没有签名，混着会签不上。
#
# 原来写成 `codesign ... 2>&1 | sed 's/^/    /' || { echo 签名失败; }`。
# `||` 判的是 sed 的退出码，而 sed 几乎总是成功——实测 `false | sed ... || echo X`
# 不打印 X。set -eu 也救不了：管道里只有最后一条命令的退出码算数。结果是
# codesign 真失败时脚本照常往下走、最后打印「完成」，用户装上去被 Gatekeeper
# 拦成「已损坏」，而构建日志里一个字都没提签名。改成先存退出码再判断。
sign_err=$(mktemp)
trap 'rm -f "$sign_err"' EXIT
if codesign --force --deep -s - "$APP" >"$sign_err" 2>&1; then
  sed 's/^/    /' "$sign_err"
else
  sign_status=$?
  sed 's/^/    /' "$sign_err"
  echo "    签名失败（退出码 $sign_status）。app 在 macOS 上会被拦成「已损坏，应移到废纸篓」。"
  exit 1
fi

echo "==> 校验"
[ -x "$BIN" ] || { echo "二进制没生成"; exit 1; }
plutil -lint "$APP/Contents/Info.plist" >/dev/null
# 用 ast.parse 而不是 py_compile：后者会在 Resources/backend/ 里留下
# __pycache__ 目录，把编译产物塞进交付物里。
python3 - "$RES/backend/vmkit.py" "$RES/backend/winarm-vm.py" <<'PY'
import ast, sys
for f in sys.argv[1:]:
    ast.parse(open(f, encoding="utf-8").read(), filename=f)
print("    python 语法 ok")
PY
# 万一之前留下过缓存，清掉
rm -rf "$RES/backend/__pycache__"

echo ""
echo "完成：$APP"
echo "大小：$(du -sh "$APP" | cut -f1)"
echo "试运行：open \"$APP\""
