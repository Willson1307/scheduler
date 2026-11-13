"""组织公告能力验收测试（卡 rTVvb8）。

为什么必须走**真实 HTTP**：
  1. 401 由 `@auth.require_auth` 装饰器在分发层产出，403 由 `authz.can` 判定，
     `B3.resolve_authorized_org` 抛的 `ApiError` 又被接口层转成嵌套错误体 ——
     这三层都发生在 handler 之外，直调 handler 看不到真正发出去的 JSON。
  2. 「拿别组织的 group / id 会怎样」只有在完整请求链上才成立
     （query 解析 → 组织解析 → SQL 带 org_id）。

三条本卡专用的硬钉子（缺一条不算交付）：
  · 未登录 401、无写权限 403；
  · 公告里的 `<img src=x onerror=alert(1)>` **原样当文本**（后端逐字保真 +
    前端静态断言「全站零 innerHTML 赋值」）；
  · 停用后 GET 不再返回该公告。

运行：python -m unittest tests.test_announcements -v
只用 Python 标准库。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from server import config, routes  # noqa: E402
from server.b_announcement_kernels import (  # noqa: E402
    ALREADY_INACTIVE,
    DEACTIVATED,
    deactivate,
    ensure_announcements,
    get_active,
    upsert,
)
from server.config import ServerConfig, ensure_database  # noqa: E402
from server.http_api import SchedulerHTTPServer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: seed_demo 的学期锚定 2026-09-07；今天取第 1 周内即可。
TODAY = date(2026, 9, 8)

#: XSS 载荷：前端若用 innerHTML 渲染，这里会真的产生一个 img 节点并触发 onerror。
XSS = '<img src=x onerror=alert(1)>'


class _HttpCase(unittest.TestCase):
    """真实 HTTP 基类：进程内起服务（port=0）+ 临时库。"""

    tmp = ""
    db_path = ""
    server = None
    thread = None
    port = 0

    @classmethod
    def _start(cls, cfg):
        cls.cfg = cfg
        config.set_current_config(cfg)
        routes.reset_cache()
        from server.api_login import reset_locks

        reset_locks()
        cls.server = SchedulerHTTPServer(("127.0.0.1", 0), cfg)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def _stop(cls):
        if cls.server is not None:
            cls.server.shutdown()
            cls.server.server_close()
            cls.server = None
        if cls.thread is not None:
            cls.thread.join(timeout=5)
            cls.thread = None
        config.set_current_config(None)
        routes.reset_cache()
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- 请求层 ----

    @classmethod
    def _raw(cls, method, path, token=None, payload=None):
        """发一次请求，返回 (status, 响应原文)。"""
        headers = {}
        body = None
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    @classmethod
    def _json(cls, method, path, token=None, payload=None):
        status, raw = cls._raw(method, path, token=token, payload=payload)
        try:
            return status, json.loads(raw), raw
        except ValueError:
            raise AssertionError(f"响应不是合法 JSON（HTTP {status}）：{raw[:300]}")

    @classmethod
    def _login(cls, login, password):
        status, body, raw = cls._json("POST", "/api/login", payload={
            "login": login, "password": password,
        })
        if status != 200 or not body.get("token"):
            raise AssertionError(f"登录失败 {login}: HTTP {status} {raw[:200]}")
        return body["token"], body["default_group_id"]

    # ---- 断言助手 ----

    def assertNestedError(self, status, body, want_status, want_code):
        self.assertEqual(status, want_status, f"响应体={body!r}")
        self.assertIsInstance(body.get("error"), dict, f"必须嵌套错误体：{body!r}")
        self.assertEqual(body["error"].get("code"), want_code, f"响应体={body!r}")
        self.assertTrue(body["error"].get("message"), f"message 不能为空：{body!r}")

    def count_rows(self):
        conn = connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) AS c FROM announcements").fetchone()["c"]
        finally:
            conn.close()

    def fetch_row(self, ann_id):
        conn = connect(self.db_path)
        try:
            return conn.execute(
                "SELECT * FROM announcements WHERE id = ?", (ann_id,)
            ).fetchone()
        finally:
            conn.close()


# =============================================================================
# 1. 单组织：鉴权 / 校验 / upsert / 停用 / XSS 保真
# =============================================================================
class AnnouncementApiTest(_HttpCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ann_api_")
        cls.db_path = os.path.join(cls.tmp, "demo.db")
        cfg = ServerConfig(host="127.0.0.1", port=0, db_path=cls.db_path, today=TODAY)
        ensure_database(cfg)          # 新库：init_db + seed_demo
        cls._start(cfg)
        cls.admin_token, cls.admin_group = cls._login("admin", "admin123")
        cls.teacher_token, cls.teacher_group = cls._login("t01", "123456")

    @classmethod
    def tearDownClass(cls):
        cls._stop()

    def setUp(self):
        # 每条用例从「无公告」开始，避免方法间互相依赖（公告是单行模型）
        conn = connect(self.db_path)
        ensure_announcements(conn)
        conn.execute("DELETE FROM announcements")
        conn.commit()
        conn.close()

    def _publish(self, title="本周三教研活动", body_md="## 安排\n- 14:00 开始", group=None):
        return self._json("POST", "/api/announcements", token=self.admin_token, payload={
            "group": self.admin_group if group is None else group,
            "title": title,
            "body_md": body_md,
        })

    # ---- #1 未登录 ----
    def test_unauthenticated_401(self):
        cases = [
            ("GET", "/api/announcements", None),
            ("POST", "/api/announcements", {"title": "x"}),
            ("DELETE", "/api/announcements?id=ann_x", None),
        ]
        for method, path, payload in cases:
            with self.subTest(f"{method} {path}"):
                status, body, _ = self._json(method, path, token=None, payload=payload)
                self.assertNestedError(status, body, 401, "unauthorized")

    # ---- #2 空公告 ----
    def test_get_empty_returns_null(self):
        status, body, _ = self._json(
            "GET", "/api/announcements?group=" + self.admin_group, token=self.admin_token
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"))
        self.assertIsNone(body.get("announcement"), "无公告必须是 null，不是 404 或 {}")

    # ---- #3 发布 → 读取 ----
    def test_admin_publish_then_get(self):
        status, body, _ = self._publish()
        self.assertEqual(status, 200, body)
        self.assertTrue(body["created"])
        ann = body["announcement"]
        self.assertEqual(ann["title"], "本周三教研活动")
        self.assertEqual(ann["body_md"], "## 安排\n- 14:00 开始")
        self.assertTrue(ann["updated_at"])
        self.assertTrue(ann["updated_by_name"], "更新人显示名不能为空")
        self.assertNotIn("org_id", ann, "不回传 org_id（组织由请求的 group 决定）")
        self.assertNotIn("active", ann)

        status, got, _ = self._json(
            "GET", "/api/announcements?group=" + self.admin_group, token=self.admin_token
        )
        self.assertEqual(status, 200, got)
        self.assertEqual(got["announcement"], ann, "GET 必须原样返回刚发布的公告")

    # ---- #4 教师可读不可写 ----
    def test_teacher_can_read_but_not_write(self):
        _status, pub, _ = self._publish()
        ann_id = pub["announcement"]["id"]

        status, got, _ = self._json(
            "GET", "/api/announcements?group=" + self.teacher_group, token=self.teacher_token
        )
        self.assertEqual(status, 200, got)
        self.assertEqual(got["announcement"]["id"], ann_id, "同组织教师应能看到公告")

        # 教师天花板 = propose（authz.ROLE_CEILING）→ 写一律 403
        status, body, _ = self._json("POST", "/api/announcements", token=self.teacher_token, payload={
            "group": self.teacher_group, "title": "教师改的", "body_md": "x",
        })
        self.assertNestedError(status, body, 403, "forbidden")

        status, body, _ = self._json(
            "DELETE", "/api/announcements?id=" + ann_id + "&group=" + self.teacher_group,
            token=self.teacher_token,
        )
        self.assertNestedError(status, body, 403, "forbidden")

        row = self.fetch_row(ann_id)
        self.assertEqual(row["active"], 1, "越权写请求不得改动数据")
        self.assertEqual(row["title"], "本周三教研活动")

    # ---- #5 XSS 往返保真 ----
    def test_xss_roundtrip_fidelity(self):
        body_md = XSS + "\n\n<script>alert(1)</script>\n\n&MARKER&"
        status, body, _ = self._publish(title=XSS, body_md=body_md)
        self.assertEqual(status, 200, body)

        status, raw_status = self._raw(
            "GET", "/api/announcements?group=" + self.admin_group, token=self.admin_token
        )
        self.assertEqual(status, 200)
        # 后端不做 HTML 转义：原样存、原样还
        self.assertIn("<img src=x onerror=alert(1)>", raw_status, "响应里必须保留字面 <img")
        self.assertNotIn("&lt;img", raw_status, "⛔ 后端不得转义正文")
        self.assertIn("&MARKER&", raw_status)

        got = json.loads(raw_status)["announcement"]
        self.assertEqual(got["title"], XSS, "标题必须逐字相等")
        self.assertEqual(got["body_md"], body_md, "正文必须逐字相等（不 strip、不转义）")

    # ---- #6 空标题 ----
    def test_blank_title_400(self):
        bad_payloads = [
            {"title": ""},
            {"title": "   "},
            {"title": "\u3000"},          # 全角空格：Python strip() 会去掉
            {"body_md": "没有 title 字段"},
            {"title": 123},
            {"title": None},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                payload = dict(payload, group=self.admin_group)
                status, body, _ = self._json(
                    "POST", "/api/announcements", token=self.admin_token, payload=payload
                )
                self.assertNestedError(status, body, 400, "bad_request")
        self.assertEqual(self.count_rows(), 0, "非法请求不得落库")

    # ---- #6b 标题过长 ----
    def test_title_too_long_400(self):
        status, body, _ = self._publish(title="标" * 201)
        self.assertNestedError(status, body, 400, "bad_request")

    # ---- #7 body_md 类型 ----
    def test_body_md_must_be_string_400(self):
        for bad in (123, {"a": 1}, ["x"]):
            with self.subTest(bad=bad):
                status, body, _ = self._json(
                    "POST", "/api/announcements", token=self.admin_token,
                    payload={"group": self.admin_group, "title": "t", "body_md": bad},
                )
                self.assertNestedError(status, body, 400, "bad_request")
        self.assertEqual(self.count_rows(), 0)

    # ---- #8 upsert 单行 + 停用后复活 ----
    def test_upsert_single_row_and_reactivate(self):
        _s, first, _ = self._publish(title="第一版", body_md="A")
        ann_id = first["announcement"]["id"]
        self.assertTrue(first["created"])
        self.assertEqual(self.count_rows(), 1)

        _s, second, _ = self._publish(title="第二版", body_md="B")
        self.assertFalse(second["created"], "同组织第二次发布是覆盖，不是新建")
        self.assertEqual(second["announcement"]["id"], ann_id, "覆盖必须保留同一 id")
        self.assertEqual(second["announcement"]["title"], "第二版")
        self.assertEqual(self.count_rows(), 1, "每组织恒一条，不得累积死行")

        status, body, _ = self._json(
            "DELETE", "/api/announcements?id=" + ann_id + "&group=" + self.admin_group,
            token=self.admin_token,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(self.fetch_row(ann_id)["active"], 0)

        _s, third, _ = self._publish(title="第三版", body_md="C")
        self.assertEqual(third["announcement"]["id"], ann_id, "停用后再发布应复用同一行")
        self.assertEqual(self.fetch_row(ann_id)["active"], 1, "🔴 重新发布必须把 active 置回 1")
        self.assertEqual(self.count_rows(), 1)

    # ---- #9 停用后 GET 不返回 ----
    def test_deactivate_then_get_null(self):
        _s, pub, _ = self._publish()
        ann_id = pub["announcement"]["id"]

        status, body, _ = self._json(
            "DELETE", "/api/announcements?id=" + ann_id + "&group=" + self.admin_group,
            token=self.admin_token,
        )
        self.assertEqual(status, 200, body)
        self.assertFalse(body["active"])
        self.assertFalse(body["already_inactive"])

        status, got, _ = self._json(
            "GET", "/api/announcements?group=" + self.admin_group, token=self.admin_token
        )
        self.assertEqual(status, 200)
        self.assertIsNone(got["announcement"], "停用后 GET 不得再返回该公告")

        row = self.fetch_row(ann_id)
        self.assertIsNotNone(row, "停用是软删，行必须保留（审计与 id 复用）")
        self.assertEqual(row["active"], 0)

    # ---- #10 停用幂等 ----
    def test_delete_idempotent(self):
        _s, pub, _ = self._publish()
        ann_id = pub["announcement"]["id"]
        path = "/api/announcements?id=" + ann_id + "&group=" + self.admin_group

        status, first, _ = self._json("DELETE", path, token=self.admin_token)
        self.assertEqual(status, 200, first)
        self.assertFalse(first["already_inactive"])

        status, second, _ = self._json("DELETE", path, token=self.admin_token)
        self.assertEqual(status, 200, "重复停用必须幂等（双击不报错）")
        self.assertTrue(second["already_inactive"])

    # ---- #11 缺 id ----
    def test_delete_missing_id_400(self):
        status, body, _ = self._json(
            "DELETE", "/api/announcements?group=" + self.admin_group, token=self.admin_token
        )
        self.assertNestedError(status, body, 400, "bad_request")

    # ---- #12 未知 id ----
    def test_delete_unknown_id_404(self):
        status, body, _ = self._json(
            "DELETE", "/api/announcements?id=ann_does_not_exist&group=" + self.admin_group,
            token=self.admin_token,
        )
        self.assertNestedError(status, body, 404, "not_found")

    # ---- #13 group 类型 ----
    def test_group_wrong_type_400(self):
        status, body, _ = self._json(
            "POST", "/api/announcements", token=self.admin_token,
            payload={"group": 123, "title": "t", "body_md": "b"},
        )
        self.assertNestedError(status, body, 400, "bad_request")

    # ---- #14 请求体坏 JSON ----
    def test_bad_json_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/announcements", body=b"{oops",
                         headers={"Authorization": "Bearer " + self.admin_token,
                                  "Content-Type": "application/json"})
            resp = conn.getresponse()
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()
        self.assertNestedError(status, body, 400, "bad_request")


# =============================================================================
# 2. 跨组织隔离（甲校 vs 乙校）
# =============================================================================
def _build_two_orgs(db_path):
    """两个组织，各一个 admin。⛔ 不给甲 admin 任何乙校授权。"""
    conn = connect(db_path)
    init_db(conn)

    a_org, a_root = og.create_organization(conn, "甲校")
    a_admin = og.create_object(conn, a_org, "user", "甲管理员")
    og.create_user(conn, a_admin, "aadmin", "pw", role="admin")
    og.add_edge(conn, a_root, a_admin)

    b_org, b_root = og.create_organization(conn, "乙校")
    b_admin = og.create_object(conn, b_org, "user", "乙管理员")
    og.create_user(conn, b_admin, "badmin", "pw", role="admin")
    og.add_edge(conn, b_root, b_admin)

    conn.commit()
    conn.close()
    return {"a_org": a_org, "a_root": a_root, "b_org": b_org, "b_root": b_root}


class AnnouncementIsolationTest(_HttpCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ann_iso_")
        cls.db_path = os.path.join(cls.tmp, "two_orgs.db")
        cls.fix = _build_two_orgs(cls.db_path)
        cfg = ServerConfig(host="127.0.0.1", port=0, db_path=cls.db_path, today=TODAY)
        cls._start(cfg)
        cls.a_token, cls.a_group = cls._login("aadmin", "pw")
        cls.b_token, cls.b_group = cls._login("badmin", "pw")

    @classmethod
    def tearDownClass(cls):
        cls._stop()

    def setUp(self):
        conn = connect(self.db_path)
        ensure_announcements(conn)
        conn.execute("DELETE FROM announcements")
        conn.commit()
        conn.close()

    def _publish_as_b(self, title="乙校公告-机密"):
        status, body, _ = self._json("POST", "/api/announcements", token=self.b_token, payload={
            "group": self.b_group, "title": title, "body_md": "乙校内部正文",
        })
        self.assertEqual(status, 200, body)
        return body["announcement"]["id"]

    # ---- #15 跨组织 group → 403 ----
    def test_cross_org_group_403(self):
        self._publish_as_b()
        b_group = self.fix["b_root"]

        cases = [
            ("GET", "/api/announcements?group=" + b_group, None),
            ("POST", "/api/announcements", {"group": b_group, "title": "入侵", "body_md": "x"}),
            ("DELETE", "/api/announcements?id=ann_x&group=" + b_group, None),
        ]
        for method, path, payload in cases:
            with self.subTest(f"{method} {path}"):
                status, body, raw = self._json(
                    method, path, token=self.a_token, payload=payload
                )
                self.assertNestedError(status, body, 403, "forbidden")
                self.assertNotIn("乙校公告-机密", raw, "响应体泄漏了乙组织数据")
                self.assertNotIn(self.fix["b_org"], raw, "响应体泄漏了乙组织 id")

    # ---- #16 不传 group 也不泄漏 ----
    def test_no_group_does_not_leak_other_org(self):
        self._publish_as_b()
        status, body, raw = self._json("GET", "/api/announcements", token=self.a_token)
        self.assertEqual(status, 200, body)
        self.assertNotIn("乙校公告-机密", raw, "默认组泄漏了乙组织公告")
        self.assertNotIn(self.fix["b_org"], raw)
        self.assertIsNone(body["announcement"], "甲校自己没有公告，就必须是 null")

    # ---- #17 跨组织 id 停用 → 404，且不影响乙 ----
    def test_cross_org_id_delete_404(self):
        b_id = self._publish_as_b()
        status, body, raw = self._json(
            "DELETE", "/api/announcements?id=" + b_id + "&group=" + self.a_group,
            token=self.a_token,
        )
        self.assertNestedError(status, body, 404, "not_found")
        self.assertNotIn("乙校公告-机密", raw)
        self.assertEqual(
            self.fetch_row(b_id)["active"], 1, "跨组织停用不得改动对方数据"
        )

    # ---- #18 各看各的 ----
    def test_each_org_sees_only_own(self):
        status, pub_a, _ = self._json("POST", "/api/announcements", token=self.a_token, payload={
            "group": self.a_group, "title": "甲校公告", "body_md": "甲正文",
        })
        self.assertEqual(status, 200, pub_a)
        self._publish_as_b()

        status, got_a, raw_a = self._json(
            "GET", "/api/announcements?group=" + self.fix["a_root"], token=self.a_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(got_a["announcement"]["title"], "甲校公告")
        self.assertNotIn("乙校公告-机密", raw_a)

        status, got_b, raw_b = self._json(
            "GET", "/api/announcements?group=" + self.fix["b_root"], token=self.b_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(got_b["announcement"]["title"], "乙校公告-机密")
        self.assertNotIn("甲校公告", raw_b)


# =============================================================================
# 3. 领域内核：旧库补表 / 幂等 / 唯一约束
# =============================================================================
class AnnouncementKernelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ann_kernel_")
        self.db_path = os.path.join(self.tmp, "k.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)
        # 用 object_graph 建组织 + 用户：users.object_id 有外键，手插假 id 会被 FK 拒
        self.org_id, self.root_id = og.create_organization(self.conn, "内核测试校")
        obj_id = og.create_object(self.conn, self.org_id, "user", "内核测试用户")
        og.add_edge(self.conn, self.root_id, obj_id)
        self.user_id = og.create_user(self.conn, obj_id, "kuser", "pw", role="admin")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- #19 旧库补表 + 幂等 ----
    def test_ensure_idempotent_and_recreates_missing_table(self):
        # 模拟「已存在的 data/*.db 没有新表」
        self.conn.execute("DROP TABLE announcements")
        self.conn.commit()
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='announcements'"
            ).fetchone()
        )

        ensure_announcements(self.conn)
        ensure_announcements(self.conn)          # 幂等：连调两次不报错
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='announcements'"
            ).fetchone()
        )

        upsert(self.conn, self.org_id, self.user_id, "标题", "正文")
        ensure_announcements(self.conn)          # 已有数据时再调不得清表
        got = get_active(self.conn, self.org_id)
        self.assertIsNotNone(got, "ensure 不得清掉已有公告")
        self.assertEqual(got["title"], "标题")

    # ---- #20 每组织恒一条：唯一约束 ----
    def test_unique_org_constraint(self):
        upsert(self.conn, self.org_id, self.user_id, "标题", "正文")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO announcements (id, org_id, title, body_md, active, updated_by)"
                " VALUES ('ann_dup', ?, '另一条', '', 1, ?)",
                (self.org_id, self.user_id),
            )

    # ---- #21 内核三态：停用 / 已停用 / 不存在 ----
    def test_deactivate_returns_three_states(self):
        row = upsert(self.conn, self.org_id, self.user_id, "标题", "正文")["announcement"]
        ann_id = row["id"]

        self.assertEqual(deactivate(self.conn, self.org_id, ann_id, self.user_id), DEACTIVATED)
        self.assertEqual(
            deactivate(self.conn, self.org_id, ann_id, self.user_id), ALREADY_INACTIVE
        )
        self.assertIsNone(deactivate(self.conn, self.org_id, "ann_nope", self.user_id))
        self.assertIsNone(deactivate(self.conn, "org_other", ann_id, self.user_id))


# =============================================================================
# 4. 前端静态断言：XSS 防线（禁 innerHTML）
# =============================================================================
def _read_app_js():
    with open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as f:
        return f.read()


def _function_body(src: str, name: str) -> str:
    """取 `function <name>(...) { ... }` 的函数体（按花括号配平，够用且不引依赖）。"""
    start = src.index("function " + name)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"找不到函数体：{name}")


class AnnouncementStaticXssTest(unittest.TestCase):
    """前端零测试框架，所以用源码级断言守住「页面不得产生 img 节点」的**根因**：

    公告正文只经 `mdToDom()` 构建 DOM，而它全程 createElement / textContent。
    一旦有人把正文改成 innerHTML，`<img src=x onerror=alert(1)>` 就会真的
    变成图片节点并执行脚本 —— 这条断言必须在那一刻变红。
    """

    def test_no_innerhtml_assignment_anywhere(self):
        src = _read_app_js()
        hits = re.findall(r"\.innerHTML\s*(?:\+)?=", src)
        self.assertEqual(hits, [], "web/app.js 出现 innerHTML 赋值（存储型 XSS 面）")
        self.assertNotIn("insertAdjacentHTML", src)
        self.assertNotIn("document.write", src)
        self.assertNotIn("outerHTML", src)

    def test_md_to_dom_is_pure_dom(self):
        body = _function_body(_read_app_js(), "mdToDom")
        for bad in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(bad, body, f"mdToDom 函数体出现 {bad}")

    def test_announcement_renderer_uses_md_to_dom(self):
        """公告渲染必须复用 mdToDom，⛔ 不许自己写 Markdown 解析器。"""
        src = _read_app_js()
        body = _function_body(src, "renderAnnouncement")
        self.assertIn("mdToDom(", body, "公告正文必须走 mdToDom()")
        self.assertIn("textContent", body, "标题等纯文本必须走 textContent")


if __name__ == "__main__":
    unittest.main()
