#!/bin/sh
# 抓「全角标点紧跟变量名」——bash -n 查不出来的那一类。
#
# 症状：脚本在某个 warn/die 行上崩，报的是
#     ./install.sh: line 100: APP_DST<0xEF>: unbound variable
# 而那一行看着只是 `warn "已存在 $APP_DST，覆盖"`。
#
# 原因：`，` 是 U+FF0C，UTF-8 三字节 EF BC 8C。bash 的变量名规则本来只认
# [A-Za-z0-9_]，但 LC_CTYPE 是 UTF-8 时高位字节被它当成名字的一部分——
# `$APP_DST，覆盖` 解析成一个叫 APP_DST<U+FF0C> 的变量，set -u 判它未绑定，
# 整个脚本退出。2026-11-10 实测：install.sh 因此在把 AgentVM.app 拷进
# /Applications 之前一步退出，而前面 QEMU 检查、vmz 编译全打了 ok。
# 详见 docs/PITFALLS.md M20。
#
# 判据：没有花括号的 $VAR 后面紧跟一个非 ASCII 字节。注释行不算——那是文档，
# 不会执行。
set -eu

cd "$(dirname "$0")/.."

python3 -I - <<'PY'
import pathlib
import re
import sys

# (?<!\{) 排除 ${VAR}；(?=\}) 排除 ${VAR} 的闭合括号。
pat = re.compile(r'(?<!\{)\$([A-Za-z_][A-Za-z0-9_]*)(?!\})(?=[^\x00-\x7f])')

# 只看 shell。scripts/lib/*.sh 也在内，还有两个没有 .sh 后缀的大文件。
targets = sorted(pathlib.Path(".").glob("scripts/*.sh"))
targets += sorted(pathlib.Path(".").glob("scripts/lib/*.sh"))
for name in ("install.sh", "app/build.sh"):
    targets.append(pathlib.Path(name))
for name in ("scripts/agentvm-fetch", "scripts/agentvm-serve"):
    targets.append(pathlib.Path(name))

bad = []
for p in targets:
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        continue
    for n, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue
        if pat.search(line):
            bad.append(f"{p}:{n}: {line.strip()[:100]}")

if bad:
    sys.stderr.write("全角标点紧跟变量名，bash 会把它并进变量名（set -u 下直接崩）。"
                     "改成 ${VAR}：\n")
    for b in bad:
        sys.stderr.write("  " + b + "\n")
    sys.stderr.write("详见 docs/PITFALLS.md M20\n")
    raise SystemExit(1)
print("  ok 没有全角标点紧跟变量名")
PY
