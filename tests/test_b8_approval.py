"""B8 验收测试：审批流（提议→批准→落库），16 项 + rCMOFk 修复 4 缺陷补测。

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

rCMOFk（2026-11-15）新增口径（测试欠账一并补）：
  6. **落库失败回滚 pending**（缺陷1）：mock 掉落库函数制造失败 → 状态必须回
     pending（不是 approved 僵死态）、approver_id/decided_at 零残留、业务数据
     零变化、有 fail 审计事件，且 approve/reject/withdraw 仍可用（不僵死）。
  7. **只批下级**（缺陷2，docs/06 §1）：批准人在该组织的档位必须**严格高于**
     提出人 → 同级 403（两个 admin 互批、admin 互拒都拦）；对照组
     admin(own) 批 teacher(propose) 仍 applied。
  8. **倒置区间**（缺陷3）：after.valid_from > 目标 valid_to → 提交即 400 零写入；
     == valid_to 的单日区间合法（边界不误杀）。
  9. **同目标竞态**（缺陷4）：两条 pending 先后批准，第二条 before 快照对不上
     → 409「数据已被他人改动，请重新提议」，业务数据零变化、状态仍 pending。
  10. **真实 HTTP**：起常驻服务（port=0）真打一条同 admin 互批 403——覆盖
     「http_api 分发 → headers 透传 → require_auth → handler → kernel」整链，
     handler 直调抓不住分发层/装饰器层的挪动。

---------------------------------------------------------------------------
测试基建说明
---------------------------------------------------------------------------

接口层直接调 handler（与 test_b9_exceptions 同源，token 走真实签发链路），
不起真实 HTTP 服务；401/403/400/404/409 每条状态码都有对应用例。
例外：TestRealHttpSameLevel403 按卡面要求起真实服务（见上第 10 条）。
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date
from unittest import mock  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import authz  # noqa: E402
from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.occupancy import create_term  # noqa: E402
from scheduler.slots import bit, mask_to_hex, setup_default_slots  # noqa: E402
from server import api_approvals as API  # noqa: E402
from server import auth, b8_kernels as K  # noqa: E402
from server import config, routes  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.http_api import SchedulerHTTPServer  # noqa: E402

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


def _dump_org_periodic(conn, org_id):
    """指定组织的 schedules + schedule_periodic 快照（跨组织零写入比对）。"""
    sched = [tuple(r) for r in conn.execute(
        "SELECT id, kind, title FROM schedules WHERE org_id = ? ORDER BY id",
        (org_id,),
    ).fetchall()]
    sp = [tuple(r) for r in conn.execute(
        "SELECT p.schedule_id, p.week_parity, p.slot_bitmap,"
        " p.valid_from, p.valid_to"
        " FROM schedule_periodic p JOIN schedules s ON s.id = p.schedule_id"
        " WHERE s.org_id = ? ORDER BY p.schedule_id",
        (org_id,),
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

        # 第二个管理员（rCMOFk 缺陷2「两个 admin 互批」用例）：同为 admin 角色，
        # 在本组织的有效档都是 own（短路），互批必须 403。
        admin_b_obj = og.create_object(cls.conn, cls.org, "user", "管理员乙")
        cls.admin_b_uid = og.create_user(cls.conn, admin_b_obj, "admin_b", "pw", role="admin")
        cls.admin_b_token = auth.issue_token(cls.admin_b_uid, "admin_b", "admin", "obj_ab")
        cls.admin_b_headers = {"authorization": f"Bearer {cls.admin_b_token}"}

        # 本组织第二个教师（rRK0Q5 读取侧可见性用例）：同为 propose 档，
        # 与教师甲（teacher_a）同级 → 互相看不到对方的申请。
        # 登录名不能再用 teacher_b（已被乙校占用，users.login 唯一）。
        teacher_b2_obj = og.create_object(cls.conn, cls.org, "user", "教师乙")
        cls.teacher_b2_uid = og.create_user(
            cls.conn, teacher_b2_obj, "teacher_c", "pw", role="teacher"
        )
        authz.grant_role(cls.conn, cls.org, cls.cls, cls.teacher_b2_uid, "propose")
        cls.teacher_b2_token = auth.issue_token(
            cls.teacher_b2_uid, "teacher_c", "teacher", "obj_tc"
        )
        cls.teacher_b2_headers = {"authorization": f"Bearer {cls.teacher_b2_token}"}
        cls.conn.commit()

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
    def _headers(self, uid):
        if uid == self.admin_uid:
            return self.admin_headers
        if uid == self.admin_b_uid:
            return self.admin_b_headers
        if uid == self.teacher_b2_uid:
            return self.teacher_b2_headers
        return self.teacher_headers

    def _submit(self, uid, target_id="sch_a", valid_from="2026-09-28", group=None, **extra):
        headers = self._headers(uid)
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
        headers = self._headers(uid)
        return API.approve_request(
            "POST", "/api/approvals/approve", {"id": [request_id]}, b"",
            headers, context=None,
        )

    def _reject(self, uid, request_id, reason="不批"):
        headers = self._headers(uid)
        return API.reject_request(
            "POST", "/api/approvals/reject", {"id": [request_id]},
            _body(reason=reason), headers, context=None,
        )

    def _withdraw(self, uid, request_id):
        headers = self._headers(uid)
        return API.withdraw_request(
            "POST", "/api/approvals/withdraw", {"id": [request_id]}, b"",
            headers, context=None,
        )

    def _list(self, uid, **query):
        headers = self._headers(uid)
        return API.list_requests(
            "GET", "/api/approvals", {k: [v] for k, v in query.items()}, b"",
            headers, context=None,
        )

    def _events(self, uid, request_id):
        headers = self._headers(uid)
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

    def test_cross_org_approve_403_zero_write(self):
        """rCMOFk 测试欠账：跨组织批准 → 403 且查库确认对方零写入。

        用甲校 admin（比 teacher 更强的越权者）打乙校请求：admin 对乙校
        组织无任何授权行，`require_approve_in_org` 必须拦 403；
        乙校 schedules / schedule_periodic 全量快照逐字段比对零变化。
        """
        rid = self._submit_other()["request_id"]
        other_before = _dump_org_periodic(self.conn, self.other_org)
        with self.assertRaises(Exception) as cm:
            self._approve(self.admin_uid, rid)
        self.assertEqual(cm.exception.status, 403)
        # 查库：乙校业务表零写入（含 sch_evil_a 的 valid_from 原值未动）
        self.assertEqual(_dump_org_periodic(self.conn, self.other_org), other_before)
        row = self.conn.execute(
            "SELECT p.valid_from FROM schedule_periodic p"
            " WHERE p.schedule_id = 'sch_evil_a'"
        ).fetchone()
        self.assertEqual(row["valid_from"], "2026-09-21")
        # 乙校请求状态零改动（仍 pending）
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM change_requests WHERE id = ?", (rid,)
            ).fetchone()["status"],
            "pending",
        )

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
# 七、rCMOFk 缺陷1：落库失败回滚错态（approved 僵死 → 必须回 pending）
# ===========================================================================


class TestApplyFailureRollback(ApiFixture):
    def test_apply_fail_rolls_back_to_pending_with_fail_event(self):
        """mock 掉落库函数制造失败 → 状态回 pending、业务数据零变化、有 fail 事件。

        原缺陷：except 分支把状态回滚成 approved → approve/reject/withdraw
        都只认 pending，请求就此僵死（全部 409）。
        """
        rid = self._submit(self.teacher_uid)["request_id"]
        before = _dump_business(self.conn)
        with mock.patch(
            "server.b8_kernels.apply_change", side_effect=RuntimeError("落库炸了")
        ):
            with self.assertRaises(RuntimeError):
                self._approve(self.admin_uid, rid)

        # 1) 状态回 pending（不是 approved 僵死态）
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM change_requests WHERE id = ?", (rid,)
            ).fetchone()["status"],
            "pending",
        )
        # 2) 半改零残留：approver_id / decided_at 未落库（随 rollback 撤销）
        row = self.conn.execute(
            "SELECT approver_id, decided_at FROM change_requests WHERE id = ?",
            (rid,),
        ).fetchone()
        self.assertIsNone(row["approver_id"])
        self.assertIsNone(row["decided_at"])
        # 3) 业务数据零变化
        self.assertEqual(_dump_business(self.conn), before)
        # 4) fail 审计事件照记
        events = self._events(self.admin_uid, rid)["events"]
        self.assertEqual([e["action"] for e in events], ["submit", "fail"])
        # 5) 请求未僵死：withdraw 仍可用（原缺陷下这里会 409）
        payload = self._withdraw(self.teacher_uid, rid)
        self.assertEqual(payload["status"], "withdrawn")

    def test_apply_fail_then_retry_approve_still_works(self):
        """失败后不 mock 再批一次 → 正常 applied（回滚真的回到了可批状态）。"""
        rid = self._submit(self.teacher_uid)["request_id"]
        with mock.patch(
            "server.b8_kernels.apply_change", side_effect=RuntimeError("落库炸了")
        ):
            with self.assertRaises(RuntimeError):
                self._approve(self.admin_uid, rid)
        payload = self._approve(self.admin_uid, rid)  # 不 mock，重试
        self.assertEqual(payload["status"], "applied")
        row = self.conn.execute(
            "SELECT valid_from FROM schedule_periodic WHERE schedule_id = 'sch_a'"
        ).fetchone()
        self.assertEqual(row["valid_from"], "2026-09-28")


# ===========================================================================
# 八、rCMOFk 缺陷2：同级可批准（两个 admin 互批 → 403）
# ===========================================================================


class TestSameLevelApproval(ApiFixture):
    def test_admin_cannot_approve_peer_admin(self):
        """🔴 只批下级：admin_a 提出、admin_b（own == own 同级）批 → 403。"""
        rid = self._submit(self.admin_uid, group=self.cls)["request_id"]
        with self.assertRaises(Exception) as cm:
            self._approve(self.admin_b_uid, rid)
        self.assertEqual(cm.exception.status, 403)
        self.assertIn("下级", str(cm.exception))
        # 零状态改动：请求仍 pending
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM change_requests WHERE id = ?", (rid,)
            ).fetchone()["status"],
            "pending",
        )

    def test_admin_cannot_approve_peer_admin_reverse(self):
        """对称方向：admin_b 提出、admin_a 批 → 同样 403。"""
        rid = self._submit(self.admin_b_uid, group=self.cls)["request_id"]
        with self.assertRaises(Exception) as cm:
            self._approve(self.admin_uid, rid)
        self.assertEqual(cm.exception.status, 403)

    def test_admin_cannot_reject_peer_admin(self):
        """拒绝与批准同口径：同级互拒 → 403。"""
        rid = self._submit(self.admin_uid, group=self.cls)["request_id"]
        with self.assertRaises(Exception) as cm:
            self._reject(self.admin_b_uid, rid)
        self.assertEqual(cm.exception.status, 403)

    def test_higher_level_still_can_approve(self):
        """对照：admin(own=3) 批 teacher(propose=1) 的请求仍 applied——
        严格高于判定没有误杀正常的「领导批教师」路径。"""
        rid = self._submit(self.teacher_uid)["request_id"]
        payload = self._approve(self.admin_uid, rid)
        self.assertEqual(payload["status"], "applied")


# ===========================================================================
# 十一b、rRK0Q5 读取侧越权：列表与审计轨迹的可见性
# ===========================================================================


class TestReadVisibility(ApiFixture):
    """rRK0Q5：可见 = 本人 + 档位严格高于提出人（与 require_higher_level 同口径）。

    原缺陷：list_requests 只有 org 过滤、list_events 只要 view 档 → 同组织
    教师B 能看到教师A 的改课申请（requester_id / reason / payload 前后快照
    全泄露），跨用户隐私越权。已知且可接受的副作用：两个 admin 互不可见
    （own == own，与「只能批自己下级」同语义，不是 bug）。
    """

    def test_teacher_b_cannot_see_teacher_a_request(self):
        """🔴 教师B（propose）看不到教师A（propose）的申请：同级不可见。"""
        rid = self._submit(self.teacher_uid)["request_id"]
        payload = self._list(self.teacher_b2_uid)
        self.assertEqual(payload["count"], 0)
        self.assertNotIn(rid, [r["id"] for r in payload["requests"]])

    def test_teacher_a_sees_own_request(self):
        """教师A 看得到自己提交的申请（本人的永远可见）。"""
        rid = self._submit(self.teacher_uid)["request_id"]
        payload = self._list(self.teacher_uid)
        self.assertEqual(payload["count"], 1)
        self.assertIn(rid, [r["id"] for r in payload["requests"]])

    def test_admin_sees_teacher_request(self):
        """admin（own > propose）看得到教师的申请——没误杀审批路径。"""
        rid = self._submit(self.teacher_uid)["request_id"]
        payload = self._list(self.admin_uid)
        self.assertEqual(payload["count"], 1)
        self.assertIn(rid, [r["id"] for r in payload["requests"]])

    def test_teacher_b_read_a_events_403(self):
        """🔴 教师B 读教师A 请求的审计轨迹 → 403（events 与列表同判据）。"""
        rid = self._submit(self.teacher_uid)["request_id"]
        with self.assertRaises(Exception) as cm:
            self._events(self.teacher_b2_uid, rid)
        self.assertEqual(cm.exception.status, 403)


# ===========================================================================
# 九、rCMOFk 缺陷3：倒置区间（after.valid_from > 目标 valid_to → 提交即 400）
# ===========================================================================


class TestInvertedInterval(ApiFixture):
    def test_submit_after_beyond_valid_to_400(self):
        """🔴 倒置区间：2027-06-01 > 2026-12-31 → 400 且零写入零登记。"""
        before = _dump_business(self.conn)
        with self.assertRaises(Exception) as cm:
            self._submit(self.teacher_uid, valid_from="2027-06-01")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("区间倒置", str(cm.exception))
        # 业务数据零变化、也没有登记任何请求
        self.assertEqual(_dump_business(self.conn), before)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM change_requests").fetchone()["c"],
            0,
        )

    def test_submit_equal_to_valid_to_allowed(self):
        """边界：new_valid_from == valid_to（单日区间）合法，不误杀。"""
        payload = self._submit(self.teacher_uid, valid_from="2026-12-31")
        self.assertEqual(payload["status"], "pending")


# ===========================================================================
# 十、rCMOFk 缺陷4：同目标竞态覆盖（第二条 409 请重新提议）
# ===========================================================================


class TestRaceOverwrite(ApiFixture):
    def test_second_approve_hits_race_409_zero_write(self):
        """🔴 同日程两条 pending 先后批准：第一条 applied，第二条 409 零写入。

        原缺陷：第二条静默覆盖第一条的落库结果（before 快照没人核对）。
        修法：批准时比对 before 快照，目标当前 valid_from 对不上 → 409。
        """
        rid1 = self._submit(self.teacher_uid, valid_from="2026-09-28")["request_id"]
        rid2 = self._submit(self.teacher_uid, valid_from="2026-10-05")["request_id"]
        # 第一条正常批准落库（sch_a: 09-21 → 09-28）
        self.assertEqual(self._approve(self.admin_uid, rid1)["status"], "applied")
        before = _dump_business(self.conn)
        # 第二条：before.valid_from=09-21 ≠ 当前 09-28 → 409 请重新提议
        with self.assertRaises(Exception) as cm:
            self._approve(self.admin_uid, rid2)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("数据已被他人改动", str(cm.exception))
        # 零写入：业务数据不变（sch_a 仍是第一条的结果 09-28）、第二条仍 pending
        self.assertEqual(_dump_business(self.conn), before)
        row = self.conn.execute(
            "SELECT valid_from FROM schedule_periodic WHERE schedule_id = 'sch_a'"
        ).fetchone()
        self.assertEqual(row["valid_from"], "2026-09-28")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM change_requests WHERE id = ?", (rid2,)
            ).fetchone()["status"],
            "pending",
        )
        # 第二条审计轨迹里没有任何 approve/apply（没走到落库）
        events = self._events(self.admin_uid, rid2)["events"]
        self.assertEqual([e["action"] for e in events], ["submit"])

    def test_untouched_target_still_approvable(self):
        """对照：没人动过目标时（before 快照吻合）批准不受影响。"""
        rid = self._submit(self.teacher_uid, valid_from="2026-09-28")["request_id"]
        payload = self._approve(self.admin_uid, rid)
        self.assertEqual(payload["status"], "applied")


# ===========================================================================
# 十一、rCMOFk 测试欠账：至少 1 条真实 HTTP 用例（起服务真打 403）
# ===========================================================================


class TestRealHttpSameLevel403(unittest.TestCase):
    """真实 HTTP：两个 admin 互批 → 403（缺陷2 新判定端到端）。

    与 handler 直调的区别（同 test_server_authz_e2e 的理由）：真实 HTTP
    额外覆盖「http_api 分发 → headers 透传 → require_auth 装饰器 →
    handler → b8_kernels」整条链路。若将来有人把同级判定挪层、或分发层
    改动 headers 归一化，只有这条抓得住。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b8_http_")
        cls.db_path = os.path.join(cls.tmp, "b8_http.db")
        conn, cls.org, cls.cls, _teacher, cls.admin_uid, _slots_cfg = (
            _build_api_db(cls.db_path)
        )
        admin_b_obj = og.create_object(conn, cls.org, "user", "管理员乙")
        cls.admin_b_uid = og.create_user(conn, admin_b_obj, "admin_b", "pw", role="admin")
        conn.commit()
        conn.close()

        cls._prev_cfg = config._current
        config.set_current_config(
            ServerConfig(
                host="127.0.0.1", port=0, db_path=cls.db_path,
                today=date(2026, 9, 22), seed_if_missing=False,
            )
        )
        routes.reset_cache()
        from server.api_login import reset_locks
        reset_locks()

        cls.server = SchedulerHTTPServer(
            ("127.0.0.1", 0), config.get_current_config()
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin_token = cls._login("admin", "pw")
        cls.admin_b_token = cls._login("admin_b", "pw")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        config.set_current_config(cls._prev_cfg)
        routes.reset_cache()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _request(cls, method, path, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
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
        payload = json.dumps({"login": login, "password": password}).encode("utf-8")
        status, data = cls._request("POST", "/api/login", body=payload)
        assert status == 200, f"登录失败 {login}: {status} {data}"
        return json.loads(data)["token"]

    def test_same_level_approve_403_over_http(self):
        # 1) admin_a 提交一条改 sch_a 起始日的请求（真实 HTTP POST）
        submit_body = json.dumps({
            "scope": "permanent", "op": "update", "target_type": "schedule",
            "target_id": "sch_a", "payload": {"after": {"valid_from": "2026-09-28"}},
            "reason": "HTTP 同级互批用例",
        }).encode("utf-8")
        st, data = self._request(
            "POST", "/api/approvals", token=self.admin_token, body=submit_body
        )
        self.assertEqual(st, 200, data)
        rid = json.loads(data)["request_id"]

        # 2) admin_b（同级 own）批准 → 403，错误体为扁平 error 原文
        st, data = self._request(
            "POST", f"/api/approvals/approve?id={rid}", token=self.admin_b_token
        )
        self.assertEqual(st, 403, data)
        body = json.loads(data)
        self.assertIn("下级", body["error"])

        # 3) 请求零状态改动：列表里仍是 pending（真实 HTTP GET）
        st, data = self._request(
            "GET", f"/api/approvals?status=pending", token=self.admin_token
        )
        self.assertEqual(st, 200, data)
        items = json.loads(data)["requests"]
        self.assertEqual(
            [r["id"] for r in items], [rid],
            "同级 403 后请求必须仍为 pending 且未被改动",
        )


# ===========================================================================
# 十二、路由注册（约定式扫描 + doctor 期望清单）
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
