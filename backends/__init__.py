"""agentvm 的虚拟化后端集合。

按平台选后端：macOS 走 Tart，Windows 走 Hyper-V。导入本模块不会
执行任何平台命令，所以两个实现可以在任意平台上被安全 import。
"""

from __future__ import annotations

import os

from .base import Backend, BackendError

__all__ = ["Backend", "BackendError", "get_backend", "available_backends"]


def available_backends() -> list[str]:
    """当前平台上可能可用的后端名列表，按推荐度排序。"""
    names = []
    if os.name == "nt":
        names.append("hyperv")
    else:
        names.append("tart")
    return names


def get_backend(preferred: str | None = None, cfg: dict | None = None) -> Backend:
    """取一个后端实例。

    preferred 显式指定后端名；None 时按平台自动选。指定的后端不可用时
    报错并说明原因，不静默回退到另一个后端——那会让用户在错误的平台上
    得到看似成功的执行结果。
    """
    cfg = cfg or {}
    name = preferred or cfg.get("backend")

    if name is None:
        name = "hyperv" if os.name == "nt" else "tart"

    if name == "hyperv":
        from .hyperv import HyperVBackend

        if os.name != "nt":
            raise BackendError(
                "Hyper-V 后端只能在 Windows 上使用。在 macOS 上请用 tart 后端。"
            )
        return HyperVBackend(cfg)

    if name == "tart":
        from .tart import TartBackend

        import platform

        if platform.system() != "Darwin":
            raise BackendError(
                f"Tart 后端只能在 macOS 上使用，当前平台是 {platform.system()}。"
            )
        return TartBackend(cfg)

    raise BackendError(
        f"未知后端：{name}。可用值：hyperv（Windows）、tart（macOS）。"
    )
