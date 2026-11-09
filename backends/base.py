"""Backend 抽象层。

虚拟化后端是 agentvm 唯一与宿主操作系统绑定的部分。这里定义一套接口，
macOS 的 Tart 与 Windows 的 Hyper-V 各自实现，上层流程不认识具体后端。

设计约束：
  * 两个平台的实现必须同时可 import。hyperv.py 里调 powershell.exe，
    tart.py 里调 tart，都不在 import 阶段执行，所以本文件在 macOS 上
    import hyperv 不会炸。
  * 接口只覆盖现有脚本真正用到的能力，不预设计。
"""

from __future__ import annotations

import shutil


class BackendError(RuntimeError):
    """后端操作失败。message 必须是能直接给用户看的中文说明。"""


class Backend:
    """虚拟化后端接口。

    子类必须实现全部方法。所有方法在目标平台上应当幂等：重复调用
    create_vm 不应报错，而应识别已存在的 VM 并跳过。
    """

    #: 后端标识，用于 agentvm.py 选路与日志
    name = "abstract"

    #: 人类可读的平台说明
    platform_note = ""

    # ---------------------------------------------------------------- 探测 --

    @classmethod
    def available(cls) -> bool:
        """当前宿主是否可能跑这个后端。

        只做便宜的探测（可执行文件是否存在、系统组件是否启用），
        不做需要管理员权限的检查。探测失败返回 False 而不是抛异常。
        """
        raise NotImplementedError

    def missing_prerequisites(self) -> list[str]:
        """返回缺失的前置条件，空列表表示齐备。

        用于 00-check.sh 的等价物：把「缺什么」和「怎么补」一次性说清，
        而不是让用户在后面的步骤里撞到看不懂的错。
        """
        raise NotImplementedError

    # ---------------------------------------------------------------- 安装 --

    def install(self) -> None:
        """把后端自身及其依赖装好。需要管理员权限时自行提权或明确报错。"""
        raise NotImplementedError

    # ------------------------------------------------------------ VM 生命周期 --

    def list_vms(self) -> list[dict]:
        """返回 [{"name": str, "state": str, "cpu": int, "memory_mb": int}]。"""
        raise NotImplementedError

    def vm_exists(self, name: str) -> bool:
        raise NotImplementedError

    def create_vm(
        self,
        name: str,
        cpu: int,
        memory_mb: int,
        disk_gb: int,
        base_image: str | None = None,
    ) -> None:
        """从基础镜像造一个 VM。

        base_image 为 None 时用配置里的默认镜像，走「下载 + 转换」全流程。
        磁盘是 CoW 增量盘，语义对应 Tart 的 clone。
        """
        raise NotImplementedError

    def run_vm(self, name: str) -> None:
        """启动 VM，无图形输出。VM 已在运行时是幂等成功。"""
        raise NotImplementedError

    def stop_vm(self, name: str) -> None:
        raise NotImplementedError

    def delete_vm(self, name: str) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------ guest 内执行 --

    def exec_vm(self, name: str, command: str, timeout: int = 300) -> str:
        """在 guest 内执行命令并返回 stdout。

        这是唯一有语义落差的接口：Tart 的 guest agent 免登录直达，
        Hyper-V 后端走 SSH，因此多出「密钥已注入」与「guest 已就绪」
        两个前置条件，由实现方在内部处理，对上层透明。

        命令必须以非交互方式运行，不得因等待输入而挂住。
        """
        raise NotImplementedError

    def exec_vm_script(self, name: str, script_path: str, timeout: int = 600) -> str:
        """把宿主机上的脚本送进 guest 执行。

        默认实现走 stdin 管道，和原项目 `tart exec vm sh -s < script` 一致，
        避免把脚本内容塞进命令行参数。
        """
        raise NotImplementedError

    def wait_ready(self, name: str, timeout: int = 180) -> None:
        """轮询等待 guest 可以接受命令。

        对应 PITFALLS 第 8 条：VM 状态变成 running 之后客人机还要约 90 秒
        才真正可用。轮询而不是猜，超时抛 BackendError 并带上诊断信息。
        """
        raise NotImplementedError

    # ---------------------------------------------------------------- 工具 --

    @staticmethod
    def have(cmd: str) -> bool:
        return shutil.which(cmd) is not None
