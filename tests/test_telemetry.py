"""S14 埋点后端测试（feat/ops）。

两部分：
  A. 端到端（真实 HTTP + port=0，照 r13kE9 范式）：401/403/400/200/聚合权限。
  B. 纯单元（:memory:）：trial_code 推导、org_kind 判定、无 login 哈希、
     聚合数值、聚合无个人字段、--assert-clean 会红。

只用 Python 标准库。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from scheduler import authz, object_graph as og  # noqa: E402
from scheduler.db import connect, init_db, new_id  # noqa: E402
from server import config, routes, telemetry  # noqa: E402
from server import auth as server_auth  # noqa: E402
from server.http_api import SchedulerHTTPServer  # noqa: E402
import telemetry_report  # noqa: E402

TODAY = date(2026, 9, 8)
B_MARK = "乙"
A_MARK = "甲"

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _build_two_orgs(db_path):
    """建两个组织 A「甲校」/ B「乙校」，A 含低权限用户 aviewer（view 档）。照 r13kE9。"""
    conn = connect(db_path)
    init_db(conn)

    a_org, a_root = og.create_organization(conn, "甲校")
    a_grade = og.create_object(conn, a_org, "group", "甲高一")
    a_cls = og.create_object(conn, a_org, "group", "甲高一1班")
    og.add_edge(conn, a_root, a_grade)
    og.add_edge(conn, a_grade, a_cls)
    a_admin_obj = og.create_object(conn, a_org, "user", "甲管理员")
    og.create_user(conn, a_admin_obj, "aadmin", "pw", role="admin")
    og.add_edge(conn, a_root, a_admin_obj)
    a_view_obj = og.create_object(conn, a_org, "user", "甲学生甲")
    og.create_user(conn, a_view_obj, "aviewer", "pw", role="student")
    og.add_edge(conn, a_cls, a_view_obj)
    a_viewer_uid = conn.execute("SELECT id FROM users WHERE login='aviewer'").fetchone()["id"]
    authz.grant_role(conn, a_org, a_cls, a_viewer_uid, "view")

    b_org, b_root = og.create_organization(conn, "乙校")
    b_grade = og.create_object(conn, b_org, "group", "乙高一")
    b_cls = og.create_object(conn, b_org, "group", "乙高一1班")
    og.add_edge(conn, b_root, b_grade)
    og.add_edge(conn, b_grade, b_cls)
    b_admin_obj = og.create_object(conn, b_org, "user", "乙管理员")
    og.create_user(conn, b_admin_obj, "badmin", "pw", role="admin")
    og.add_edge(conn, b_root, b_admin_obj)

    conn.commit()
    conn.close()
    return {"a_org": a_org, "a_cls": a_cls, "b_org": b_org, "b_cls": b_cls}


def _build_trial_org(conn, name="试用学校"):
    """建 trial 组织（login 带 trial_ 前缀）。"""
    org_id, root = og.create_organization(conn, name)
    admin_obj = og.create_object(conn, org_id, "user", "示例管理员")
    og.add_edge(conn, root, admin_obj)
    og.create_user(conn, admin_obj, "trial_admin", "trial123", role="admin")
    for k in (1, 2):
        t = og.create_object(conn, org_id, "user", f"示例教师{k:02d}")
        og.add_edge(conn, root, t)
        og.create_user(conn, t, f"trial_t{k:02d}", "123456", role="teacher")
    conn.commit()
    return org_id


def _build_demo_org(conn, name="试用学校"):
    """建 demo 组织（login 无 trial_ 前缀）。"""
    org_id, root = og.create_organization(conn, name)
    admin_obj = og.create_object(conn, org_id, "user", "系统管理员")
    og.add_edge(conn, root, admin_obj)
    og.create_user(conn, admin_obj, "admin", "admin123", role="admin")
    t = og.create_object(conn, org_id, "user", "教师01")
    og.add_edge(conn, root, t)
    og.create_user(conn, t, "t01", "123456", role="teacher")
    conn.commit()
    return org_id


def _insert_raw(conn, ts, trial_code, session_id, event, payload, org_id, org_kind):
    """直插一条带指定 ts 的事件（供确定性聚合测试）。"""
    conn.execute(
        "INSERT INTO event_log (id, ts, trial_code, session_id, event, payload, org_id, org_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id("log"), ts, trial_code, session_id, event,
         json.dumps(payload, ensure_ascii=False), org_id, org_kind),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# A. 端到端（真实 HTTP）
# ---------------------------------------------------------------------------


class TelemetryE2ETest(unittest.TestCase):
    """真实 HTTP 越权/鉴权测试：起服务 + http.client 发请求（照 r13kE9）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="telemetry_e2e_")
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

    def _ping(self, token=None, body=None):
        return self._request("POST", "/api/ping", token=token, body=body)

    def _telemetry(self, token=None):
        return self._request("GET", "/api/telemetry", token=token)

    # ---- 1. 未登录 / 坏令牌 → 401 --------------------------------------

    def test_ping_no_token_401(self):
        status, _ = self._ping(body=json.dumps({"event": "task_start", "session_id": "s"}))
        self.assertEqual(status, 401)

    def test_ping_invalid_token_401(self):
        status, _ = self._ping(token="garbage.notvalid",
                               body=json.dumps({"event": "task_start", "session_id": "s"}))
        self.assertEqual(status, 401)

    # ---- 2. 跨组织（A 低权限账号写 B 的 group）→ 403 -------------------

    def test_ping_cross_org_group_403(self):
        b_cls = self.fix["b_cls"]
        body = json.dumps({"event": "task_start", "session_id": "sess-xyz", "group": b_cls})
        status, resp = self._ping(token=self.a_viewer_token, body=body)
        self.assertEqual(status, 403)
        self.assertNotIn(B_MARK, resp, "响应体泄漏了 B 组织数据字段")

    # ---- 3. 正常写入 → 200，事件落库 -----------------------------------

    def test_ping_success_200(self):
        a_cls = self.fix["a_cls"]
        body = json.dumps({"event": "task_start", "session_id": "sess-ok-123", "group": a_cls})
        status, resp = self._ping(token=self.a_viewer_token, body=body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(resp), {"ok": True})
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT trial_code, session_id, event, org_kind FROM event_log "
                "WHERE session_id = 'sess-ok-123'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "事件未落库")
        self.assertEqual(row["trial_code"], "")       # aviewer 非 trial_ 登录 → 空短码
        self.assertEqual(row["session_id"], "sess-ok-123")
        self.assertEqual(row["event"], "task_start")
        self.assertEqual(row["org_kind"], "demo")      # 甲校无 trial_ 登录 → demo

    # ---- 4. 参数校验 → 400 --------------------------------------------

    def test_ping_unknown_event_400(self):
        body = json.dumps({"event": "bogus", "session_id": "sess-xyz"})
        status, _ = self._ping(token=self.a_viewer_token, body=body)
        self.assertEqual(status, 400)

    def test_ping_missing_session_400(self):
        body = json.dumps({"event": "task_start"})
        status, _ = self._ping(token=self.a_viewer_token, body=body)
        self.assertEqual(status, 400)

    # ---- 5. 聚合读取权限 ----------------------------------------------

    def test_telemetry_no_token_401(self):
        status, _ = self._telemetry()
        self.assertEqual(status, 401)

    def test_telemetry_non_admin_403(self):
        status, _ = self._telemetry(token=self.a_viewer_token)
        self.assertEqual(status, 403)

    def test_telemetry_admin_200_no_personal(self):
        status, resp = self._telemetry(token=self.a_admin_token)
        self.assertEqual(status, 200)
        agg = json.loads(resp)
        self.assertEqual(set(agg.keys()), {"participants", "duration", "completion", "satisfaction"})
        # 逐字段断言：聚合结果不含姓名/班级/登录名（甲/乙/aadmin 均不出现，且无中文）
        blob = json.dumps(agg, ensure_ascii=False)
        self.assertNotIn(A_MARK, blob)
        self.assertNotIn(B_MARK, blob)
        self.assertNotIn("aadmin", blob)
        self.assertIsNone(_CJK_RE.search(blob), "聚合输出含中文（疑似个人字段）")


# ---------------------------------------------------------------------------
# B. 纯单元（:memory:）
# ---------------------------------------------------------------------------


class TelemetryUnitTest(unittest.TestCase):
    def _fresh(self):
        conn = connect(":memory:")
        init_db(conn)
        return conn

    def test_derive_trial_code(self):
        self.assertEqual(telemetry.derive_trial_code("trial_t01"), "T01")
        self.assertEqual(telemetry.derive_trial_code("trial_t12"), "T12")
        self.assertEqual(telemetry.derive_trial_code("admin"), "")
        self.assertEqual(telemetry.derive_trial_code("t01"), "")
        self.assertEqual(telemetry.derive_trial_code(""), "")

    def test_org_kind_detection(self):
        conn = self._fresh()
        try:
            trial_org = _build_trial_org(conn)
            demo_org = _build_demo_org(conn)
            self.assertEqual(telemetry.org_kind_of(conn, trial_org), "trial")
            self.assertEqual(telemetry.org_kind_of(conn, demo_org), "demo")
        finally:
            conn.close()

    def test_identity_not_login_hash(self):
        conn = self._fresh()
        try:
            org_id = _build_trial_org(conn)
            telemetry.write_event(conn, org_id=org_id, trial_code="T01",
                                  session_id="abc123XYZ_9", event="task_start",
                                  payload={}, org_kind="trial")
            rows = conn.execute("SELECT * FROM event_log").fetchall()
            digest = hashlib.sha256(("trial_t01" + "salt").encode()).hexdigest()
            for r in rows:
                for col in ("trial_code", "session_id", "event", "payload"):
                    self.assertNotIn(digest, r[col], f"{col} 含 login 哈希")
                    self.assertNotIn("trial_t01", r[col], f"{col} 直写登录名")
            # 身份是短码 T01，不是 login、不是哈希
            self.assertEqual(rows[0]["trial_code"], "T01")
        finally:
            conn.close()

    def test_aggregate_correctness(self):
        conn = self._fresh()
        try:
            org_id = _build_trial_org(conn)
            _insert_raw(conn, "2026-10-08T09:00:00", "T01", "s1", "task_start", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:01:00", "T01", "s1", "task_success", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:03:00", "T01", "s1", "task_start", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:10:00", "T01", "s1", "task_abandon", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:00:00", "T02", "s2", "task_start", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:00:30", "T02", "s2", "task_success", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:05:00", "T01", "s3", "survey", {"score": 4}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:06:00", "T02", "s4", "survey", {"score": 5}, org_id, "trial")

            agg = telemetry.aggregate(conn)

            self.assertEqual(agg["participants"], 2)
            d = agg["duration"]
            self.assertEqual(d["session_count"], 2)
            self.assertEqual(d["person_count"], 2)
            self.assertEqual(d["avg_session_seconds"], 105.0)  # (180+30)/2
            self.assertEqual(d["avg_person_seconds"], 105.0)
            c = agg["completion"]
            self.assertEqual(c["starts"], 3)
            self.assertEqual(c["success"], 2)
            self.assertEqual(c["abandon"], 1)
            self.assertEqual(c["completion_rate"], 0.6667)
            s = agg["satisfaction"]
            self.assertEqual(s["n"], 2)
            self.assertEqual(s["mean"], 4.5)
            self.assertEqual(s["ge4_ratio"], 1.0)
            self.assertEqual(s["distribution"], {1: 0, 2: 0, 3: 0, 4: 1, 5: 1})

            # 两次调用逐字一致
            self.assertEqual(agg, telemetry.aggregate(conn))
        finally:
            conn.close()

    def test_trial_admin_pollution_excluded(self):
        """rPm5gf 问题 11 回归：trial_admin（org_kind=trial 但 trial_code=''）的
        task_start/success/abandon、survey、会话时长一律不计入聚合，
        否则「完成率分母 > 人数」口径不自洽。"""
        conn = self._fresh()
        try:
            org_id = _build_trial_org(conn)
            # 正常试用教师 T01：1 start + 1 success + 1 份问卷 + 1 个会话
            _insert_raw(conn, "2026-10-08T09:00:00", "T01", "s1", "task_start", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:01:00", "T01", "s1", "task_success", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:05:00", "T01", "s5", "survey", {"score": 4}, org_id, "trial")
            # trial_admin（trial_code=''）污染数据：各类事件混入
            _insert_raw(conn, "2026-10-08T09:00:00", "", "adm1", "task_start", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:00:30", "", "adm1", "task_success", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:00:40", "", "adm1", "task_abandon", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:02:00", "", "adm2", "survey", {"score": 1}, org_id, "trial")

            agg = telemetry.aggregate(conn)

            self.assertEqual(agg["participants"], 1)
            c = agg["completion"]
            self.assertEqual(c["starts"], 1)      # adm1 的 start 不计入
            self.assertEqual(c["success"], 1)      # adm1 的 success 不计入
            self.assertEqual(c["abandon"], 0)      # adm1 的 abandon 不计入
            self.assertEqual(c["completion_rate"], 1.0)
            d = agg["duration"]
            self.assertEqual(d["session_count"], 1)  # adm1 会话不计入
            self.assertEqual(d["avg_session_seconds"], 60.0)
            s = agg["satisfaction"]
            self.assertEqual(s["n"], 1)            # adm2 的 1 分问卷不计入
            self.assertEqual(s["mean"], 4.0)
            self.assertEqual(s["distribution"], {1: 0, 2: 0, 3: 0, 4: 1, 5: 0})
        finally:
            conn.close()

    def test_aggregate_no_personal_fields(self):
        conn = self._fresh()
        try:
            org_id = _build_trial_org(conn)
            _insert_raw(conn, "2026-10-08T09:00:00", "T01", "s1", "task_start", {}, org_id, "trial")
            _insert_raw(conn, "2026-10-08T09:05:00", "T01", "s3", "survey", {"score": 4}, org_id, "trial")
            agg = telemetry.aggregate(conn)
            self.assertEqual(set(agg.keys()),
                             {"participants", "duration", "completion", "satisfaction"})
            blob = json.dumps(agg, ensure_ascii=False)
            self.assertNotIn("trial_", blob)
            self.assertIsNone(_CJK_RE.search(blob))
        finally:
            conn.close()

    def test_secret_persistent_across_reload(self):
        """rPm5gf 问题 9.1 回归：密钥持久化在 data/.auth_secret，
        模块重载（模拟服务重启）后旧令牌仍然有效，不再全量 401。"""
        import importlib

        token = server_auth.issue_token("u1", "t01", "teacher", "obj1")
        self.assertIsNotNone(server_auth.verify_token(token))

        importlib.reload(server_auth)  # 模拟重启：重新加载持久化密钥
        try:
            self.assertIsNotNone(
                server_auth.verify_token(token),
                "重启（reload）后旧令牌应仍有效（密钥持久化失效则此处为 None）",
            )
        finally:
            importlib.reload(server_auth)  # 恢复模块原状，避免影响后续测试

    def test_assert_clean_red_then_green(self):
        conn = self._fresh()
        try:
            org_id = _build_trial_org(conn)
            # 干净事件 → 通过
            _insert_raw(conn, "2026-10-08T09:00:00", "T01", "abc123XYZ_9",
                        "task_start", {"score": 4}, org_id, "trial")
            ok, _ = telemetry_report.assert_clean(conn)
            self.assertTrue(ok)

            # 注入「张三」（中文 → 违规）
            _insert_raw(conn, "2026-10-08T09:01:00", "T01", "abc123XYZ_8",
                        "task_start", {"note": "张三"}, org_id, "trial")
            ok2, viol = telemetry_report.assert_clean(conn)
            self.assertFalse(ok2, "注入中文姓名后 assert_clean 应报警")
            self.assertTrue(any("中文" in v for v in viol), f"违规行应含中文提示：{viol}")

            # 注入登录名直写 → 违规
            _insert_raw(conn, "2026-10-08T09:02:00", "trial_t01", "abc123XYZ_7",
                        "task_start", {}, org_id, "trial")
            ok3, viol3 = telemetry_report.assert_clean(conn)
            self.assertFalse(ok3)
            self.assertTrue(any("非短码" in v for v in viol3), f"违规行应含非短码提示：{viol3}")

            # 还原（删坏行）→ 通过
            conn.execute("DELETE FROM event_log WHERE payload LIKE '%张三%' OR trial_code = 'trial_t01'")
            conn.commit()
            ok4, _ = telemetry_report.assert_clean(conn)
            self.assertTrue(ok4)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
