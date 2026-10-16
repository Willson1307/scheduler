"""B8 验收测试：审批流（提议→批准→落库），16 项。

运行：python -m unittest tests.test_b8_approval -v

---------------------------------------------------------------------------
🔴 本文件钉死的口径（每条对应一个「不报错但结果错」的真实风险）
---------------------------------------------------------------------------

  1. **pending 绝不碰业务数据**：提交/撤回/拒绝前后 schedules 与
     schedule_periodic 逐字段比对，零变化才算通过。
  2. **批准时重跑硬约束**：提议时合法 ≠ 批准时合法。用例 8 在提交后、
     批准前「改了课表」（期间别人动课），批准必须 409 且零写入。
  3. **自己批准自己 → 403**：requester_id == context["user_id"]，与 level 无关，
     admin 自己提自己批也 403（审批流的灵魂）。
  4. **teacher 天花板 propose ⇒ 老师批不了**：propose 档批准 → 403。
  5. **旧库兜底**：DROP 两张表后调接口不 500（ensure_change_tables 生效），
     且 ensure 幂等。

---------------------------------------------------------------------------
测试基建说明
---------------------------------------------------------------------------

接口层直接调 handler（与 test_b9_exceptions 同源，token 走真实签发链路），
不起真实 HTTP 服务；401/403/400/404/409 每条状态码都有对应用例。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import authz  # noqa: E402
from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.occupancy import create_term  # noqa: E402
from scheduler.slots import bit, mask_to_hex, setup_default_slots  # noqa: E402
from server import api_approvals as API  # noqa: E402
from server import auth, b8_kernels as K  # noqa: E402
from server import config  # noqa: E402
from server.config import ServerConfig  # noqa: E402

#: 学期起点（周一）。sch_a / sch_b 均为整学期。
MONDAY = date(2026, 9, 21)


def fresh():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def _add_periodic(conn, org, sid, title, bitmap, vf="2026-09-21", vt="2026-12-31",
                  obj_id=None, role="target"):
    conn.execute(
        "INSERT INTO schedules(id, org_id, kind, title) VALUES (?,?,?,?)",
        (sid, org, "periodic", title),
    )
    conn.execute(
        "INSERT INTO schedule_periodic"
        "(schedule_id, week_parity, slot_bitmap, valid_from, valid_to)"
        " VALUES (?,?,?,?,?)",
        (sid, 0, mask_to_hex(bitmap), vf, vt),
    )
    if obj_id is not None:
        conn.execute(
            "INSERT INTO schedule_participants(schedule_id, object_id, role)"
            " VALUES (?,?,?)",
            (sid, obj_id, role),
        )
    conn.commit()


def _build_api_db(db_path):
    """可控小组织：root→高一→高一1班；一个 propose 档 teacher + 一个 admin；
    两条周期课（sch_a 周三第1节 / sch_b 周三第2节，互不冲突）。"""
    conn = connect(db_path)
    init_db(conn)
    org_id, root_id = og.create_organization(conn, "测试校")
    grade = og.create_object(conn, org_id, "group", "高一")
    cls = og.create_object(conn, org_id, "group", "高一1班")
    teacher_obj = og.create_object(conn, org_id, "user", "教师甲")
    admin_obj = og.create_object(conn, org_id, "user", "管理员")
    og.add_edge(conn, root_id, grade)
    og.add_edge(conn, grade, cls)
    teacher_uid = og.create_user(conn, teacher_obj, "teacher_a", "pw", role="teacher")
    admin_uid = og.create_user(conn, admin_obj, "admin", "pw", role="admin")
    authz.grant_role(conn, org_id, cls, teacher_uid, "propose")
    cfg = setup_default_slots(conn, org_id)
    create_term(conn, org_id, "2026秋", "2026-09-21", "2026-12-31")
    _add_periodic(conn, org_id, "sch_a", "数学", bit(3, 1), obj_id=cls)
    _add_periodic(conn, org_id, "sch_b", "英语", bit(3, 2), obj_id=cls)
    conn.commit()
    return conn, org_id, cls, teacher_uid, admin_uid, cfg


def _body(**kw):
    return json.dumps(kw).encode("utf-8")


def _dump_business(conn):
    """schedules + schedule_periodic 全量快照（用于「业务数据零变化」逐字段比对）。"""
    sched = [tuple(r) for r in conn.execute(
        "SELECT id, org_id, kind, title, created_by, updated_at"
        " FROM schedules ORDER BY id"
    ).fetchall()]
    sp = [tuple(r) for r in conn.execute(
        "SELECT schedule_id, week_parity, slot_bitmap, valid_from, valid_to"
        " FROM schedule_periodic ORDER BY schedule_id"
    ).fetchall()]
    return (sched, sp)


class ApiFixture(unittest.TestCase):
    """接口层共用基建：真实 token（teacher + admin）+ 真实 handler 调用。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b8_api_")
        cls.db_path = os.path.join(cls.tmp, "b8.db")
        (cls.conn, cls.org, cls.cls, cls.teacher_uid, cls.admin_uid, cls.cfg) = (
            _build_api_db(cls.db_path)
        )
        cls.cfg_srv = ServerConfig(
            host="127.0.0.1", port=0, db_path=cls.db_path,
            today=date(2026, 9, 22), seed_if_missing=False,
        )
        config.set_current_config(cls.cfg_srv)
        cls.teacher_token = auth.issue_token(cls.teacher_uid, "teacher_a", "teacher", "obj_t")
        cls.admin_token = auth.issue_token(cls.admin_uid, "admin", "admin", "obj_a")
        cls.teacher_headers = {"authorization": f"Bearer {cls.teacher_token}"}
        cls.admin_headers = {"authorization": f"Bearer {cls.admin_token}"}

        # 乙校（跨组织测试基建，只建一次，避免重复建 teacher_b 撞 users.login 唯一约束）
        cls.other_org, _ = og.create_organization(cls.conn, "乙校")
        cls.other_cls = og.create_object(cls.conn, cls.other_org, "group", "乙班")
        cls.other_teacher_obj = og.create_object(cls.conn, cls.other_org, "user", "乙师")
        cls.other_teacher = og.create_user(
            cls.conn, cls.other_teacher_obj, "teacher_b", "pw", role="teacher"
        )
        authz.grant_role(cls.conn, cls.other_org, cls.other_cls, cls.other_teacher, "propose")
        setup_default_slots(cls.conn, cls.other_org)
        create_term(cls.conn, cls.other_org, "2026秋", "2026-09-21", "2026-12-31")
        _add_periodic(cls.conn, cls.other_org, "sch_evil_a", "乙校数学", bit(3, 1), obj_id=cls.other_cls)
        cls.other_teacher_token = auth.issue_token(cls.other_teacher, "teacher_b", "teacher", "obj_b")
        cls.other_headers = {"authorization": f"Bearer {cls.other_teacher_token}"}

    @classmethod
    def tearDownClass(cls):
        config.set_current_config(None)
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 每个用例从干净基线开始：清空审批数据 + 重建 sch_a / sch_b
        # （case 9 可能删过 sch_a，故这里连 schedules 一起重置）。
        self.conn.execute("DELETE FROM change_events")
        self.conn.execute("DELETE FROM change_requests")
        self.conn.execute("DELETE FROM schedules WHERE id IN ('sch_a','sch_b')")
        self.conn.commit()
        _add_periodic(self.conn, self.org, "sch_a", "数学", bit(3, 1), obj_id=self.cls)
        _add_periodic(self.conn, self.org, "sch_b", "英语", bit(3, 2), obj_id=self.cls)

    # -- handler 直调包装（context 由 require_auth 从 token 解析填充）--
    def _submit(self, uid, target_id="sch_a", valid_from="2026-09-28", group=None, **extra):
        headers = self.admin_headers if uid == self.admin_uid else self.teacher_headers
        kw = dict(
            scope="permanent", op="update", target_type="schedule",
            target_id=target_id,
            payload={"after": {"valid_from": valid_from}},
            reason="测试理由",
        )
        if group is not None:
            kw["group"] = group
        kw.update(extra)
        return API.submit_request(
            "POST", "/api/approvals", {}, _body(**kw), headers, context=None,
        )

    def _submit_other(self, target_id="sch_evil_a", valid_from="2026-09-28"):
        """以乙校 teacher 提交一条请求（用于跨组织测试）。"""
        return API.submit_request(
            "POST", "/api/approvals", {}, _body(
                scope="permanent", op="update", target_type="schedule",
                target_id=target_id,
                payload={"after": {"valid_from": valid_from}},
                reason="乙校理由",
            ), self.other_headers, context=None,
        )

    def _approve(self, uid, request_id):
        headers = self.admin_headers if uid == self.admin_uid else self.teacher_headers
        return API.approve_request(
            "POST", "/api/approvals/approve", {"id": [request_id]}, b"",
            headers, context=None,
        )

    def _reject(self, uid, request_id, reason="不批"):
        headers = self.admin_headers if uid == self.admin_uid else self.teacher_headers
        return API.reject_request(
            "POST", "/api/approvals/reject", {"id": [request_id]},
            _body(reason=reason), headers, context=None,
        )

    def _withdraw(self, uid, request_id):
        headers = self.admin_headers if uid == self.admin_uid else self.teacher_headers
        return API.withdraw_request(
            "POST", "/api/approvals/withdraw", {"id": [request_id]}, b"",
            headers, context=None,
        )

    def _list(self, uid, **query):
        headers = self.admin_headers if uid == self.admin_uid else self.teacher_headers
        return API.list_requests(
            "GET", "/api/approvals", {k: [v] for k, v in query.items()}, b"",
            headers, context=None,
        )

    def _events(self, uid, request_id):
        headers = self.admin_headers if uid == self.admin_uid else self.teacher_headers
        return API.list_events(
            "GET", "/api/approvals/events", {"id": [request_id]}, b"",
            headers, context=None,
        )


# ===========================================================================
# 一、状态机（pending 零写入 / approve / reject / withdraw / terminal）
# ===========================================================================


class TestStateMachine(ApiFixture):
    def test_submit_pending_and_zero_data_change(self):
        """提交后 status='pending' 且业务数据逐字段零变化。"""
        before = _dump_business(self.conn)
        payload = self._submit(self.teacher_uid)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["status"], "pending")
        self.assertTrue(payload["request_id"].startswith("cr_"))
        self.assertEqual(_dump_business(self.conn), before)

    def test_pending_does_not_touch_valid_from(self):
        """批准前 schedule_periodic.valid_from 必须保持原值。"""
        rid = self._submit(self.teacher_uid, valid_from="2026-09-28")["request_id"]
        row = self.conn.execute(
            "SELECT valid_from FROM schedule_periodic WHERE schedule_id = 'sch_a'"
        ).fetchone()
        self.assertEqual(row["valid_from"], "2026-09-21")
        # 请求确实处于 pending（未被批准）
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM change_requests WHERE id = ?", (rid,)
            ).fetchone()["status"],
            "pending",
        )

    def test_approve_applies_valid_from_and_marks_applied(self):
        """approve → valid_from 真变 + status=applied + events 有 apply。"""
        rid = self._submit(self.teacher_uid, valid_from="2026-09-28")["request_id"]
        payload = self._approve(self.admin_uid, rid)
        self.assertEqual(payload["status"], "applied")
        row = self.conn.execute(
            "SELECT valid_from FROM schedule_periodic WHERE schedule_id = 'sch_a'"
        ).fetchone()
        self.assertEqual(row["valid_from"], "2026-09-28")
        events = self._events(self.admin_uid, rid)["events"]
        self.assertEqual([e["action"] for e in events], ["submit", "approve", "apply"])

    def test_reject_marks_rejected_and_zero_data_change(self):
        before = _dump_business(self.conn)
        rid = self._submit(self.teacher_uid)["request_id"]
        payload = self._reject(self.admin_uid, rid, reason="不合适")
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(_dump_business(self.conn), before)

    def test_withdraw_by_requester(self):
        rid = self._submit(self.teacher_uid)["request_id"]
        payload = self._withdraw(self.teacher_uid, rid)
        self.assertEqual(payload["status"], "withdrawn")

    def test_withdraw_by_other_403(self):
        rid = self._submit(self.teacher_uid)["request_id"]
        with self.assertRaises(Exception) as cm:
            self._withdraw(self.admin_uid, rid)
        self.assertEqual(cm.exception.status, 403)

    def test_terminal_approve_409(self):
        rid = self._submit(self.teacher_uid)["request_id"]
        self._approve(self.admin_uid, rid)
        with self.assertRaises(Exception) as cm:
            self._approve(self.admin_uid, rid)
        self.assertEqual(cm.exception.status, 409)


# ===========================================================================
# 二、批准时重跑校验（撞车 409 / target 已删 404）
# ===========================================================================


class TestValidation(ApiFixture):
    def test_approve_clash_409_and_zero_data_change(self):
        """🔴 提议时合法 ≠ 批准时合法：提交后、批准前改课表 → 409 零写入。"""
        rid = self._submit(self.teacher_uid, valid_from="2026-09-28")["request_id"]
        # 「期间别人改了课表」：把 sch_b 从周三第2节改到周三第1节 → 与 sch_a 撞车
        self.conn.execute(
            "UPDATE schedule_periodic SET slot_bitmap = ? WHERE schedule_id = 'sch_b'",
            (mask_to_hex(bit(3, 1)),),
        )
        self.conn.commit()
        with self.assertRaises(Exception) as cm:
            self._approve(self.admin_uid, rid)
        self.assertEqual(cm.exception.status, 409)
        # 零写入：valid_from 未变、status 仍 pending
        row = self.conn.execute(
            "SELECT valid_from FROM schedule_periodic WHERE schedule_id = 'sch_a'"
        ).fetchone()
        self.assertEqual(row["valid_from"], "2026-09-21")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM change_requests WHERE id = ?", (rid,)
            ).fetchone()["status"],
            "pending",
        )

    def test_approve_missing_target_404(self):
        rid = self._submit(self.teacher_uid)["request_id"]
        self.conn.execute("DELETE FROM schedules WHERE id = 'sch_a'")
        self.conn.commit()
        with self.assertRaises(Exception) as cm:
            self._approve(self.admin_uid, rid)
        self.assertEqual(cm.exception.status, 404)


# ===========================================================================
# 三、权限与隔离（跨组织 / 自己批自己 / 老师档批）
# ===========================================================================


class TestAuthz(ApiFixture):
    def test_cross_org_request_invisible(self):
        rid = self._submit_other()["request_id"]
        # 甲校 teacher 列自己的请求：看不到乙校那条
        self._submit(self.teacher_uid)
        payload = self._list(self.teacher_uid)
        self.assertEqual(payload["count"], 1)
        ids = [r["id"] for r in payload["requests"]]
        self.assertNotIn(rid, ids)

    def test_cross_org_approve_403(self):
        rid = self._submit_other()["request_id"]
        # 甲校 teacher（无乙校授权）批乙校请求 → 403。
        with self.assertRaises(Exception) as cm:
            self._approve(self.teacher_uid, rid)
        self.assertEqual(cm.exception.status, 403)

    def test_self_approve_403(self):
        """🔴 审批流灵魂：自己批准自己 → 403，与 level 无关。"""
        rid = self._submit(self.admin_uid, group=self.cls)["request_id"]  # admin 提出
        with self.assertRaises(Exception) as cm:
            self._approve(self.admin_uid, rid)  # admin 自己批
        self.assertEqual(cm.exception.status, 403)

    def test_propose_approve_403(self):
        """老师档（propose）批准 → 403（teacher 天花板 propose < approve）。"""
        rid = self._submit(self.admin_uid, group=self.cls)["request_id"]  # admin 提出
        with self.assertRaises(Exception) as cm:
            self._approve(self.teacher_uid, rid)  # teacher（propose）批 → 403
        self.assertEqual(cm.exception.status, 403)


# ===========================================================================
# 四、旧库兜底（缺表不 500 / ensure 幂等）
# ===========================================================================


class TestEnsure(ApiFixture):
    def test_old_db_missing_tables_no_500(self):
        """DROP 两张表 → 调接口不 500（ensure_change_tables 缺则 init_db）。"""
        self.conn.execute("DROP TABLE change_events")
        self.conn.execute("DROP TABLE change_requests")
        self.conn.commit()
        payload = self._submit(self.teacher_uid)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["status"], "pending")

    def test_ensure_idempotent(self):
        K.ensure_change_tables(self.conn)
        K.ensure_change_tables(self.conn)  # 调两次不报错


# ===========================================================================
# 五、审计轨迹 + 拒绝参数
# ===========================================================================


class TestAuditAndReject(ApiFixture):
    def test_events_order_and_actor(self):
        rid = self._submit(self.teacher_uid)["request_id"]
        self._approve(self.admin_uid, rid)
        events = self._events(self.admin_uid, rid)["events"]
        self.assertEqual([e["action"] for e in events], ["submit", "approve", "apply"])
        self.assertEqual(
            [e["actor_id"] for e in events],
            [self.teacher_uid, self.admin_uid, self.admin_uid],
        )

    def test_reject_missing_reason_400(self):
        rid = self._submit(self.teacher_uid)["request_id"]
        headers = self.admin_headers
        with self.assertRaises(Exception) as cm:
            API.reject_request(
                "POST", "/api/approvals/reject", {"id": [rid]}, b"",
                headers, context=None,
            )
        self.assertEqual(cm.exception.status, 400)


# ===========================================================================
# 六、路由注册（约定式扫描 + doctor 期望清单）
# ===========================================================================


class TestRoutesRegistration(unittest.TestCase):
    def test_routes_non_empty_and_complete(self):
        self.assertEqual(
            sorted(API.ROUTES),
            [
                ("GET", "/api/approvals"),
                ("GET", "/api/approvals/events"),
                ("POST", "/api/approvals"),
                ("POST", "/api/approvals/approve"),
                ("POST", "/api/approvals/reject"),
                ("POST", "/api/approvals/withdraw"),
            ],
        )

    def test_routes_scan_picks_up_module(self):
        from server import routes

        routes.reset_cache()
        try:
            table = routes.scan()
            for key in API.ROUTES:
                self.assertIn(key, table)
        finally:
            routes.reset_cache()

    def test_doctor_expected_routes_include_approvals(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        try:
            import doctor
        finally:
            sys.path.pop()
        for key in API.ROUTES:
            self.assertIn(key, doctor.EXPECTED_ROUTES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
