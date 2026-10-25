"""CP-H S3 回归测试：登录名枚举必须不可区分（2026-10-25）。

背景（`docs/21 §6.3` P1 返修项②）：
  10-11 那版把「登录名不存在时不计数」当成修复，
  实际把枚举信号放大成两条硬区分：
    真实账号错密 5 次 → 第 6 次 429；不存在账号 → 永远 401。
  且 `og.authenticate()` 对不存在用户不跑 PBKDF2 → **响应耗时差一个数量级**
  （计时侧信道，比状态码更好利用）。

本文件锁三件事：
  A. **状态码对称**：存在与不存在的登录名，失败序列完全一致（401×5 → 429）。
  B. **拼错不波及**：打错一个串，不锁住正确登录名（10-11 的合理诉求仍守住）。
  C. **等时补偿存在**：不存在用户的响应耗时与存在用户同量级，不再「秒回」。

⛔ 破坏法验证（本文件必须能判红）：
   - 删掉 `_record_failure(login_name, ip, now)` 前的等时补偿 → C 组变红
   - 改成「只在用户存在时计数」→ A 组变红
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
        obj = og.create_object(conn, org_id, "user", "示例教师01")
        og.add_edge(conn, root, obj)
        og.create_user(conn, obj, "t01", "right-password", role="teacher")
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

    def call(self, login, password):
        body = json.dumps({"login": login, "password": password}).encode()
        res = api_login.login("POST", LOGIN, {}, body, {})
        if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], int):
            return res[0], res[1]
        return 200, res


class TestNoEnumerationByStatusCode(_Base):
    """A. 状态码对称 —— 这是 CP-H S3 的核心断言。"""

    def test_nonexistent_login_locks_like_real_one(self):
        seq = [self.call("no_such_user", "x")[0] for _ in range(6)]
        self.assertEqual(seq, [401, 401, 401, 401, 401, 429],
                         f"不存在账号未按同一规则锁定 → 枚举信号仍在：{seq}")

    def test_status_sequence_identical_for_both(self):
        real, fake = [], []
        for _ in range(6):
            real.append(self.call("t01", "wrong-password")[0])
            fake.append(self.call("ghost_user", "wrong-password")[0])
        self.assertEqual(real, fake,
                         f"两条路径状态码不一致 → 可枚举账号：真实={real} 不存在={fake}")

    def test_both_return_same_error_code(self):
        api_login.reset_locks()
        s1, b1 = self.call("t01", "wrong")
        s2, b2 = self.call("ghost", "wrong")
        self.assertEqual(s1, s2)
        self.assertEqual(b1["error"]["code"], b2["error"]["code"])
        self.assertEqual(b1["error"]["message"], b2["error"]["message"])


class TestMisspellingDoesNotLockReal(unittest.TestCase):
    """B. 守住 10-11 的合理诉求：拼错一个串，不锁住正确登录名。"""

    def setUp(self):
        api_login.reset_locks()

    def tearDown(self):
        api_login.reset_locks()

    def test_typo_does_not_lock_correct_name(self):
        # 2026-10-25 签名升级：计数键 = (login, ip)，直接调内部函数需传 ip
        for _ in range(5):
            api_login._record_failure("tO1", "9.9.9.9", time.time())
        now = time.time() + 1
        self.assertTrue(api_login._is_locked("tO1", "9.9.9.9", now))
        self.assertFalse(api_login._is_locked("t01", "9.9.9.9", now),
                         "拼错的串锁住了正确登录名 → 10-11 的可用性问题复发")


class TestTimingEqualized(_Base):
    """C. 等时补偿：不存在用户的耗时不能显著低于存在用户。"""

    def _min_time(self, login, n=3):
        samples = []
        for _ in range(n):
            api_login.reset_locks()  # 每次清零，避免走锁定早退分支
            t0 = time.perf_counter()
            self.call(login, "wrong-password")
            samples.append(time.perf_counter() - t0)
        return min(samples)

    def test_nonexistent_not_faster_than_real(self):
        t_real = self._min_time("t01")
        t_fake = self._min_time("ghost_user_timing")
        ratio = (t_fake / t_real) if t_real else 0.0
        self.assertGreater(
            ratio, 0.5,
            f"不存在账号明显更快（{t_fake * 1000:.1f}ms vs {t_real * 1000:.1f}ms，"
            f"比值 {ratio:.2f}）→ 计时侧信道仍可枚举，等时补偿失效",
        )

    def test_dummy_hash_is_valid_format(self):
        self.assertTrue(api_login._DUMMY_HASH.startswith("pbkdf2$"))
        self.assertEqual(len(api_login._DUMMY_HASH.split("$")), 3)
        self.assertFalse(og.verify_password("anything", api_login._DUMMY_HASH))


if __name__ == "__main__":
    unittest.main(verbosity=2)
