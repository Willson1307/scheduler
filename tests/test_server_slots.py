"""B 批·局部补排后端：GET /api/slots 的验收测试。

运行：python -m unittest tests.test_server_slots -v

覆盖（卡面 B 批·局部补排后端 的逐条验收）：
  1. 200 有候选（admin 与教师两条路径，教师=非管理员走一遍）；
  2. 未登录 401（@require_auth，嵌套格式）；
  3. 跨组织 403（教师查别组织目标，⛔ 不是 200 空结果）；
  4. 400：缺参 / 日期非法 / from>to / 跨度>31 天 / duration<1 或 >16 /
     limit>50 / target 不存在（含「对象不存在」）；
  5. 全满 200 + count=0（空 candidates 是正常业务态，不是错误）；
  6. 🔴 掩码不外发：candidate 无 mask 字段、保留 mask_hex；
  7. 🔴 special（冬季作息）日不出现第 7/8 节（effective_slot_config 语义，
     破坏法落点——把 effective_slot_config 换成「忽略 special」本用例立刻变红）；
  8. 只读：调用前后关键表逐行一致；
  9. 口径一致：与直接调 m2.find_slots(cfg=None) 逐项一致。

只用 Python 标准库。
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect  # noqa: E402
from scheduler.slots import bit, mask_to_hex  # noqa: E402
from scheduler.solver import m2  # noqa: E402
from server import api_slots  # noqa: E402
from server import auth, config  # noqa: E402
from server.config import ServerConfig, ensure_database  # noqa: E402
from server.http_api import ApiError  # noqa: E402

TODAY = date(2026, 9, 8)  # 周一；seed_demo 的 term_start 锚定 2026-09-07
MON = "2026-09-07"
SUN = "2026-09-13"

_TABLES = (
    "schedules",
    "schedule_periodic",
    "schedule_once",
    "schedule_participants",
    "schedule_resources",
    "schedule_exceptions",
    "calendar_exceptions",
    "period_slots",
    "period_sets",
    "set_slots",
    "objects",
    "terms",
)


def _issue_token(conn, login: str) -> str:
    row = conn.execute(
        "SELECT id, login, role, object_id FROM users WHERE login = ?", (login,)
    ).fetchone()
    assert row is not None, f"账号 {login} 不存在"
    return "Bearer " + auth.issue_token(row["id"], row["login"], row["role"], row["object_id"])


class TestSlotsAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="slots-test-")
        cls.db_path = os.path.join(cls.tmpdir, "demo.db")
        cfg = ServerConfig(db_path=cls.db_path, today=TODAY)
        ensure_database(cfg)
        cls._prev_cfg = config._current
        config.set_current_config(cfg)

        conn = connect(cls.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM objects WHERE kind='group' AND name='高一(1)班'"
            ).fetchone()
            cls.class_id = row["id"]
            cls.org_id = conn.execute("SELECT org_id FROM objects WHERE id=?", (cls.class_id,)).fetchone()["org_id"]
            cls.admin_token = _issue_token(conn, "admin")
            cls.teacher_token = _issue_token(conn, "t01")
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        if cls._prev_cfg is not None:
            config.set_current_config(cls._prev_cfg)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def _call(self, query, headers=None):
        return api_slots.list_slots("GET", "/api/slots", query, b"", headers or {}, None)

    def _q(self, target, d0=MON, d1=SUN, **extra):
        q = {"target": [target], "from": [d0], "to": [d1]}
        for k, v in extra.items():
            q[k] = [str(v)]
        return q

    def _conn(self):
        return connect(self.db_path)

    def _add_periodic(self, conn, sid, bitmap, obj_id, vf=MON, vt=SUN, title="满班日程"):
        conn.execute(
            "INSERT INTO schedules(id, org_id, kind, title) VALUES (?,?,?,?)",
            (sid, self.org_id, "periodic", title),
        )
        conn.execute(
            "INSERT INTO schedule_periodic(schedule_id, week_parity, slot_bitmap, valid_from, valid_to)"
            " VALUES (?,?,?,?,?)",
            (sid, 0, mask_to_hex(bitmap), vf, vt),
        )
        conn.execute(
            "INSERT INTO schedule_participants(schedule_id, object_id, role) VALUES (?,?,?)",
            (sid, obj_id, "target"),
        )
        conn.commit()

    # -- 1. 200 有候选 ------------------------------------------------------
    def test_admin_returns_candidates(self):
        result = self._call(self._q(self.class_id), {"authorization": self.admin_token})
        self.assertTrue(result["ok"])
        self.assertEqual(result["target"], self.class_id)
        self.assertGreater(result["count"], 0)
        self.assertEqual(result["count"], len(result["candidates"]))
        c0 = result["candidates"][0]
        for key in ("date", "day_of_week", "periods", "mask_hex", "cost", "reasons"):
            self.assertIn(key, c0)

    def test_teacher_returns_candidates(self):
        """非管理员账号走一遍（AGENTS §4：admin 全组织短路会掩盖教师全空）。"""
        result = self._call(self._q(self.class_id), {"authorization": self.teacher_token})
        self.assertTrue(result["ok"])
        self.assertGreater(result["count"], 0)

    # -- 2. 未登录 401 ------------------------------------------------------
    def test_unauthenticated_401(self):
        status, body = self._call(self._q(self.class_id), headers={})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    # -- 3. 跨组织 403 ------------------------------------------------------
    def test_cross_org_403(self):
        conn = self._conn()
        try:
            org_b, _root = og.create_organization(conn, "测试校B")
            group_b = og.create_object(conn, org_b, "group", "B班")
            conn.commit()  # create_object 不自动 commit
        finally:
            conn.close()
        with self.assertRaises(ApiError) as ctx:
            self._call(self._q(group_b), {"authorization": self.teacher_token})
        self.assertEqual(ctx.exception.status, 403)

    def test_admin_cross_org_403_unless_granted(self):
        """admin 对自己组织短路，但别组织未显式授权 → 同样 403。"""
        conn = self._conn()
        try:
            org_b, _root = og.create_organization(conn, "测试校C")
            group_b = og.create_object(conn, org_b, "group", "C班")
            conn.commit()  # create_object 不自动 commit
        finally:
            conn.close()
        with self.assertRaises(ApiError) as ctx:
            self._call(self._q(group_b), {"authorization": self.admin_token})
        self.assertEqual(ctx.exception.status, 403)

    # -- 4. 400 各形态 ------------------------------------------------------
    def test_missing_params_400(self):
        with self.assertRaises(ApiError) as ctx:
            self._call({}, {"authorization": self.admin_token})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError) as ctx:
            self._call({"target": [self.class_id]}, {"authorization": self.admin_token})
        self.assertEqual(ctx.exception.status, 400)

    def test_bad_date_400(self):
        with self.assertRaises(ApiError) as ctx:
            self._call(self._q(self.class_id, d0="not-a-date"), {"authorization": self.admin_token})
        self.assertEqual(ctx.exception.status, 400)

    def test_from_after_to_400(self):
        with self.assertRaises(ApiError) as ctx:
            self._call(self._q(self.class_id, d0=SUN, d1=MON), {"authorization": self.admin_token})
        self.assertEqual(ctx.exception.status, 400)

    def test_span_too_long_400(self):
        with self.assertRaises(ApiError) as ctx:
            self._call(self._q(self.class_id, d0=MON, d1="2026-10-31"), {"authorization": self.admin_token})
        self.assertEqual(ctx.exception.status, 400)

    def test_duration_out_of_range_400(self):
        for bad in (0, 17):
            with self.assertRaises(ApiError) as ctx:
                self._call(self._q(self.class_id, duration=bad), {"authorization": self.admin_token})
            self.assertEqual(ctx.exception.status, 400, f"duration={bad} 应 400")

    def test_limit_out_of_range_400(self):
        for bad in (0, 51):
            with self.assertRaises(ApiError) as ctx:
                self._call(self._q(self.class_id, limit=bad), {"authorization": self.admin_token})
            self.assertEqual(ctx.exception.status, 400, f"limit={bad} 应 400")

    def test_target_not_found_400(self):
        with self.assertRaises(ApiError) as ctx:
            self._call(self._q("obj_nope"), {"authorization": self.admin_token})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("对象不存在", ctx.exception.message)

    # -- 5. 全满 200 + count=0 ---------------------------------------------
    def test_full_returns_empty_200(self):
        # 只填周一~五、也只查周一~五：demo 库周末槽位 enabled=1（ensure_weekend_slots），
        # 若查满整周则周六周日仍空着，会误出候选。
        conn = self._conn()
        try:
            full_cls = og.create_object(conn, self.org_id, "group", "满班")
            everything = sum(bit(d, i) for d in range(1, 6) for i in range(1, 11))
            self._add_periodic(conn, "sch_full", everything, full_cls)
        finally:
            conn.close()
        result = self._call(
            self._q(full_cls, d0=MON, d1="2026-09-11"), {"authorization": self.admin_token}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["candidates"], [])

    # -- 6. 掩码不外发 ------------------------------------------------------
    def test_no_mask_field(self):
        result = self._call(self._q(self.class_id), {"authorization": self.admin_token})
        for c in result["candidates"]:
            self.assertNotIn("mask", c, "周级 mask 不得外发（前端不做位运算）")
            self.assertIn("mask_hex", c)
            self.assertIsInstance(c["periods"], list)

    # -- 7. special（冬季作息）日不出现第 7/8 节 ---------------------------
    def test_special_day_no_missing_periods(self):
        conn = self._conn()
        try:
            # 一个全新教师（无任何日程），绑 2026-09-16（周三）到「只有 1~6 节」的方案
            teacher = og.create_object(conn, self.org_id, "user", "冬老师")
            conn.execute(
                "INSERT INTO period_sets(id, org_id, name) VALUES (?,?,?)",
                ("set_winter", self.org_id, "冬季作息"),
            )
            for i in range(1, 7):
                conn.execute(
                    "INSERT INTO set_slots(id, set_id, day_of_week, idx, start_time, end_time, enabled)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (f"set_winter_s{i}", "set_winter", 3, i, "08:00", "08:45", 1),
                )
            conn.execute(
                "INSERT INTO schedule_exceptions(id, org_id, scope, target_id, date, mode, set_id)"
                " VALUES (?,?,?,NULL,?,?,?)",
                ("se_winter", self.org_id, "day", "2026-09-16", "special", "set_winter"),
            )
            conn.commit()
        finally:
            conn.close()

        result = self._call(
            self._q(teacher, d0="2026-09-16", d1="2026-09-16"),
            {"authorization": self.admin_token},
        )
        self.assertGreater(result["count"], 0, "1~6 节全空，必须返回候选")
        for c in result["candidates"]:
            self.assertLessEqual(max(c["periods"]), 6, c)
        # 单节候选应正好给满 1~6 节（防止「只返回 1 个也能绿」）
        self.assertEqual(sorted(c["periods"][0] for c in result["candidates"]), [1, 2, 3, 4, 5, 6])

    # -- 8. 只读 ------------------------------------------------------------
    def test_readonly_no_writes(self):
        conn = self._conn()
        try:
            before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()] for t in _TABLES}
        finally:
            conn.close()
        self._call(self._q(self.class_id, duration=2, limit=20), {"authorization": self.admin_token})
        conn = self._conn()
        try:
            after = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()] for t in _TABLES}
        finally:
            conn.close()
        self.assertEqual(before, after)

    # -- 9. 口径一致：与直接调 find_slots(cfg=None) 逐项一致 -----------------
    def test_consistent_with_direct_find_slots(self):
        api = self._call(self._q(self.class_id, duration=2, limit=8), {"authorization": self.admin_token})
        conn = self._conn()
        try:
            direct = m2.find_slots(
                conn, self.org_id, target=self.class_id,
                date_range=(date(2026, 9, 7), date(2026, 9, 13)),
                duration=2, limit=8, cfg=None,
            )
        finally:
            conn.close()
        stripped = [{k: v for k, v in c.items() if k != "mask"} for c in direct]
        self.assertEqual(api["candidates"], stripped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
