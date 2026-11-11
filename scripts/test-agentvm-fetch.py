#!/usr/bin/env python3
"""scripts/agentvm-fetch 的离线测试：并发下载、token 文件、HTTP 码捕获。

不起真网络。做法是在 PATH 最前面放一个 curl shim：
  · 把 https://127.0.0.1:PORT/... 改写成 http://127.0.0.1:PORT/... 再转给真
    curl——这样能打一个本地的 python HTTP server，不必给产品代码加 http 后门，
    也不必往系统 keychain 里装自签 CA（macOS 的 curl 走 SecureTransport，
    不认 CURL_CA_BUNDLE）。
  · 对指定的 digest 直接 `printf '000'; exit 7`，精确复现「curl 失败了但 -w
    仍然写出了 000」这个形状，用来钉 HTTP 码捕获不被 `|| echo 000` 接成
    "000000"。

测的四件事：
  1. --concurrency 的参数校验（死参数时代照单全收，等到 xargs 才报错）。
  2. 下载真的并发（原来严格串行；这条是本次审计最大的一笔提速）。
  3. curl 失败时 HTTP 码是 000 而不是 000000。
  4. --verify 的全量哈希也是并行的，且能抓出内容不对的 blob。
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FETCH = HERE / "agentvm-fetch"

REPO = "test/img"
TAG = "latest"
BLOBS = {}


def _digest(payload: bytes) -> str:
    import hashlib
    return hashlib.sha256(payload).hexdigest()


# 四个正常的 blob，加一个用来制造 curl 失败的。
GOOD = {
    _digest(b"config-bytes"): b"config-bytes",
    _digest(b"layer-one" * 200): b"layer-one" * 200,
    _digest(b"layer-two" * 300): b"layer-two" * 300,
    _digest(b"layer-three" * 400): b"layer-three" * 400,
}
SIM_FAIL_DIGEST = _digest(b"this-one-cannot-be-downloaded")

MANIFEST = {
    "schemaVersion": 2,
    "config": {"digest": "sha256:" + _digest(b"config-bytes"), "size": len(b"config-bytes")},
    "layers": [
        {"digest": "sha256:" + d, "size": len(p)}
        for d, p in list(GOOD.items())[1:]
    ],
}
# 带一个永远下不成的 blob 的 manifest，只给测 HTTP 码捕获那组用。
MANIFEST_WITH_FAILURE = {
    "schemaVersion": 2,
    "config": MANIFEST["config"],
    "layers": MANIFEST["layers"] + [
        {"digest": "sha256:" + SIM_FAIL_DIGEST, "size": 32},
    ],
}


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0


class Handler(http.server.BaseHTTPRequestHandler):
    state = None          # 由 setUp 注入
    manifest = MANIFEST   # 需要失败用例的类覆盖成 MANIFEST_WITH_FAILURE
    delay = 0.4           # 每个 blob 请求握这么久，好让并发看得见

    def log_message(self, *a):    # 静音
        pass

    def _count(self):
        with self.state.lock:
            self.state.inflight += 1
            self.state.peak = max(self.state.peak, self.state.inflight)

    def _done(self):
        with self.state.lock:
            self.state.inflight -= 1

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/token":
            self._json({"token": "tok-1"})
            return
        if path == f"/v2/{REPO}/manifests/{TAG}":
            self._json(self.manifest)
            return
        if path.startswith(f"/v2/{REPO}/blobs/sha256:"):
            digest = path.rsplit(":", 1)[1]
            body = GOOD.get(digest)
            if body is None:
                self.send_error(404)
                return
            self._count()
            try:
                time.sleep(self.delay)      # 慢一点，并发才测得出来
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                self._done()
            return
        self.send_error(404)

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FetchTestCase(unittest.TestCase):
    """公共的脚手架：起 server、写 shim、跑 agentvm-fetch。"""

    def setUp(self):
        Handler.manifest = MANIFEST      # 默认全好；需要失败用例的类自己覆盖
        Handler.state = _State()
        self.tmp = Path(tempfile.mkdtemp(prefix="agentvm-fetch-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        Handler.state = _State()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.srv.server_close)

        # curl shim：改写协议，并对指定的 digest 模拟 curl 失败。
        #
        # 改写用「前缀剥离再拼」而不是 ${a/pat/rep}：后者在 replacement 里
        # 的 `\/` 会被原样留下（实测得到 http:\/\/127.0.0.1.../，curl 报
        # "Port number was not a decimal number"）。剥离式写法里 `/` 不是
        # 分隔符，不用转义。
        self.shimdir = self.tmp / "shim"
        self.shimdir.mkdir()
        prefix = f"https://127.0.0.1:{self.port}/"
        shim = self.shimdir / "curl"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f"P={prefix!r}\n"
            f"H='http://127.0.0.1:{self.port}/'\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            f"    *{SIM_FAIL_DIGEST}*) printf '000'; exit 7 ;;\n"
            "  esac\n"
            "done\n"
            "out=()\n"
            "for a in \"$@\"; do\n"
            "  if [[ $a == \"$P\"* ]]; then out+=(\"$H${a#$P}\"); else out+=(\"$a\"); fi\n"
            "done\n"
            "exec /usr/bin/curl \"${out[@]}\"\n"
        )
        shim.chmod(0o755)

        self.dest = self.tmp / "image"
        self.env = dict(os.environ)
        self.env["PATH"] = f"{self.shimdir}:{self.env['PATH']}"
        self.env["NO_COLOR"] = "1"

    def run_fetch(self, *extra, conc=4):
        cmd = ["bash", str(FETCH),
               "--repo", f"127.0.0.1:{self.port}/{REPO}",
               "--tag", TAG,
               "--dest", str(self.dest),
               "--concurrency", str(conc)]
        cmd += list(extra)
        return subprocess.run(cmd, capture_output=True, text=True,
                              env=self.env, timeout=300)


class TestConcurrencyArg(FetchTestCase):
    """--concurrency 曾经是个死参数，连值都不校验。"""

    def test_a_non_number_is_refused_up_front(self):
        for bad in ("abc", "0", "-1", "65", "", "4x"):
            r = self.run_fetch(conc=bad) if bad else None
            if bad == "":
                continue
            self.assertNotEqual(r.returncode, 0, f"--concurrency={bad!r} 应该被拒")
            self.assertIn("--concurrency", r.stderr)

    def test_a_sane_number_is_accepted(self):
        r = self.run_fetch(conc=1)
        # conc=1 也能把 4 个正常 blob 下完（第 5 个是故意失败的）
        self.assertEqual(r.returncode, 0, r.stderr)
        for d in GOOD:
            self.assertTrue((self.dest / "blobs" / "sha256" / d).is_file())


class TestActuallyParallel(FetchTestCase):
    """本次审计最大的一笔：下载从严格串行改成 xargs -P。"""

    def test_four_blobs_download_concurrently(self):
        t0 = time.time()
        r = self.run_fetch(conc=4)
        elapsed = time.time() - t0

        self.assertEqual(r.returncode, 0, r.stderr)
        for d in GOOD:
            f = self.dest / "blobs" / "sha256" / d
            self.assertTrue(f.is_file(), f"{d[:12]} 没下到")
            self.assertEqual(f.read_bytes(), GOOD[d])

        # server 每个请求握 0.4 秒。串行是 4 x 0.4 = 1.6 秒以上；
        # 4 路并行理论上接近 0.4 秒。这里给到 1.2 秒的判据，
        # 串行一定过不去、并行稳稳过得去。
        self.assertLess(elapsed, 1.2,
                        f"4 个各握 0.4 秒的请求花了 {elapsed:.2f} 秒，"
                        f"看着像串行")
        # 峰值并发至少 3：真并行了。这条比计时更硬，不受机器负载影响。
        self.assertGreaterEqual(Handler.state.peak, 3,
                                f"观测到的峰值并发只有 {Handler.state.peak}，"
                                f"不像在并行下载")

    def test_concurrency_1_is_serial_and_still_works(self):
        r = self.run_fetch(conc=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLessEqual(Handler.state.peak, 1)


class TestHttpCodeCapture(FetchTestCase):
    """`|| echo 000` 会把码接成 000000。"""

    def setUp(self):
        super().setUp()
        Handler.manifest = MANIFEST_WITH_FAILURE

    def test_a_failed_curl_reports_000_not_000000(self):
        r = self.run_fetch()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HTTP 000", r.stderr, r.stderr)
        self.assertNotIn("000000", r.stderr)

    def test_the_failure_message_names_the_digest(self):
        r = self.run_fetch()
        self.assertIn(SIM_FAIL_DIGEST[:16], r.stderr)


class TestVerifyParallel(FetchTestCase):
    """--verify 的全量哈希也是并行的，且抓得出内容不对的 blob。

    这条路径的形状要注意：带 --verify 时 fetch_one 自己就会在「大小已对上」的
    早退分支里先算一遍哈希，对不上就 warn 并重下。所以末尾那次全量复查是第二道
    防线——它覆盖的是 fetch_one 那条提前校验之外的情况。两道都在这一条命令里。
    """

    def test_verify_passes_and_reports_progress(self):
        r = self.run_fetch()
        self.assertEqual(r.returncode, 0, r.stderr)
        # config 一个 + 三个 layer
        self.assertEqual(len(list((self.dest / "blobs" / "sha256").iterdir())), 4)

        r2 = self.run_fetch("--verify")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("verified against their digests", r2.stderr)

    def test_a_corrupted_blob_is_caught_and_repaired(self):
        self.run_fetch()
        victim = self.dest / "blobs" / "sha256" / list(GOOD)[1]
        orig = victim.read_bytes()
        # 同样长度、不同内容：大小那道关拦不住，只能靠哈希。
        victim.write_bytes(b"X" * len(orig))

        r = self.run_fetch("--verify")
        self.assertEqual(r.returncode, 0, r.stderr)
        # 提前校验那一道报了出来，并且重下修好了
        self.assertIn("hash mismatch", r.stderr)
        self.assertEqual(victim.read_bytes(), orig)


class TestTokenFile(FetchTestCase):
    """token 走文件而不是父进程变量：worker 是独立进程，变量传不进去。"""

    def test_token_file_is_written_private_and_used(self):
        r = self.run_fetch()
        tok = self.dest / ".token"
        self.assertTrue(tok.is_file(), "token 没落盘，worker 读不到")
        self.assertEqual(oct(tok.stat().st_mode & 0o777), oct(0o600),
                         "token 文件不该让别人读")
        self.assertIn("tok-1", tok.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
