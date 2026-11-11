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

# 临时文件在这儿建、统一清。原来 vmz_log 的定义在 140 行而第一次使用在
# 67 行——set -u 下直接 "unbound variable" 退出，而报错行号离原因很远。
vmz_log=$(mktemp)
sign_err=$(mktemp)
trap 'rm -f "$vmz_log" "$sign_err"' EXIT

# die() 必须自己定义。install.sh 里有一个，但 `sh build.sh` 是独立进程，不继承
# 函数。原来第 31 行直接调了 install.sh 那个 die：没装 Xcode 命令行工具时输出
# 的是 "build.sh: line 31: die: command not found"，退出码 127，而那句
# 「需要 Xcode 命令行工具：xcode-select --install」一个字符都不出现——它正是
# 这一行唯一的目的。set -e 也救不了：|| 列表的退出码是 127，照样退出。
# 实测用一个坏 SDK 名复现过。2026-11-11 审计补上。
die() {
    echo "build.sh: $*" >&2
    exit 1
}

# 没装命令行工具时 xcrun 报裸错误、set -e 直接退出，用户看不到「要装工具」。
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) \
  || die "需要 Xcode 命令行工具：xcode-select --install"
# vmz 编到仓库里的临时位置，再拷进 app 的 Resources。不进 git
# （.gitignore 里有 vmz 一行），装机时 install.sh 会另编一份到
# ~/.agentvm/bin/vmz 给命令行用。
VMBIN="$ROOT/scripts/vmz/vmz"
# 部署目标取 14。NavigationSplitView 要 13，Grid 要 13，这里用的几个控件
# 都在 14 之前就有。定低一点覆盖面广，不追求新 API。
TARGET="arm64-apple-macos14.0"

echo "==> 清理旧构建"
rm -rf "$APP"
# vmz 必须一起删。原来只删 $APP：一次编译被 Ctrl-C / 磁盘满 / OOM 打断时，
# ld 可能已经建好了 0755 的输出文件而内容不完整。下一次 build 看到
# `[ -x "$VMBIN" ]` 为真就跳过编译，把那个垃圾文件原样拷进 app——
# 实测把一个 24 字节的假二进制放进去，build.sh 全程零警告，
# app 里那个 vmz 跑起来是「command not found」。
rm -f "$VMBIN"

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

echo "==> 编 vmz（macOS 虚拟机后端）"
# macOS 客人机跑在 Virtualization.framework 上，那套 API 只有 Swift/ObjC，
# 所以后端除了 vmkit.py 还有一个 Swift 小程序。它和 app 共用
# app/Sources/MacVMEngine.swift——同一份代码，一个走 CLI 一个走 UI。
# 编译日志先落文件再判退出码，不能 `swiftc ... | sed`：管道里 $? 是 sed 的，
# 而 sed 几乎总是成功——实测 `if false | sed >/dev/null; then` 走 THEN 分支。
# 结果是 swiftc 真失败时脚本打印「vmz 编好了」然后往下走。
if swiftc -O -sdk "$SDK" -target "$TARGET" \
        -o "$VMBIN" \
        "$ROOT/app/Sources/MacVMEngine.swift" \
        "$ROOT/scripts/vmz/main.swift" >"$vmz_log" 2>&1; then
    sed 's/^/    /' "$vmz_log"
    echo "    vmz 编好了"
else
    vmz_status=$?
    sed 's/^/    /' "$vmz_log"
    echo "    vmz 编译失败（退出码 ${vmz_status}）。Windows/Linux 不受影响，macOS 虚拟机用不了。"
    rm -f "$VMBIN"
fi
# 拷进 app 之前先确认它真是个 Mach-O。上面已经 rm 过残文件，这里是第二道：
# 万一编译「成功」而输出不是可执行文件（磁盘满时 ld 会留一个空文件），
# 拷进去用户拿到的是一个跑不起来的 vmz，而报错在很远的将来。
if [ -f "$VMBIN" ]; then
    # file 失败也要和「不是 Mach-O」分开：脚本是 /bin/sh、没有 pipefail，
    # `file "$VMBIN" | grep -q` 里 file 挂了会被完全吞掉，grep 拿空输入返回 1，
    # 于是走 else 把「file 失败了」说成「二进制无效」。先把 file 的输出存下来再判。
    if out="$(file "$VMBIN" 2>/dev/null)" && printf '%s' "$out" | grep -q "Mach-O"; then
        cp "$VMBIN" "$RES/backend/vmz"
        chmod 755 "$RES/backend/vmz"
    else
        echo "    vmz 不是有效二进制，跳过（macOS 虚拟机用不了）"
        rm -f "$VMBIN"
    fi
fi

echo "==> 拷后端（vmkit.py + winarm-vm.py + autoinstall.py + catalog.json + lib/ + vmz）"
cp "$ROOT/scripts/vmkit.py"          "$RES/backend/"
cp "$ROOT/scripts/winarm-vm.py"      "$RES/backend/"
# autoinstall.py 必须一起拷。vmkit.py 用 importlib 按**自己所在目录**加载它，
# 而 app 里 vmkit.py 在 Resources/backend/ 下——少拷这一个文件，从 app 里装
# 自己的安装包会直接 FileNotFoundError，而命令行下手工跑的一条不少。这种
# 「app 里整条路不能用」的差距在单元测试里看不出来，只有真去装才发现。
cp "$ROOT/scripts/autoinstall.py"    "$RES/backend/"
cp "$ROOT/scripts/catalog.json"      "$RES/backend/"
# scripts/lib/ 必须一起拷进来。vmkit.py 现在 import 这里的 qga.py（客人机
# 代理客户端），少拷一个目录的后果是：从 AgentVM.app 里起的 Linux 虚拟机
# 全都用不上 guest agent，而宿主机 scripts/ 目录下手工跑的一条不少——
# 这种「app 里少一半功能」的差距在测试里看不出来，只有用户报「关机不优雅」
# 才发现。vmkit.py 侧已用 AGENTVM_QGA_LIB 兜底并告警，这里是根治。
mkdir -p "$RES/backend/lib"
cp "$ROOT"/scripts/lib/*.py          "$RES/backend/lib/"
chmod 755 "$RES/backend/vmkit.py" "$RES/backend/winarm-vm.py" \
	"$RES/backend/autoinstall.py"
find "$RES/backend/lib" -name '*.py' -exec chmod 644 {} +

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

# 嵌套的 vmz 要先单独签。原来只签 bundle，而 --deep 不会把 entitlement
# 传给 Contents/Resources/backend/vmz：实测签完 bundle 之后查那个文件，
# `codesign -d --entitlements -` 只有一行 Executable=，没有 entitlement 字典。
# 而 Virtualization.framework 的 macOS 客人机 API 全都要
# com.apple.security.virtualization——少了它 VZ 抛的是
# NSInvalidArgumentException（未捕获的 ObjC 异常，进程直接 abort），
# vmkit 只会看到 vmz 非零退出、拿不到任何 JSON。
# 顺序是先内后外：先签嵌套二进制，再签 bundle，两步之后
# `codesign --verify --deep --strict` 仍然通过。
if [ -f "$RES/backend/vmz" ]; then
    if ! codesign -s - --entitlements "$ROOT/app/Resources/entitlements.plist" \
            --force "$RES/backend/vmz" >"$sign_err" 2>&1; then
        sed 's/^/    /' "$sign_err"
        # 原来只 echo 一句 warn 就继续：bundle 照样签、照样「完成」，用户拿到一个
        # macOS 虚拟机一启动就 abort 的 app，而构建日志里没有任何失败信号。嵌套
        # vmz 签不上必须让整个构建失败，不能带着它往下走。
        echo "    嵌套 vmz 签名失败，构建中止（app 里起 macOS 虚拟机会直接崩）。"
        exit 1
    fi
    # 签完要验。签名命令成功不等于 entitlement 真的在里面。
    if ! codesign -d --entitlements - "$RES/backend/vmz" 2>/dev/null \
            | grep -q "com.apple.security.virtualization"; then
        echo "    vmz 里没有 virtualization entitlement，macOS 虚拟机起不来。"
    fi
fi

echo "==> ad-hoc 签名"
# -s - 是 ad-hoc 签名，不上传、不联网。签名后 app 才能在本机直接打开。
# force 是因为 bundle 里的脚本没有签名，混着会签不上。
#
# 原来写成 `codesign ... 2>&1 | sed 's/^/    /' || { echo 签名失败; }`。
# `||` 判的是 sed 的退出码，而 sed 几乎总是成功——实测 `false | sed ... || echo X`
# 不打印 X。set -eu 也救不了：管道里只有最后一条命令的退出码算数。结果是
# codesign 真失败时脚本照常往下走、最后打印「完成」，用户装上去被 Gatekeeper
# 拦成「已损坏」，而构建日志里一个字都没提签名。改成先存退出码再判断。
# --entitlements 不能省：Virtualization.framework 的 macOS 客人机 API
# 全都要 com.apple.security.virtualization，少了它 VZMacOSRestoreImage
# 只会说「restore image catalog failed to load」，看不出是权限问题。
# --deep 保留：bundle 里还有未签名的脚本。
if codesign --force --deep --entitlements "$ROOT/app/Resources/entitlements.plist" \
        -s - "$APP" >"$sign_err" 2>&1; then
  sed 's/^/    /' "$sign_err"
else
  sign_status=$?
  sed 's/^/    /' "$sign_err"
  echo "    签名失败（退出码 ${sign_status}。app 在 macOS 上会被拦成「已损坏，应移到废纸篓」。"
  exit 1
fi

echo "==> 校验"
[ -x "$BIN" ] || { echo "二进制没生成"; exit 1; }
plutil -lint "$APP/Contents/Info.plist" >/dev/null
# 用 ast.parse 而不是 py_compile：后者会在 Resources/backend/ 里留下
# __pycache__ 目录，把编译产物塞进交付物里。
# lib/*.py 一起查——qga.py 有语法错的话 vmkit.py 每次 import 才炸，
# 那时候错误信息指向的是 import 那一行，不是真正写错的那一行。
python3 - "$RES/backend/vmkit.py" "$RES/backend/winarm-vm.py" \
	"$RES/backend/autoinstall.py" "$RES"/backend/lib/*.py <<'PY'
import ast, sys
for f in sys.argv[1:]:
	ast.parse(open(f, encoding="utf-8").read(), filename=f)
print("    python 语法 ok")
PY
# 万一之前留下过缓存，清掉。lib/ 下也查一遍：少清一处，下一次构建的
# 「清缓存」就等于没清。
rm -rf "$RES/backend/__pycache__" "$RES/backend/lib/__pycache__"

echo ""
echo "完成：$APP"
echo "大小：$(du -sh "$APP" | cut -f1)"
echo "试运行：open \"$APP\""
