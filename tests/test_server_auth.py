"""B2 服务端鉴权测试：POST /api/login（分支B）。

覆盖（docs/07 §1）：
  - 登录成功回 token + user{id,login,global_role} + default_group_id
  - 🔴 响应绝不回传 password_hash
  - 坏凭证 → 401，error.code == "bad_credentials"
  - 连续失败 5 次后锁定 15 分钟 → 429
  - default_group_id 按勘误3（admin→根组）

运行：python -m unittest discover -s tests -p "test_server_auth.py" -v
"""

import os
import sys
import tempfile
import unittest
import json
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import authz  # noqa: E402
from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from server import config, routes  # noqa: E402


def _build_org(db_path):
    """建一个可控的小型组织（不用 demo seed，保证测试确定）。"""
    conn = connect(db_path)
    init_db(conn)
    org, root = og.create_organization(conn, "测试校")
    grade = og.create_object(conn, org, "group", "高一")
    cls = og.create_object(conn, org, "group", "高一1班")
    og.add_edge(conn, root, grade)
    og.add_edge(conn, grade, cls)
    # admin
    a_obj = og.create_object(conn, org, "user", "管理员")
    og.create_user(conn, a_obj, "admin", "admin123", role="admin")
    # teacher（在年级授 propose，继承到班级）
    t_obj = og.create_object(conn, org, "user", "王老师")
    og.create_user(conn, t_obj, "t1", "t1pw", role="teacher")
    tid = conn.execute("SELECT id FROM users WHERE login='t1'").fetchone()["id"]
    authz.grant_role(conn, org, grade, tid, "propose")
    conn.commit()
    conn.close()
    return {"org": org, "root": root, "grade": grade, "cls": cls, "teacher_id": tid}


class _ServerTestBase(unittest.TestCase):
    """公共 setUp/tearDown：临时库 + 配置 + 路由重扫。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.fix = _build_org(self.db_path)
        self.cfg = config.ServerConfig(
            host="127.0.0.1", port=0, db_path=self.db_path,
            today=date(2026, 10, 5), seed_if_missing=False,
        )
        config.set_current_config(self.cfg)
        routes.reset_cache()
        from server.api_login import reset_locks
        reset_locks()

    def tearDown(self):
        config.set_current_config(None)
        routes.reset_cache()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _login_handler(self):
        from server.api_login import ROUTES
        return ROUTES[("POST", "/api/login")]

    def _call(self, handler, method, path, body_obj=None, headers=None):
        body = json.dumps(body_obj).encode() if body_obj is not None else b""
        return handler(method, path, {}, body, headers or {})


class TestLoginSuccess(_ServerTestBase):
    def test_admin_login_returns_token_and_default_group(self):
        h = self._login_handler()
        status, payload = self._normalize(self._call(h, "POST", "/api/login",
                                                     {"login": "admin", "password": "admin123"}))
        self.assertEqual(status, 200)
        self.assertIn("token", payload)
        self.assertTrue(payload["token"])
        self.assertIn("user", payload)
        self.assertEqual(set(payload["user"].keys()), {"id", "login", "global_role"})
        self.assertEqual(payload["user"]["login"], "admin")
        self.assertEqual(payload["user"]["global_role"], "admin")
        # 勘误3：admin → 根组
        self.assertEqual(payload["default_group_id"], self.fix["root"])

    def test_password_hash_never_in_response(self):
        """🔴 安全铁律：响应绝不回传 password_hash。"""
        h = self._login_handler()
        status, payload = self._normalize(self._call(h, "POST", "/api/login",
                                                     {"login": "admin", "password": "admin123"}))
        self.assertNotIn("password_hash", payload)
        self.assertNotIn("password_hash", payload.get("user", {}))
        # 整个序列化串里都不应出现 password_hash
        self.assertNotIn("password_hash", json.dumps(payload, ensure_ascii=False))

    def test_teacher_login_default_group_is_grade(self):
        """teacher 在年级授 propose → default_group = 年级（层级最高授权组）。"""
        h = self._login_handler()
        status, payload = self._normalize(self._call(h, "POST", "/api/login",
                                                     {"login": "t1", "password": "t1pw"}))
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["global_role"], "teacher")
        self.assertEqual(payload["default_group_id"], self.fix["grade"])

    @staticmethod
    def _normalize(result):
        # 三元组 = (status, payload, extra_headers)（429 带 Retry-After 走这条）。
        # 旧版只认二元组 → 三元组被当成 200，看起来像「锁定失效」。断言未改。
        if isinstance(result, tuple) and isinstance(result[0], int) and len(result) in (2, 3):
            return result[0], result[1]
        return 200, result


class TestLoginBadCredentials(_ServerTestBase):
    def test_wrong_password_401(self):
        h = self._login_handler()
        status, payload = self._normalize(self._call(h, "POST", "/api/login",
                                                     {"login": "admin", "password": "wrong"}))
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "bad_credentials")

    def test_nonexistent_login_401(self):
        h = self._login_handler()
        status, payload = self._normalize(self._call(h, "POST", "/api/login",
                                                     {"login": "nobody", "password": "x"}))
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "bad_credentials")

    def test_missing_fields_400(self):
        h = self._login_handler()
        status, payload = self._normalize(self._call(h, "POST", "/api/login",
                                                     {"login": "admin"}))  # 缺 password
        self.assertEqual(status, 400)

    @staticmethod
    def _normalize(result):
        # 三元组 = (status, payload, extra_headers)（429 带 Retry-After 走这条）。
        # 旧版只认二元组 → 三元组被当成 200，看起来像「锁定失效」。断言未改。
        if isinstance(result, tuple) and isinstance(result[0], int) and len(result) in (2, 3):
            return result[0], result[1]
        return 200, result


class TestLoginLockout(_ServerTestBase):
    def test_five_failures_then_429(self):
        """连续失败 5 次后第 6 次返回 429（组合锁 60 秒，2026-10-25 起口径）。"""
        h = self._login_handler()
        for i in range(5):
            s, _ = self._normalize(self._call(h, "POST", "/api/login",
                                              {"login": "admin", "password": "wrong"}))
            self.assertEqual(s, 401)
        s, payload = self._normalize(self._call(h, "POST", "/api/login",
                                                {"login": "admin", "password": "wrong"}))
        self.assertEqual(s, 429)

    @staticmethod
    def _normalize(result):
        # 三元组 = (status, payload, extra_headers)（429 带 Retry-After 走这条）。
        # 旧版只认二元组 → 三元组被当成 200，看起来像「锁定失效」。断言未改。
        if isinstance(result, tuple) and isinstance(result[0], int) and len(result) in (2, 3):
            return result[0], result[1]
        return 200, result


if __name__ == "__main__":
    unittest.main(verbosity=2)
