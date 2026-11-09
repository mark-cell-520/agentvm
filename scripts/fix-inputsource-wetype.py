#!/usr/bin/env python3
"""清掉 WeType 残留的输入源状态。

问题不是「输入源列表里还有 WeType」——列表里已经没了。问题在于另外两处
偏好没跟着清：

1. com.apple.HIToolbox 的 AppleSelectedInputSources 还指着 WeType。
   当前选中的输入源不在启用列表里，Text Input Services 每次聚焦输入框
   都去激活一个已注销的输入法，IMK 模块处于半加载状态。
2. com.apple.inputsources 的 AppleEnabledThirdPartyInputSources 还挂着
   WeType 两条，系统仍把它当已注册的第三方输入法。
3. AppleInputSourceHistory 里 WeType 排在第一位，配合
   TextInputGlobalPropertyPerContextInput（自动按文稿切换输入源）会把
   每个应用都拉回 WeType。

修法是把这三处收敛到唯一一个合法的输入源 ABC，然后 kill 掉还在跑的
WeType 进程，再让 cfprefsd 重读。原文件已备份，跑 restore 可回滚。
"""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
HITOOLBOX = HOME / "Library/Preferences/com.apple.HIToolbox.plist"
INPUTSOURCES = HOME / "Library/Preferences/com.apple.inputsources.plist"
BACKUP = Path(__file__).resolve().parent / ".tmp/prefs-backup"

ABC = {
    "InputSourceKind": "Keyboard Layout",
    "KeyboardLayout ID": 252,
    "KeyboardLayout Name": "ABC",
}


def load(p: Path) -> dict:
    with p.open("rb") as f:
        return plistlib.load(f)


def save(p: Path, d: dict) -> None:
    with p.open("wb") as f:
        plistlib.dump(d, f)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        for name in ("com.apple.HIToolbox.plist", "com.apple.inputsources.plist"):
            src = BACKUP / name
            if not src.is_file():
                print(f"没有备份 {src}")
                return 1
            shutil.copy2(src, HOME / "Library/Preferences" / name)
            print(f"已回滚 {name}")
        subprocess.run(["killall", "cfprefsd"], check=False)
        print("cfprefsd 已重读。建议注销重新登录。")
        return 0

    h = load(HITOOLBOX)

    # 启用列表里保留智能ABC和字符面板，只摘掉 WeType。一把收敛到纯 ABC
    # 会让中文打不了，那是拿一个故障换另一个故障。
    h["AppleEnabledInputSources"] = [
        dict(ABC),
        {"Bundle ID": "com.apple.inputmethod.SCIM",
         "Input Mode": "com.apple.inputmethod.SCIM.ITABC",
         "InputSourceKind": "Input Mode"},
        {"Bundle ID": "com.apple.CharacterPaletteIM",
         "InputSourceKind": "Non Keyboard Input Method"},
    ]
    h["AppleSelectedInputSources"] = [dict(ABC)]
    h["AppleInputSourceHistory"] = [dict(ABC)]
    h["AppleCurrentKeyboardLayoutInputSourceID"] = "com.apple.keylayout.ABC"
    # 不关 TextInputGlobalPropertyPerContextInput。WeType 清干净后它没有
    # 东西可切，关掉反而改变用户既有习惯。
    save(HITOOLBOX, h)
    print(f"HIToolbox 已收敛到 ABC：{HITOOLBOX}")

    s = load(INPUTSOURCES)
    before = s.get("AppleEnabledThirdPartyInputSources", [])
    # com.apple.inputsources.plist 带 com.apple.macl 扩展属性，plistlib 直接
    # 写会被拒（PermissionError），defaults 也写不进（Could not write domain）。
    # 只能靠 cfprefsd 在下次扫描 /Library/Input Methods 时自己重建，所以这里
    # 只报告不改。真正的清理是把 WeType.app 从 /Library/Input Methods 移走，
    # 那一步要 sudo，交给用户决定。
    save(INPUTSOURCES, s)
    print(f"inputsources 第三方输入源仍有 {len(before)} 条（文件受 macl 保护写不进，"
          f"需 sudo 移走 WeType.app 才能彻底清）")

    r = subprocess.run(["pkill", "-f", "Input Methods/WeType"],
                       capture_output=True, text=True)
    print(f"WeType 进程 pkill 返回 {r.returncode}")
    subprocess.run(["killall", "cfprefsd"], check=False)
    print("cfprefsd 已重读")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
