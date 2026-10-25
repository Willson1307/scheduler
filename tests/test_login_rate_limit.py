"""登录限流测试：组合锁 + IP 滑动窗口（2026-10-25 方案升级）。

背景：
  旧方案「同 login 连续失败 5 次 → 账号锁 15 分钟」是可被任何人利用的
  账号级 DoS——登录名可猜（trial_t01..t12），任何人打 5 次错密码就能让
  老师 15 分钟进不去。本文件锁住新方案的三层语义：

    ① 组合锁：(login, client_ip) 连续失败 5 次 → 只锁这一组 60 秒
    ② IP 限流：单 IP 60 秒内 > 30 次登录请求 → 429 + Retry-After
    ③ 账号级硬锁不存在：别人（别的 IP）对同一账号登录完全不受影响

⛔ 破坏法验证（本文件必须能判红）：
   - 把计数键从 (login_name, ip) 改回 login_name → test_other_ip_unaffected 变红
   - 被限流拒绝的请求若也入窗（永久封禁）→ test_ip_window_recovery 变红
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from server import api_login  # noqa: E402

LOGIN = "/api/login"


class _Base(unittest.TestCase):
    def setUp(self):
        api_login.reset_locks()
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = connect(self.db_path)
        init_db(conn)
        org_id, root = og.create_organization(conn, "示例学校")
        for name in ("t01", "t02"):
            obj = og.create_object(conn, org_id, "user", f"示例教师{name}")
            og.add_edge(conn, root, obj)
            og.create_user(conn, obj, name, "right-password", role="teacher")
        conn.commit()
        conn.close()

        # handler 每次 open_db() 都会新开连接并自行 close —— 指到临时库即可
        self._orig_open_db = api_login.config.open_db
        api_login.config.open_db = lambda: connect(self.db_path)

    def tearDown(self):
        api_login.config.open_db = self._orig_open_db
        api_login.reset_locks()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def call(self, login, password, ip):
        """直接调 handler，返回 (status, body, extra_headers) 三元组。"""
        body = json.dumps({"login": login, "password": password}).encode()
        res = api_login.login("POST", LOGIN, {}, body, {"x-client-ip": ip})
        if isinstance(res, tuple) and len(res) == 3:
            return res
        if isinstance(res, tuple) and len(res) == 2:
            return res[0], res[1], None
        return 200, res, None


class TestComboLock(_Base):
    """① 组合锁：只锁 (login, ip) 这一组，60 秒。"""

    def test_five_failures_then_429(self):
        seq = [self.call("t01", "wrong", "10.0.0.1")[0] for _ in range(6)]
        self.assertEqual(seq, [401, 401, 401, 401, 401, 429],
                         f"组合锁未按 5 次失败 + 第 6 次 429 生效：{seq}")

    def test_other_ip_unaffected(self):
        """🔴 核心断言：一个人恶意操作不影响别人（消除账号级 DoS）。

        IP-A 把 t01 锁了，IP-B 用**正确密码**登录 t01 必须 200。
        旧账号级锁在这里会让 IP-B 也吃 429 —— 正是要消灭的行为。
        """
        for _ in range(5):
            self.call("t01", "wrong", "10.0.0.1")
        status, body, _ = self.call("t01", "right-password", "10.0.0.2")
        self.assertEqual(status, 200,
                         f"IP-A 锁了 t01 却波及 IP-B 的正常登录（{status}）"
                         f"→ 账号级 DoS 复发：{body}")

    def test_lock_not_ip_wide_ban(self):
        """锁定按组合隔离：IP-A 对 t01 被锁，同一 IP-A 打 t02 不受影响。"""
        for _ in range(5):
            self.call("t01", "wrong", "10.0.0.1")
        status, body, _ = self.call("t02", "wrong", "10.0.0.1")
        self.assertEqual(status, 401,
                         f"对 t01 的组合锁把整个 IP 或别的账号也封了（{status}）"
                         f"→ 锁定粒度错误：{body}")


class TestIPRateLimit(_Base):
    """② IP 滑动窗口限流：单 IP 60 秒内 > 30 次登录请求 → 429。"""

    def test_31st_request_429_with_retry_after(self):
        # 每次换登录名，绕开组合锁（每个组合只失败 1 次），单测 IP 窗口本身
        for i in range(30):
            status, _, _ = self.call(f"ghost_{i:02d}", "wrong", "10.9.9.9")
            self.assertEqual(status, 401, f"第 {i + 1} 次不应被限流")
        status, body, extra = self.call("ghost_30", "wrong", "10.9.9.9")
        self.assertEqual(status, 429, f"第 31 次必须 429：{body}")
        self.assertEqual(body["error"]["code"], "rate_limited")
        self.assertEqual(extra and extra.get("Retry-After"), "60",
                         f"429 必须带 Retry-After: 60 响应头：{extra}")

    def test_ip_window_recovery_not_permanent(self):
        """窗口溢出后随时间前进恢复——不是永久封禁（被拒请求不入窗）。"""
        for _ in range(30):
            api_login._ip_record("10.8.8.8", 1000.0)
        self.assertTrue(api_login._ip_rate_limited("10.8.8.8", 1000.5),
                        "窗口内打满 30 次后应被限流")
        self.assertFalse(api_login._ip_rate_limited("10.8.8.8", 1061.0),
                         "60 秒窗口滑过后应恢复（永久封禁 = 违背设计）")

    def test_reset_locks_clears_both_trackers(self):
        """reset_locks 必须同时清组合锁与 IP 窗口，否则测试间互相污染。"""
        for _ in range(5):
            api_login._record_failure("t01", "10.7.7.7", time.time())
        for _ in range(30):
            api_login._ip_record("10.7.7.7", time.time())
        api_login.reset_locks()
        self.assertFalse(api_login._is_locked("t01", "10.7.7.7", time.time() + 1))
        self.assertFalse(api_login._ip_rate_limited("10.7.7.7", time.time()))


class TestNoEnumeration(_Base):
    """③ 枚举不可区分：组合锁 / IP 限流对真实与不存在账号完全对称。"""

    def test_sequence_bitwise_identical(self):
        real, fake = [], []
        for _ in range(6):
            s, b, _ = self.call("t01", "wrong-password", "10.1.1.1")
            real.append((s, b["error"]["code"], b["error"]["message"]))
        for _ in range(6):
            s, b, _ = self.call("ghost_user", "wrong-password", "10.1.1.2")
            fake.append((s, b["error"]["code"], b["error"]["message"]))
        self.assertEqual(real, fake,
                         f"失败序列可区分账号是否存在 → 枚举信号回归："
                         f"真实={real} 不存在={fake}")
        self.assertEqual([r[0] for r in real], [401] * 5 + [429])


class TestTimingEqualized(_Base):
    """等时补偿仍生效：不存在账号耗时 ≥ 真实账号的 50%。"""

    def _min_time(self, login, ip, n=3):
        samples = []
        for _ in range(n):
            api_login.reset_locks()  # 每次清零，避免走锁定/限流早退分支
            t0 = time.perf_counter()
            self.call(login, "wrong-password", ip)
            samples.append(time.perf_counter() - t0)
        return min(samples)

    def test_nonexistent_not_faster_than_real(self):
        t_real = self._min_time("t01", "10.2.2.1")
        t_fake = self._min_time("ghost_user_timing", "10.2.2.2")
        ratio = (t_fake / t_real) if t_real else 0.0
        self.assertGreater(
            ratio, 0.5,
            f"不存在账号明显更快（{t_fake * 1000:.1f}ms vs {t_real * 1000:.1f}ms，"
            f"比值 {ratio:.2f}）→ 计时侧信道仍可枚举，等时补偿失效",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
