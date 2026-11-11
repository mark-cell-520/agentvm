"""Tart 后端（macOS，Apple Silicon）。

这是原有行为在新抽象层下的包装，逻辑与 scripts/30-create-vm.sh、
60-autostart.sh 中的 tart 调用保持一致。保留它的目的是让上层流程
不必区分平台，同时让 macOS 侧行为可回归对比。

原有的 VM 生命周期语义没动；新增的是 tart run 的三个透传参数
（--dir 目录共享、--vnc、--net-softnet-expose 端口暴露），见
_run_share_args。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import Backend, BackendError

DEFAULT_TART = str(Path.home() / ".local" / "bin" / "tart")


# ------------------------------------------------- tart run 选项的解析 --

def _parse_port(port, what: str) -> int:
    """端口必须是 1-65535 的整数。

    不能只交给 tart：它对畸形端口的反应是一句 usage 错误，而这里的值来自
    用户的 .agentvmrc 或环境变量，报错里指不出是哪一条、该改成什么。
    """
    try:
        n = int(str(port).strip())
    except (TypeError, ValueError):
        raise BackendError(
            f"{what}：端口要是 1-65535 的整数，你给了 {port!r}"
        ) from None
    if not 1 <= n <= 65535:
        raise BackendError(f"{what}：端口 {n} 不在 1-65535 里")
    return n


def _parse_dir_specs(spec) -> list[tuple[Path, str]]:
    """把目录共享配置解析成 [(宿主机路径, 标签), ...]。

    接受 "~/a:host,~/b:code" 这样的字符串，也接受已经是列表的形式
    （命令行 --dir 可以给多次）。逗号分隔多条。

    两个坑：
      * 标签从右边切（rsplit）。macOS 的路径里允许出现冒号（Finder 里显示
        成别的样子），从左边切会把路径当成标签，然后得到一个不存在的目录。
      * 空串和 None 都当「没配」。配置文件里写空值是常见的「先关掉」写法，
        不能因此报错。
    """
    if not spec:
        return []
    items: list[str] = []
    if isinstance(spec, str):
        items = [s for s in spec.split(",") if s.strip()]
    else:
        for s in spec:
            items += [x for x in str(s).split(",") if x.strip()]

    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for item in items:
        raw, sep, tag = item.rpartition(":")
        if not sep or not raw.strip() or not tag.strip():
            raise BackendError(
                f"tart 目录共享要「宿主机路径:标签」，例如 ~/Shared:host，"
                f"你给了 {item!r}")
        tag = tag.strip()
        if tag in seen:
            # tart 对重复标签的反应是启动失败，而客人机里两个同名目录也分不清
            # 谁是谁。在这里就指出来。
            raise BackendError(f"共享目录的标签重复了：{tag}")
        seen.add(tag)
        out.append((Path(raw.strip()).expanduser(), tag))
    return out


def _parse_expose_specs(spec) -> list[tuple[int, int]]:
    """把端口暴露配置解析成 [(宿主端口, 客人端口), ...]。

    走 tart 的 --net-softnet-expose，这是 tart 官方的端口转发方案。比自己在
    宿主机上起 socat/ssh 转发稳：多一个进程就多一个会静默死掉的东西，而它
    死了之后的表象是「端口突然连不上」，跟 VM 本身没关系，极难归因。
    """
    if not spec:
        return []
    items: list[str] = []
    if isinstance(spec, str):
        items = [s for s in spec.split(",") if s.strip()]
    else:
        for s in spec:
            items += [x for x in str(s).split(",") if x.strip()]

    out: list[tuple[int, int]] = []
    for item in items:
        parts = item.split(":")
        if len(parts) != 2:
            raise BackendError(
                f"端口暴露要「宿主端口:客人端口」，例如 8080:80，你给了 {item!r}")
        out.append((_parse_port(parts[0], f"端口暴露的宿主端口（{item}）"),
                    _parse_port(parts[1], f"端口暴露的客人端口（{item}）")))
    return out


def _parse_vnc(spec) -> str | None:
    """校验 --vnc 的「地址:端口」。空值返回 None，表示不传这个参数。"""
    s = str(spec or "").strip()
    if not s:
        return None
    host, sep, port = s.rpartition(":")
    if not sep or not host.strip():
        raise BackendError(
            f"tart_vnc 要「地址:端口」，例如 127.0.0.1:5901，你给了 {spec!r}")
    _parse_port(port, f"tart_vnc 的端口（{s}）")
    return s


class TartBackend(Backend):
    name = "tart"
    platform_note = "macOS，Tart 加 Virtualization.framework，arm64 原生客人机"

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.tart = cfg.get("tart_bin") or DEFAULT_TART
        self.home = Path(cfg.get("agentvm_home") or Path.home() / ".agentvm")
        # run_vm 要把 tart 的 pid 写到 <vms>/<name>/tart.pid，没有这个属性
        # 就是 AttributeError——macOS 上 agentvm.py start 第一步就崩，
        # 而它只在 hyperv 后端那边定义过。
        self.vms_dir = self.home / "vms"
        self.registry_port = int(cfg.get("local_registry_port") or 5000)
        self.repo_name = cfg.get("local_repo_name") or "debian"
        self.source = cfg.get("image_source") or (
            f"127.0.0.1:{self.registry_port}/{self.repo_name}:latest"
        )
        # tart run 的透传参数。在这里就把格式校验做掉（便宜、无 IO），路径
        # 是否存在留到 _run_share_args——那一步马上要起 VM 了，报了错用户
        # 立刻知道是哪一条。
        self.dirs = _parse_dir_specs(cfg.get("tart_dirs"))
        self.vnc = _parse_vnc(cfg.get("tart_vnc"))
        self.expose = _parse_expose_specs(cfg.get("tart_expose"))
        # run_vm 起的 tart 的日志：{name: (路径, 打开的文件句柄)}。
        # 句柄要留着好关，路径要留着好让 wait_ready 超时时把尾部附在错误里。
        self._run_logs: dict = {}

    def set_share_options(self, dirs=None, vnc=None, expose=None) -> None:
        """命令行覆盖 tart run 的三个透传参数。

        单独一个方法而不是让上层直接改 self.dirs：解析和校验必须留在这儿，
        否则 agentvm.py 那边每个来源各写一遍，迟早和 .agentvmrc 那条路的
        口径不一致——而这种不一致的表现是「照文档配的不好使」。
        """
        if dirs is not None:
            self.dirs = _parse_dir_specs(dirs)
        if vnc is not None:
            self.vnc = _parse_vnc(vnc)
        if expose is not None:
            self.expose = _parse_expose_specs(expose)

    # ------------------------------------------------------------- 探测 --

    @classmethod
    def available(cls) -> bool:
        import platform
        import shutil

        if platform.system() != "Darwin" or platform.machine() != "arm64":
            return False
        return shutil.which("tart") is not None or Path(DEFAULT_TART).exists()

    def missing_prerequisites(self) -> list[str]:
        import platform

        missing: list[str] = []
        if platform.system() != "Darwin":
            return [f"当前平台不是 macOS（{platform.system()}），Tart 后端不可用"]
        if platform.machine() != "arm64":
            missing.append("Tart 后端需要 Apple 芯片")
        if not Path(self.tart).exists():
            missing.append(f"找不到 tart：{self.tart}。请运行 scripts/10-install-tart.sh")
        return missing

    def install(self) -> None:
        script = Path(__file__).resolve().parent.parent.parent / "scripts" / "10-install-tart.sh"
        if not script.exists():
            raise BackendError(f"找不到安装脚本：{script}")
        proc = subprocess.run(["bash", str(script)], text=True)
        if proc.returncode != 0:
            raise BackendError(f"Tart 安装失败，退出码 {proc.returncode}")

    # ------------------------------------------------------------ tart 封装 --

    def _tart(self, *args: str, check: bool = True, stdin: str | None = None,
              capture: bool = True) -> str:
        if not Path(self.tart).exists():
            raise BackendError(f"tart 不存在：{self.tart}，请先 install")
        proc = subprocess.run(
            [self.tart, *args],
            capture_output=capture,
            text=True,
            input=stdin,
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            if not detail:
                # capture=False 时两条流都没被接住，detail 必然是空的。这时不能
                # 甩一句空消息——告诉用户 tart 自己的输出已经打到终端上面去了。
                detail = ("没有捕获到输出（这条命令的输出直接写在了终端里，"
                          "往上面看）" if not capture else "tart 没有输出")
            raise BackendError(
                f"tart {' '.join(args)} 失败（退出码 {proc.returncode}）：{detail}"
            )
        return (proc.stdout or "").strip()

    # -------------------------------------------------------- VM 生命周期 --

    def _tart_list(self) -> list[dict]:
        """解析 tart list，返回 [{"name":..., "state":...}]。

        第一列才是 NAME。原来两处都取第二列：vm_exists 拿 SIZE 列当名字比，
        恒为 False，于是「已存在就跳过克隆」这个分支永不命中，每次重跑
        create-vm 都去 tart clone 然后死在 already exists 上——而 README
        明写每一步都可重跑（scripts/30-create-vm.sh 也是取第一列）。

        表头靠内容识别，不靠 `[1:]`：tart 各版本列数不一样（有的多一列
        SIZE），按行号跳会把第一台 VM 当成表头吃掉，或者把表头当成一台
        名叫 NAME 的虚拟机。
        """
        out = self._tart("list")
        rows: list[dict] = []
        for line in out.splitlines():
            parts = line.split()
            if not parts or parts[0].upper() == "NAME":
                continue
            rows.append({
                "name": parts[0],
                "state": parts[1] if len(parts) > 1 else "",
            })
        return rows

    def vm_exists(self, name: str) -> bool:
        return any(r["name"] == name for r in self._tart_list())

    def list_vms(self) -> list[dict]:
        return [
            {"name": r["name"], "state": r["state"], "cpu": None, "memory_mb": None}
            for r in self._tart_list()
        ]

    def create_vm(self, name, cpu, memory_mb, disk_gb, base_image=None) -> None:
        if self.vm_exists(name):
            print(f"  ok VM '{name}' 已存在，跳过克隆")
            return
        source = base_image or self.source
        print(f"==> 克隆 {source} -> {name}")
        # --insecure 是必须的：Tart 拒绝明文 HTTP 注册表
        #
        # capture=False 是刻意的：tart clone 一个 4.3 GB 镜像要 10 到 15 分钟，
        # 而 _tart 默认 capture_output=True，进度条被整个吞掉、调用方又丢掉返回值
        # ——用户对着十几分钟零输出的终端，无法区分「在跑」和「卡死」。
        # 现在让 tart 的进度直接写到本进程的 stderr。check 仍然是 True，
        # 失败时 _tart 会拿 proc.stderr 报出来（capture=False 时它是 None，
        # 于是落到 proc.stdout，也是 None，最后是一句空消息——所以下面自己兜）。
        self._tart("clone", source, name, "--insecure", "--concurrency", "16",
                   capture=False)
        self._tart("set", name, "--cpu", str(cpu), "--memory", str(memory_mb))

    def _run_share_args(self) -> list[str]:
        """把目录共享、VNC、端口暴露拼成 tart run 的额外参数。

        为什么透传而不是自己在宿主机起转发：这三个都是 tart
        （Virtualization.framework）自带的能力，--dir 走 virtiofs，端口暴露走
        --net-softnet-expose。自己在宿主机上再起一个 socat/ssh 转发就等于多
        一个会静默死掉的进程，而它死了之后的表象是「端口突然连不上」，跟 VM
        本身没关系，极难归因。

        客人机侧的挂载点是 /Volumes/My Shared Files/<标签>，这是 tart 定的，
        不是我们定的——所以标签最好起得能认出来（host、code）。

        --vnc 和 --no-graphics 不冲突：后者只是不开原生窗口，VNC server 照样
        起。headless 跑又想用 app 里那个 RFB 控制台看画面时就要它。
        （本机没装 tart，这两句是本机无法实测的部分。）
        """
        out: list[str] = []
        for path, tag in self.dirs:
            if not path.is_dir():
                # 目录不在就整个不起。静默跳过的话用户拿到一台没有共享目录的
                # VM，而他要去客人机里找那个目录才会发现——那时候距离他配置
                # 已经过去很久了。
                raise BackendError(
                    f"共享目录不存在：{path}（标签 {tag}）。"
                    f"tart_dirs 里给的路径要在宿主机上真实存在")
            out += ["--dir", f"{path}:{tag}"]
        if self.vnc:
            out += ["--vnc", self.vnc]
        for host_port, guest_port in self.expose:
            out += ["--net-softnet-expose", f"{host_port}:{guest_port}"]
        return out

    def run_vm(self, name: str) -> None:
        # tart run 是前台阻塞的，而且不能加 timeout：agentvm.py 的 start 会
        # 在这一行等到天荒地老，后面的 wait_ready 和「已就绪」永远到不了，
        # 用户只能 Ctrl-C。所以改成后台起，把 tart 的 pid 交给调用方。
        # check=False 是刻意的：起不来由 wait_ready 那步去报告，这里抢跑
        # 报错只会给出一个和实际状态脱节的退出码。
        import subprocess
        from pathlib import Path

        # 透传参数放在 Popen 之前算：目录不存在这类错要在这时候就抛出来，
        # 而不是等 tart 起了一半再报一句看不懂的 usage。
        extra = self._run_share_args()
        # tart 自己的输出不能丢进 DEVNULL。原来 stdout/stderr 全是 DEVNULL：
        # tart 起不来（参数错、entitlement 缺失、VM 名不存在）时用户要干等
        # wait_ready 的 240 秒，最后只等到一句「客人机在 240 秒内未就绪」——
        # 把 tart 的失败说成客人机没就绪，排查方向从头就错。
        # 现在转到 VM 目录下的 tart.log，wait_ready 超时时会把它尾部附上。
        logf = self.vms_dir / name / "tart.log"
        try:
            logf.parent.mkdir(parents=True, exist_ok=True)
            log = open(logf, "wb")
        except OSError:
            log = subprocess.DEVNULL
            logf = None
        self._run_logs[name] = (logf, log)
        proc = subprocess.Popen(
            [self.tart, "run", name, "--no-graphics", *extra],
            stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        pidfile = self.vms_dir / name / "tart.pid"
        try:
            pidfile.parent.mkdir(parents=True, exist_ok=True)
            pidfile.write_text(str(proc.pid))
        except OSError:
            # 写不了 pid 文件不影响 VM 本身在跑，只是以后 stop 要多问一次
            pass

    def stop_vm(self, name: str) -> None:
        self._tart("stop", name, check=False)
        self._close_run_log(name)

    def delete_vm(self, name: str) -> None:
        self._tart("delete", name, check=False)
        self._close_run_log(name)

    def _close_run_log(self, name: str) -> None:
        """把 run_vm 留着的日志句柄关掉。父进程一直持有一份，不关就是泄漏。"""
        entry = self._run_logs.pop(name, None)
        if not entry:
            return
        _, log = entry
        try:
            log.flush()
            if log is not subprocess.DEVNULL:
                log.close()
        except (OSError, ValueError):
            pass

    # -------------------------------------------------------- guest 内执行 --

    def exec_vm(self, name: str, command: str, timeout: int = 300) -> str:
        # 注意 tart exec 的语法是 tart exec <name> <command>...，
        # 中间的 -- 不会被剥掉，会当成要执行的二进制名。见 PITFALLS 第 2 条。
        if not Path(self.tart).exists():
            raise BackendError(f"tart 不存在：{self.tart}")
        # TimeoutExpired 要转成 BackendError。agentvm.py 的 main 只接
        # BackendError 和 KeyboardInterrupt，超时会以完整 traceback 的形式砸到
        # 用户脸上——而客人机卡住时超时恰恰是最常见的那种失败。
        try:
            proc = subprocess.run(
                [self.tart, "exec", name, "sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise BackendError(
                f"guest 内命令 {timeout} 秒超时：{command[:200]}"
            ) from e
        if proc.returncode != 0:
            raise BackendError(
                f"guest 内命令失败（退出码 {proc.returncode}）：{(proc.stderr or '').strip()}"
            )
        return (proc.stdout or "").strip()

    def exec_vm_script(self, name: str, script_path: str, timeout: int = 600) -> str:
        script = Path(script_path).read_text(encoding="utf-8")
        if not Path(self.tart).exists():
            raise BackendError(f"tart 不存在：{self.tart}")
        try:
            proc = subprocess.run(
                # 必须是 bash 不是 sh。40-provision-guest.sh 第 21 行是
                # `set -euo pipefail`，而 tart 的客人机是 Debian，/bin/sh 是
                # dash，dash 不支持 pipefail——整段脚本第一行就退，换 apt 源和
                # 装编译工具链一样都不做，agentvm.py provision 必然以退出码 2
                # 失败。2026-11-11 实测（/bin/dash -s 喂 set -euo pipefail）：
                #   dash: set: Illegal option -o pipefail，退出码 2
                #   bash：正常往下走，退出码 0
                # Makefile 的 provision 目标早就为同一个原因改成了 bash -s，
                # 这条是漏改的同类。backends/base.py 的 docstring 里
                # 「和原项目 tart exec vm sh -s 一致」说的正是这个被否定的写法。
                [self.tart, "exec", name, "bash", "-s"],
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise BackendError(
                f"guest 内脚本 {timeout} 秒超时（{Path(script_path).name}）"
            ) from e
        if proc.returncode != 0:
            raise BackendError(
                f"guest 内脚本失败（退出码 {proc.returncode}）：{(proc.stderr or '').strip()}"
            )
        return (proc.stdout or "").strip()

    def wait_ready(self, name: str, timeout: int = 240) -> None:
        import time

        # 退避而不是固定 10 秒。原来是 while + time.sleep(10)：客人机首启要 90 秒
        # 左右，前几轮必然失败，等于干等；更要紧的是 tart exec 挂住时单次要等满
        # 它的 20 秒超时，240 秒的预算实际会变成 240 + 24x20 = 720 秒，用户等了
        # 十二分钟才看到超时，而那之前没有任何输出。
        # 现在 1、2、4、8 然后封顶 10 秒，且每轮都打一行——等待期间用户至少知道
        # 它在等什么。2026-11-11 审计补上。
        deadline = time.time() + timeout
        attempt = 0
        interval = 1
        while time.time() < deadline:
            attempt += 1
            try:
                self.exec_vm(name, "true", timeout=20)
                return
            except BackendError:
                left = int(deadline - time.time())
                if left <= 0:
                    break
                print(f"    等客人机就绪（第 {attempt} 次，还剩约 {left} 秒）…",
                      flush=True)
                time.sleep(min(interval, left))
                interval = min(interval * 2, 10)
        # 超时了。这时候最该说的是 tart 自己怎么了，而不是笼统一句「未就绪」：
        # tart 起不来（签名、entitlement、VM 名）时它早就退了，等再久也一样。
        raise BackendError(
            f"客人机在 {timeout} 秒内未就绪。{self._tart_log_tail(name)}"
        )

    def _tart_log_tail(self, name: str, lines: int = 12) -> str:
        """tart.log 的末尾几行，用来附在「未就绪」的错误里。"""
        entry = self._run_logs.get(name)
        if not entry:
            return ""
        logf, log = entry
        try:
            log.flush()
        except (OSError, ValueError):
            pass
        if logf is None or not logf.is_file():
            return ""
        try:
            tail = logf.read_text(errors="replace").splitlines()[-lines:]
        except OSError:
            return ""
        if not tail:
            return ""
        return (f"tart 自己的日志尾部（{logf}）：\n    "
                + "\n    ".join(tail))
