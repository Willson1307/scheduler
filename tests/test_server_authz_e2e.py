"""B3 越权（IDOR）端到端测试：真实 HTTP + port=0。

为什么走真实 HTTP（而非像 test_server_api.py 那样直接调 handler）：
  - @require_auth 装饰器在 handler 层拦 401，直接调 handler 也能测到；
    但真实 HTTP 额外覆盖「http_api.py 分发 → headers 透传 → context 注入」
    这条完整链路——若将来有人把鉴权从装饰器挪到分发层、或分发层改动
    headers 归一化，只有真实 HTTP 能抓住。
  - 本卡是「绿灯不等于正确」第 8 次教训的对症药，必须端到端。

覆盖（docs/21 §1.1 B2-1 越权判定标准）：
  1. 无 token → 401
  2. 无效 token → 401
  3. A 组织低权限账号（level=view）用 B 组织的 group 读三接口 → 403，
     且响应体不含 B 组织任何数据字段
  4. A 账号不传 group（走默认组）→ 200 且不泄漏 B 组织数据
  5. /api/resolve（B4 跟进 rjsdbj）：GET 预览 / POST 执行同口径——
     跨组织 403，且 POST 后**查库断言**乙校课表三张表逐行未变
     （写越权比读越权多一层「数据真的没被动过」的证据要求）

运行：python -m unittest tests.test_server_authz_e2e -v
只用 Python 标准库。
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import authz  # noqa: E402
from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.schedules import NewSchedule, create_periodic  # noqa: E402
from scheduler.slots import bit, setup_default_slots  # noqa: E402
from server import api_resolve  # noqa: E402
from server import config, routes  # noqa: E402
from server.http_api import SchedulerHTTPServer  # noqa: E402

#: 用固定日期，避免依赖系统时钟（演示库约定 2026-09-08）。
TODAY = date(2026, 9, 8)

#: B 组织对象名带「乙」前缀，用于断言「响应体不含 B 数据字段」。
#: A 组织对象名带「甲」前缀。
B_MARK = "乙"
A_MARK = "甲"


def _build_two_orgs(db_path):
    """建两个组织 A「甲校」/ B「乙校」，A 含一个低权限用户 aviewer（view 档）。

    返回 dict：a_org/a_root/a_cls/b_org/b_cls 等关键 id。
    """
    conn = connect(db_path)
    init_db(conn)

    # ---- 组织 A：甲校 ----
    a_org, a_root = og.create_organization(conn, "甲校")
    a_grade = og.create_object(conn, a_org, "group", "甲高一")
    a_cls = og.create_object(conn, a_org, "group", "甲高一1班")
    og.add_edge(conn, a_root, a_grade)
    og.add_edge(conn, a_grade, a_cls)
    # A 管理员
    a_admin_obj = og.create_object(conn, a_org, "user", "甲管理员")
    og.create_user(conn, a_admin_obj, "aadmin", "pw", role="admin")
    og.add_edge(conn, a_root, a_admin_obj)
    # A 低权限账号：学生在甲高一1班，仅 view 档
    a_view_obj = og.create_object(conn, a_org, "user", "甲学生甲")
    og.create_user(conn, a_view_obj, "aviewer", "pw", role="student")
    og.add_edge(conn, a_cls, a_view_obj)
    a_viewer_uid = conn.execute(
        "SELECT id FROM users WHERE login='aviewer'"
    ).fetchone()["id"]
    authz.grant_role(conn, a_org, a_cls, a_viewer_uid, "view")

    # ---- 组织 B：乙校（数据名带「乙」） ----
    b_org, b_root = og.create_organization(conn, "乙校")
    b_grade = og.create_object(conn, b_org, "group", "乙高一")
    b_cls = og.create_object(conn, b_org, "group", "乙高一1班")
    og.add_edge(conn, b_root, b_grade)
    og.add_edge(conn, b_grade, b_cls)
    b_admin_obj = og.create_object(conn, b_org, "user", "乙管理员")
    og.create_user(conn, b_admin_obj, "badmin", "pw", role="admin")
    og.add_edge(conn, b_root, b_admin_obj)

    # ---- B 组织课表（B4 跟进 rjsdbj）----
    # 两条同班 / 同教师 / 同节次（周三第 1 节）的冲突课：
    # 供「跨组织 POST /api/resolve 后乙校数据未被修改」的**查库断言**用——
    # 若越权漏判，POST 会真的消解并改写这两行的 schedule_periodic.slot_bitmap
    # （m2.apply_plan 只 UPDATE schedule_periodic / schedule_once，见 m2.py:383）。
    setup_default_slots(conn, b_org)
    b_teacher = og.create_object(conn, b_org, "user", "乙教师")
    og.add_edge(conn, b_root, b_teacher)
    for tag in ("A", "B"):
        create_periodic(
            conn,
            NewSchedule(
                title=f"乙物理（冲突演示{tag}）",
                org_id=b_org,
                created_by=b_admin_obj,
                participants={"target": [b_cls], "teacher": [b_teacher]},
            ),
            slot_bitmap=bit(3, 1),  # 周三第 1 节，两条同槽 → 真实冲突
            week_parity=0,
            valid_from="2026-09-07",  # 覆盖 TODAY=2026-09-08 所在周
            valid_to="2026-09-30",
        )

    conn.commit()
    conn.close()
    return {
        "a_org": a_org, "a_root": a_root, "a_cls": a_cls,
        "b_org": b_org, "b_root": b_root, "b_cls": b_cls,
    }


class AuthzE2ETest(unittest.TestCase):
    """真实 HTTP 越权测试：起服务 + http.client 发请求。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="authz_e2e_")
        cls.db_path = os.path.join(cls.tmp, "e2e.db")
        cls.fix = _build_two_orgs(cls.db_path)

        cls.cfg = config.ServerConfig(
            host="127.0.0.1", port=0, db_path=cls.db_path,
            today=TODAY, seed_if_missing=False,
        )
        config.set_current_config(cls.cfg)
        routes.reset_cache()
        from server.api_login import reset_locks
        reset_locks()

        cls.server = SchedulerHTTPServer(("127.0.0.1", 0), cls.cfg)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        # 登录取 token（真实 HTTP POST /api/login）
        cls.a_viewer_token = cls._login("aviewer", "pw")
        cls.a_admin_token = cls._login("aadmin", "pw")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        config.set_current_config(None)
        routes.reset_cache()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- HTTP 辅助 ------------------------------------------------------

    def setUp(self):
        # /api/resolve 限频是模块级滑动窗口（10 次/60 秒/ token），
        # 不清会让同 token 的多个测试方法互相挤占额度（429 误报）。
        api_resolve._rate_buckets.clear()

    @classmethod
    def _request(cls, method, path, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            conn.request(method, path, body=body, headers=headers)
        else:
            conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        status = resp.status
        conn.close()
        return status, data

    @classmethod
    def _login(cls, login, password):
        body = json.dumps({"login": login, "password": password}).encode("utf-8")
        status, data = cls._request("POST", "/api/login", body=body)
        assert status == 200, f"登录失败 {login}: {status} {data}"
        return json.loads(data)["token"]

    def _get(self, path, token=None):
        return self._request("GET", path, token=token)

    def _post(self, path, token=None, body=None):
        return self._request("POST", path, token=token, body=body)

    def _dump_b_schedules(self):
        """乙校课表全量快照（schedules + 两张扩展表），供「数据未被修改」断言。

        消解写库只碰 schedule_periodic.slot_bitmap / schedule_once.start_at|end_at
        （m2.apply_plan），但快照把三张表都拍下来——万一将来写库范围扩大，
        这里也能当场抓到，而不是静默漏网。
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            snap = []
            for r in conn.execute(
                "SELECT id, kind, title FROM schedules WHERE org_id = ?"
                " ORDER BY id",
                (self.fix["b_org"],),
            ):
                snap.append(("schedules", r["id"], r["kind"], r["title"]))
            for r in conn.execute(
                "SELECT p.schedule_id, p.week_parity, p.slot_bitmap,"
                " p.valid_from, p.valid_to"
                " FROM schedule_periodic p JOIN schedules s ON s.id = p.schedule_id"
                " WHERE s.org_id = ? ORDER BY p.schedule_id",
                (self.fix["b_org"],),
            ):
                snap.append(("periodic",) + tuple(r))
            for r in conn.execute(
                "SELECT o.schedule_id, o.start_at, o.end_at"
                " FROM schedule_once o JOIN schedules s ON s.id = o.schedule_id"
                " WHERE s.org_id = ? ORDER BY o.schedule_id",
                (self.fix["b_org"],),
            ):
                snap.append(("once",) + tuple(r))
            return snap
        finally:
            conn.close()

    # ---- 1. 未登录 / 坏令牌 → 401 --------------------------------------

    def test_no_token_401_objects(self):
        status, _ = self._get("/api/objects")
        self.assertEqual(status, 401)

    def test_no_token_401_schedules(self):
        status, _ = self._get("/api/schedules")
        self.assertEqual(status, 401)

    def test_no_token_401_conflicts(self):
        status, _ = self._get("/api/conflicts")
        self.assertEqual(status, 401)

    def test_invalid_token_401(self):
        status, _ = self._get("/api/objects", token="garbage.notvalid")
        self.assertEqual(status, 401)

    # ---- 2. 跨组织（A 低权限账号读 B 的 group）→ 403，无 B 数据 --------

    def test_cross_org_objects_forbidden(self):
        b_cls = self.fix["b_cls"]
        status, body = self._get(f"/api/objects?group={b_cls}", token=self.a_viewer_token)
        self.assertEqual(status, 403)
        self.assertNotIn(B_MARK, body, "响应体泄漏了 B 组织数据字段")

    def test_cross_org_schedules_forbidden(self):
        b_cls = self.fix["b_cls"]
        status, body = self._get(f"/api/schedules?group={b_cls}", token=self.a_viewer_token)
        self.assertEqual(status, 403)
        self.assertNotIn(B_MARK, body, "响应体泄漏了 B 组织数据字段")

    def test_cross_org_conflicts_forbidden(self):
        b_cls = self.fix["b_cls"]
        status, body = self._get(f"/api/conflicts?group={b_cls}", token=self.a_viewer_token)
        self.assertEqual(status, 403)
        self.assertNotIn(B_MARK, body, "响应体泄漏了 B 组织数据字段")

    # ---- 3. 默认组（不传 group）也不能跨组织 ---------------------------

    def test_default_group_objects_no_cross_org(self):
        status, body = self._get("/api/objects", token=self.a_viewer_token)
        self.assertEqual(status, 200)
        self.assertIn(A_MARK, body, "默认组应返回 A 组织自己的数据")
        self.assertNotIn(B_MARK, body, "默认组泄漏了 B 组织数据字段")

    def test_default_group_schedules_no_cross_org(self):
        status, body = self._get("/api/schedules", token=self.a_viewer_token)
        self.assertEqual(status, 200)
        self.assertNotIn(B_MARK, body, "默认组泄漏了 B 组织数据字段")

    def test_default_group_conflicts_no_cross_org(self):
        status, body = self._get("/api/conflicts", token=self.a_viewer_token)
        self.assertEqual(status, 200)
        self.assertNotIn(B_MARK, body, "默认组泄漏了 B 组织数据字段")

    # ---- 3b. /api/resolve（B4 跟进 rjsdbj）：读越权 + 写越权 -------------

    def test_no_token_401_resolve_get(self):
        status, _ = self._get("/api/resolve")
        self.assertEqual(status, 401)

    def test_no_token_401_resolve_post(self):
        status, _ = self._post("/api/resolve", body=b"{}")
        self.assertEqual(status, 401)

    def test_cross_org_resolve_get_forbidden(self):
        """GET 预览也是越权出口：能看到别组织的可动集合与消解方案。"""
        b_cls = self.fix["b_cls"]
        status, body = self._get(f"/api/resolve?group={b_cls}", token=self.a_viewer_token)
        self.assertEqual(status, 403)
        self.assertNotIn(B_MARK, body, "GET 预览泄漏了 B 组织数据字段")

    def test_cross_org_resolve_post_forbidden_and_no_write(self):
        """🔴 本卡核心断言：跨组织 POST → 403，且**对方课表数据未被修改**。

        只看 403 不够——必须查库确认乙校三张表逐行未变
        （乙校 fixture 特意造了两条真实冲突课，漏判时 POST 会真的消解它们）。
        """
        b_cls = self.fix["b_cls"]
        before = self._dump_b_schedules()
        status, body = self._post(
            f"/api/resolve?group={b_cls}", token=self.a_viewer_token, body=b"{}"
        )
        self.assertEqual(status, 403)
        self.assertNotIn(B_MARK, body)
        after = self._dump_b_schedules()
        self.assertEqual(
            before, after, "跨组织 POST 后乙校课表数据被修改（写越权未拦住）"
        )

    def test_default_group_resolve_no_cross_org(self):
        """不传 group 走默认组也不能跨组织（GET 200 + POST 后乙校数据未变）。

        ⚠️ 旧实现的坑是 `ORDER BY id LIMIT 1`——org id 是 uuid4，
        多组织下非确定性返回别家，此测试在旧实现下**可能假阴**（恰好选中甲校）；
        确定性抓越权靠上面显式传 group 的 403 测试。
        """
        before = self._dump_b_schedules()
        status, body = self._get("/api/resolve", token=self.a_viewer_token)
        self.assertEqual(status, 200)
        self.assertNotIn(B_MARK, body, "默认组泄漏了 B 组织数据字段")
        status, body = self._post("/api/resolve", token=self.a_viewer_token, body=b"{}")
        self.assertEqual(status, 200)
        self.assertEqual(
            before, self._dump_b_schedules(), "默认组 POST 后乙校数据被修改"
        )

    # ---- 4. 健康检查 / 登录保持公开 ------------------------------------

    def test_health_is_public(self):
        status, _ = self._get("/api/health")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
