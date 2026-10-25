"""防伪造客户端 IP 守卫测试（2026-10-25，随登录限流升级补的安全回归）。

锁住的语义（对应 http_api.py 的注入实现）：
  🔴 服务端**必须**用 socket 对端地址（self.client_address[0]）作为限流/锁定
  的归属 IP，且必须在把客户端头小写化合并进 headers **之后**注入——
  否则客户端伪造的 X-Client-IP 头就能覆盖真实 IP，整条限流形同虚设：

    A. 攻击者每次请求换一个伪造 X-Client-IP → 若服务端信任该头，
       IP 滑动窗口按伪造 IP 计数（每 IP 恒 1 次）→ 永不触发 429 → 限流被绕过。
    B. 攻击者带固定伪造头连打错密码 → 锁定仍按真实 IP 归属生效（429 locked），
       攻击者换头也逃不掉自己造成的锁定。

为什么必须打**自己起的独立子进程服务**（⛔ 绝不打 8000 共享服务）：
  限流窗口是进程内全局态。打 8000 共享服务会把该服务 60 秒窗口打满 30 次，
  后续契约测试全部吃 429 → 假红。本文件起随机端口子进程，tearDownClass
  kill 干净，端口释放，与任何共享服务互不干扰。

🔴 执行顺序依赖（有意设计，勿"优化"）：test_01 先跑（锁定语义，仅入窗 6 次），
  test_02 后跑（40 次把窗口打满触发 rate_limited）。若颠倒顺序，test_01 的
  请求会先被 test_02 打满的 IP 窗口拦成 rate_limited，锁定语义测不到。
  unittest 类内方法按名字母序执行，test_01 < test_02 恰好满足。

⛔ 破坏法验证（本文件必须能判红，AGENTS §4.3）：
  把 http_api.py 的注入行改成
      headers["x-client-ip"] = headers.get("x-client-ip") or self.client_address[0]
  （客户端头优先）→ test_02 必须红（40 次全 401，限流被绕过）；还原后必须绿。

只用 Python 标准库。
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SERVER = os.path.join(REPO, "scripts", "run_server.py")
DEMO_DB = os.path.join(REPO, "data", "demo.db")

LOGIN_PATH = "/api/login"
#: demo 库契约账号（与契约测试同源：admin/admin123）。
#: 🔴 只用**错密码**——本文件不把任何正确口令写进代码。
DEMO_ADMIN = "admin"


def _post_login(base_url: str, login: str, password: str, spoof_ip: str) -> tuple[int, dict]:
    """向独立子进程服务发一次登录请求，带伪造 X-Client-IP 头。

    返回 (status, body)。🔴 ProxyHandler({}) 必须显式置空——本机若有系统代理，
    127.0.0.1 请求会被代理搅成 502（AGENTS 排障表既有坑）。
    """
    data = json.dumps({"login": login, "password": password}).encode("utf-8")
    req = urllib.request.Request(base_url + LOGIN_PATH, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Client-IP", spoof_ip)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def _error_code(body: dict) -> str | None:
    err = body.get("error")
    return err.get("code") if isinstance(err, dict) else None


class TestClientIPNotSpoofable(unittest.TestCase):
    """X-Client-IP 伪造不能绕过 / 逃逸登录限流（独立子进程服务）。"""

    proc: subprocess.Popen
    port: int
    base_url: str
    _tmpdir: str

    @classmethod
    def setUpClass(cls):
        # ① demo 库副本（sqlite3 backup()，绝不碰 data/demo.db 本体）
        cls._tmpdir = tempfile.mkdtemp(prefix="ipguard_")
        db_copy = os.path.join(cls._tmpdir, "demo_copy.db")
        src = sqlite3.connect(DEMO_DB)
        try:
            dst = sqlite3.connect(db_copy)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        # ② 独立子进程服务：随机端口，只指向临时副本库
        cls.proc = subprocess.Popen(
            [sys.executable, RUN_SERVER,
             "--host", "127.0.0.1", "--port", "0", "--db", db_copy],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=REPO,
        )
        line = cls.proc.stdout.readline()  # READY host=... port=... db=... today=...
        cls.port = 0
        if line.startswith("READY"):
            for part in line.split():
                if part.startswith("port="):
                    cls.port = int(part.split("=", 1)[1])
        if not cls.port:
            cls.proc.kill()
            cls.proc.wait()
            raise RuntimeError(f"子进程服务未就绪（首行={line!r}）")
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        # 🔴 必须 kill + wait：端口随进程释放，残留进程会占随机端口并泄漏库句柄
        cls.proc.kill()
        cls.proc.wait()
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    # -- B（先跑）：锁定按真实 IP 归属，伪造头逃不掉 --------------------------

    def test_01_lock_binds_to_real_ip_not_spoofed_header(self):
        """带固定伪造头 X-Client-IP: 9.9.9.9 对 admin 连打 5 次错密码。

        第 6 次必须 429 且 code == locked —— 锁定发生在**攻击者自己**身上
        （按真实出口 IP 归属），换伪造头不改变归属。
        """
        seq = []
        for _ in range(5):
            status, body = _post_login(
                self.base_url, DEMO_ADMIN, "definitely-wrong-pw", "9.9.9.9")
            seq.append((status, _error_code(body)))
        self.assertEqual(
            seq, [(401, "bad_credentials")] * 5,
            f"前 5 次错密码应一律 401 bad_credentials（实际 {seq}）",
        )
        status, body = _post_login(
            self.base_url, DEMO_ADMIN, "definitely-wrong-pw", "9.9.9.9")
        self.assertEqual(
            status, 429,
            f"第 6 次必须 429（实际 {status}，body={body}）——锁定未生效",
        )
        self.assertEqual(
            _error_code(body), "locked",
            f"第 6 次 code 必须是 locked（实际 {_error_code(body)!r}，body={body}）",
        )

    # -- A（后跑）：换头绕不过 IP 滑动窗口 -----------------------------------

    def test_02_spoofed_header_cannot_bypass_rate_limit(self):
        """同一客户端 40 次登录，每次带不同 X-Client-IP: 7.7.7.N。

        服务端只认 socket 对端地址 → 40 次全记在同一个真实 IP 上，
        必然触发 IP 限流（60 秒窗口 > 30 次）→ 至少一次 429 rate_limited。
        若 40 次全是 401 → 客户端头覆盖了真实 IP → 限流被绕过 → 判红。
        （每次换登录名以避开组合锁干扰，专测 IP 窗口本身。）
        """
        seq = []
        for i in range(40):
            status, body = _post_login(
                self.base_url, f"spoof_probe_{i:02d}", "wrong-pw", f"7.7.7.{i + 1}")
            seq.append((status, _error_code(body)))
        hits = [s for s, code in seq if s == 429 and code == "rate_limited"]
        self.assertTrue(
            hits,
            "40 次换头登录全部未触发 429 rate_limited → 伪造的 X-Client-IP "
            "覆盖了真实 IP，IP 限流被绕过（安全回归）：\n"
            f"  序列前 10 = {seq[:10]}\n  去重状态码 = {sorted({s for s, _ in seq})}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
