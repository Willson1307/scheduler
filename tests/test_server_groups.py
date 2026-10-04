"""B2 服务端组列表测试：GET /api/groups（分支B）。

覆盖（docs/07 §2）：
  - 未登录 → 401
  - 无效 token → 401
  - admin → 返回全部组
  - teacher → 只返回有授权（含继承）的组，未授权的根本不列出
  - 响应项含 group_id / name / level / member_count / parent_name
  - member_count = 该组后代中 kind='user' 的人数

运行：python -m unittest discover -s tests -p "test_server_groups.py" -v
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
    og.add_edge(conn, root, a_obj)  # admin 挂根组下（是学校成员）
    # teacher（在年级授 propose）
    t_obj = og.create_object(conn, org, "user", "王老师")
    og.create_user(conn, t_obj, "t1", "t1pw", role="teacher")
    og.add_edge(conn, root, t_obj)  # teacher 挂根组下
    tid = conn.execute("SELECT id FROM users WHERE login='t1'").fetchone()["id"]
    authz.grant_role(conn, org, grade, tid, "propose")
    # 给班级挂两个学生（kind=user），验证 member_count
    s1 = og.create_object(conn, org, "user", "学生甲")
    s2 = og.create_object(conn, org, "user", "学生乙")
    og.add_edge(conn, cls, s1)
    og.add_edge(conn, cls, s2)
    conn.commit()
    conn.close()
    return {"org": org, "root": root, "grade": grade, "cls": cls,
            "teacher_id": tid, "students": [s1, s2]}


class _GroupsTestBase(unittest.TestCase):
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

    def _groups_handler(self):
        from server.api_groups import ROUTES
        return ROUTES[("GET", "/api/groups")]

    def _login_token(self, login, password):
        from server.api_login import ROUTES
        h = ROUTES[("POST", "/api/login")]
        body = json.dumps({"login": login, "password": password}).encode()
        result = h("POST", "/api/login", {}, body, {})
        payload = result[1] if isinstance(result, tuple) else result
        return payload["token"]

    def _call_groups(self, token=None):
        h = self._groups_handler()
        headers = {"authorization": f"Bearer {token}"} if token else {}
        result = h("GET", "/api/groups", {}, b"", headers)
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            return result[0], result[1]
        return 200, result


class TestGroupsAuth(_GroupsTestBase):
    def test_no_token_401(self):
        status, payload = self._call_groups()
        self.assertEqual(status, 401)
        self.assertIn("error", payload)

    def test_invalid_token_401(self):
        status, payload = self._call_groups(token="garbage.notvalid")
        self.assertEqual(status, 401)

    def test_malformed_auth_header_401(self):
        h = self._groups_handler()
        result = h("GET", "/api/groups", {}, b"", {"authorization": "Basic abc"})
        status = result[0] if isinstance(result, tuple) else 200
        self.assertEqual(status, 401)


class TestGroupsAdmin(_GroupsTestBase):
    def test_admin_sees_all_groups(self):
        token = self._login_token("admin", "admin123")
        status, payload = self._call_groups(token)
        self.assertEqual(status, 200)
        ids = {g["group_id"] for g in payload}
        # admin 看到全部组（根组、年级、班级）
        self.assertIn(self.fix["root"], ids)
        self.assertIn(self.fix["grade"], ids)
        self.assertIn(self.fix["cls"], ids)

    def test_response_shape(self):
        token = self._login_token("admin", "admin123")
        status, payload = self._call_groups(token)
        self.assertEqual(status, 200)
        self.assertGreater(len(payload), 0)
        item = payload[0]
        self.assertEqual(set(item.keys()),
                         {"group_id", "name", "level", "member_count", "parent_name"})

    def test_member_count_counts_users(self):
        """班级挂了 2 个学生 → member_count == 2。"""
        token = self._login_token("admin", "admin123")
        status, payload = self._call_groups(token)
        by_id = {g["group_id"]: g for g in payload}
        self.assertEqual(by_id[self.fix["cls"]]["member_count"], 2)
        # 年级包含班级的学生（后代）→ member_count >= 2
        self.assertGreaterEqual(by_id[self.fix["grade"]]["member_count"], 2)
        # 根组包含所有人 → member_count == 4（admin+teacher+2学生）
        self.assertEqual(by_id[self.fix["root"]]["member_count"], 4)

    def test_parent_name_root_is_none(self):
        """根组 parent_name 为 None（勘误3 判据）。"""
        token = self._login_token("admin", "admin123")
        status, payload = self._call_groups(token)
        by_id = {g["group_id"]: g for g in payload}
        self.assertIsNone(by_id[self.fix["root"]]["parent_name"])
        self.assertEqual(by_id[self.fix["cls"]]["parent_name"], "高一")


class TestGroupsTeacher(_GroupsTestBase):
    def test_teacher_only_sees_authorized(self):
        """teacher 在年级授 propose → 只看到年级+班级（继承），看不到根组。"""
        token = self._login_token("t1", "t1pw")
        status, payload = self._call_groups(token)
        self.assertEqual(status, 200)
        ids = {g["group_id"] for g in payload}
        self.assertIn(self.fix["grade"], ids)
        self.assertIn(self.fix["cls"], ids)  # 继承
        self.assertNotIn(self.fix["root"], ids)  # 未授权不列出

    def test_teacher_level_capped_by_ceiling(self):
        """teacher 授 propose（天花板也是 propose）→ level 显示 propose。"""
        token = self._login_token("t1", "t1pw")
        status, payload = self._call_groups(token)
        by_id = {g["group_id"]: g for g in payload}
        self.assertEqual(by_id[self.fix["grade"]]["level"], "propose")
        # 班级是继承来的，effective 也是 propose
        self.assertEqual(by_id[self.fix["cls"]]["level"], "propose")


if __name__ == "__main__":
    unittest.main(verbosity=2)
