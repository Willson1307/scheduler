"""消解闭环（应用前快照 / 可撤销 / 可固化）—— 卡 rvdL9a 的验收测试。

背景（为什么必须有这个文件）
---------------------------------------------------------------------
`m2.apply_plan()` 是**原地 UPDATE**（`schedule_periodic.slot_bitmap` /
`schedule_once.start_at,end_at`），教师点一次「一键消解」就再也回不去了。
现场演示敢不敢点，取决于「能不能撤回」——所以撤销的验收**不许只用冲突数
证明**：冲突数回到 23 不代表每一条都回到了原位。本文件第 2 条按字段逐行比对。

钉死的口径（每条都对应一个「不报错但结果错」的真实风险）
---------------------------------------------------------------------
  1. 快照必须在 `resolve_and_apply_filtered()` **之前**抓。抓晚了拿到的是
     消解后的状态，撤销等于没撤 —— 而且不会报错，看起来还「成功」。
  2. revert 必须**逐字段**还原，不是「冲突数一样」。
  3. committed 再 revert → **409**（不许静默忽略）；reverted 再 revert →
     **200 幂等且不写数据**（第二次不该把别人之后的合法修改打回快照）。
  4. commit 只允许 admin / own 档；教师（天花板 propose）一律 403。
  5. P2：`b9_kernels.exception_effective` 过去把 exceptions 传 None，
     换了作息的那天会「用默认作息算」→ 可能返回 effective=true 但实际不生效。

运行：python -m unittest tests.test_resolve_snapshot -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.occupancy import (  # noqa: E402
    build_occupancy,
    create_term,
    expand_periodic,
    load_slot_config,
    load_terms,
    scan,
)
from scheduler.slots import bit, mask_to_hex, setup_default_slots  # noqa: E402
from server import api_resolve  # noqa: E402
from server import auth  # noqa: E402
from server import b4_kernels as S  # noqa: E402
from server import b9_kernels as K9  # noqa: E402
from server.config import (  # noqa: E402
    ServerConfig,
    ensure_database,
    monday_of,
    open_db,
    set_current_config,
)
from server.http_api import ApiError  # noqa: E402

#: demo 种子：学期 2026-09-07 起，本周三（09-09）有 23 处硬冲突。
#: 🔴 这个 23 是 demo 的既有事实（README / docs/01 / 报名材料都写着），
#:    不是本测试自己凑出来的数——凑数就证明不了「撤销回到了原状」。
TODAY = date(2026, 9, 9)
EXPECTED_CONFLICTS = 23


# ---------------------------------------------------------------------------
# 比对用的「全量字段快照」——与 b4_kernels.capture() 同口径，但独立实现，
# 免得上游一处写错、断言跟着一起错（同源比对等于没比对）。
# ---------------------------------------------------------------------------
def dump_schedules(conn, org_id: str) -> dict:
    out = {}
    for r in conn.execute(
        "SELECT sp.schedule_id AS id, sp.slot_bitmap AS bitmap FROM schedule_periodic sp "
        "JOIN schedules s ON s.id = sp.schedule_id WHERE s.org_id = ?",
        (org_id,),
    ):
        out[r["id"]] = ("periodic", r["bitmap"], None, None)
    for r in conn.execute(
        "SELECT so.schedule_id AS id, so.start_at AS st, so.end_at AS et FROM schedule_once so "
        "JOIN schedules s ON s.id = so.schedule_id WHERE s.org_id = ?",
        (org_id,),
    ):
        out[r["id"]] = ("once", None, r["st"], r["et"])
    return out


def _count(conn, org_id: str) -> int:
    """本窗口的硬冲突数（与 /api/conflicts 的 raw_count 同口径）。"""
    w = monday_of(TODAY)
    return scan(conn, org_id, w, w + timedelta(days=6), cfg=load_slot_config(conn, org_id)).count


class SnapshotFixture(unittest.TestCase):
    """每个用例一份独立的 demo 库（快照/撤销都会改数据，不能共用）。"""

    _seq = 0

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="snap-test-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        api_resolve._rate_buckets.clear()
        type(self)._seq += 1
        self.db_path = os.path.join(self.tmpdir, f"demo_{type(self)._seq}.db")
        cfg = ServerConfig(db_path=self.db_path, today=TODAY)
        ensure_database(cfg)
        set_current_config(cfg)

        conn = open_db()
        try:
            self.org = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
            self.admin = conn.execute(
                "SELECT id, login, role, object_id FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()
            self.teacher = conn.execute(
                "SELECT id, login, role, object_id FROM users WHERE role='teacher' "
                "ORDER BY login LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self._admin_headers = self._headers_for(self.admin)
        self._teacher_headers = self._headers_for(self.teacher)

    def tearDown(self):
        set_current_config(None)

    def _headers_for(self, row) -> dict:
        return {
            "authorization": "Bearer "
            + auth.issue_token(row["id"], row["login"], row["role"], row["object_id"])
        }

    # -- 工具：直连库（断言与手工改动用）-----------------------------------
    def _conn(self):
        return open_db()

    # -- 工具：调 handler ------------------------------------------------
    def _post(self, query=None, headers=None, body=b"{}"):
        return api_resolve.resolve_post(
            "POST", "/api/resolve", query or {}, body, headers or self._admin_headers, None
        )

    def _revert(self, sid, query=None, headers=None):
        q = dict(query or {})
        q["id"] = [sid]
        return api_resolve.resolve_revert(
            "POST", "/api/resolve/revert", q, b"", headers or self._admin_headers, None
        )

    def _commit(self, sid, query=None, headers=None):
        q = dict(query or {})
        q["id"] = [sid]
        return api_resolve.resolve_commit(
            "POST", "/api/resolve/commit", q, b"", headers or self._admin_headers, None
        )


# ===========================================================================
# 一、表结构与旧库兜底（例外⑧「幂等」）
# ===========================================================================
class TestSnapshotSchema(unittest.TestCase):
    def test_init_db_idempotent(self):
        conn = connect(":memory:")
        try:
            init_db(conn)
            init_db(conn)  # 再来一次不能炸（schema.sql 只许幂等 DDL）
            names = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("resolve_snapshots", names)
        finally:
            conn.close()

    def test_status_check_constraint(self):
        """status 只认三态，第四种必须被 CHECK 拒绝（不许出现「靠人记」的第四态）。"""
        conn = connect(":memory:")
        try:
            init_db(conn)
            conn.execute("INSERT INTO organizations(id, name) VALUES ('o1','x')")
            with self.assertRaises(Exception):
                conn.execute(
                    "INSERT INTO resolve_snapshots(id, org_id, created_by, status, payload)"
                    " VALUES ('s1','o1','u1','half_done','{}')"
                )
        finally:
            conn.close()

    def test_ensure_adds_table_to_old_db(self):
        """旧库缺表 → ensure_snapshot_table 补上，不动已有数据。"""
        conn = connect(":memory:")
        try:
            init_db(conn)
            conn.execute("DROP TABLE resolve_snapshots")
            conn.commit()
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='resolve_snapshots'"
                ).fetchone()
            )
            S.ensure_snapshot_table(conn)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='resolve_snapshots'"
                ).fetchone()
            )
        finally:
            conn.close()


# ===========================================================================
# 二、验收 1/2/5：应用 → 撤销 → 逐字段还原
# ===========================================================================
class TestApplyAndRevert(SnapshotFixture):
    def test_apply_reduces_conflicts_and_writes_snapshot(self):
        """验收 1：demo 库 23 → 0，且返回体六个老字段一个不少 + snapshot_id。"""
        conn = self._conn()
        try:
            self.assertEqual(_count(conn, self.org), EXPECTED_CONFLICTS)
        finally:
            conn.close()
        result = self._post()
        self.assertEqual(result["remaining_before"], EXPECTED_CONFLICTS)
        self.assertEqual(result["remaining_after"], 0)
        self.assertGreater(result["applied"], 0)
        for key in ("applied", "remaining_before", "remaining_after", "rounds",
                    "filtered_out_count", "termination"):
            self.assertIn(key, result)  # 🔴 前端与契约测试在吃，一个都不能少
        self.assertTrue(result["snapshot_id"].startswith("snap_"))

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM resolve_snapshots WHERE id=?", (result["snapshot_id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["org_id"], self.org)
        self.assertEqual(row["created_by"], self.admin["id"])
        self.assertEqual(row["remaining_before"], EXPECTED_CONFLICTS)
        self.assertEqual(row["remaining_after"], 0)
        payload = json.loads(row["payload"])
        self.assertTrue(payload["schedules"])
        self.assertIn("captured_at", payload)

    def test_revert_restores_every_field(self):
        """验收 2：撤销后**逐行逐字段**等于应用前（不是「冲突数一样」）。"""
        conn = self._conn()
        try:
            before = dump_schedules(conn, self.org)
        finally:
            conn.close()

        result = self._post()
        self.assertEqual(result["remaining_after"], 0)

        conn = self._conn()
        try:
            changed = dump_schedules(conn, self.org)
            self.assertNotEqual(before, changed, "消解竟然没改动任何日程，本用例失去意义")
            applied_diff = sum(1 for k in before if before[k] != changed[k])
        finally:
            conn.close()

        back = self._revert(result["snapshot_id"])
        self.assertTrue(back["reverted"])
        self.assertFalse(back["already_reverted"])
        self.assertEqual(back["conflicts_after"], EXPECTED_CONFLICTS)
        self.assertGreaterEqual(back["restored"], applied_diff)

        conn = self._conn()
        try:
            after = dump_schedules(conn, self.org)
        finally:
            conn.close()
        # 🔴 逐字段比对：差一条就是「撤销不干净」，冲突数回落也不能掩盖
        self.assertEqual(after, before)
        for sid in before:
            self.assertEqual(after[sid], before[sid], f"日程 {sid} 没回到应用前")

    def test_revert_is_idempotent_and_does_not_rewrite(self):
        """验收 5：连调两次 revert —— 第二次 200，且**不再写数据**。"""
        sid = self._post()["snapshot_id"]
        self._revert(sid)

        conn = self._conn()
        try:
            after_first = dump_schedules(conn, self.org)
            # 模拟「撤销之后又有人合法改了一条」：第二次撤销绝不能把它打回快照
            conn.execute(
                "UPDATE schedule_once SET start_at='2099-01-01T00:00' "
                "WHERE schedule_id=(SELECT schedule_id FROM schedule_once ORDER BY schedule_id LIMIT 1)"
            )
            conn.commit()
            sentinel = dump_schedules(conn, self.org)
        finally:
            conn.close()
        self.assertNotEqual(after_first, sentinel)

        again = self._revert(sid)
        self.assertTrue(again["reverted"])
        self.assertTrue(again["already_reverted"])
        self.assertEqual(again["restored"], 0)

        conn = self._conn()
        try:
            final = dump_schedules(conn, self.org)
        finally:
            conn.close()
        self.assertEqual(final, sentinel, "幂等撤销竟把数据改回去了")

    def test_revert_unknown_id_404(self):
        with self.assertRaises(ApiError) as ctx:
            self._revert("snap_does_not_exist")
        self.assertEqual(ctx.exception.status, 404)

    def test_revert_requires_id_400(self):
        with self.assertRaises(ApiError) as ctx:
            api_resolve.resolve_revert(
                "POST", "/api/resolve/revert", {}, b"", self._admin_headers, None
            )
        self.assertEqual(ctx.exception.status, 400)


# ===========================================================================
# 三、验收 3/4：固化与权限
# ===========================================================================
class TestCommit(SnapshotFixture):
    def test_commit_then_revert_409(self):
        """验收 3：commit 之后 revert → 409（不许静默忽略）。"""
        sid = self._post()["snapshot_id"]
        out = self._commit(sid)
        self.assertTrue(out["committed"])

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT status FROM resolve_snapshots WHERE id=?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "committed")

        conn = self._conn()
        try:
            snapshot_data = dump_schedules(conn, self.org)
        finally:
            conn.close()
        with self.assertRaises(ApiError) as ctx:
            self._revert(sid)
        self.assertEqual(ctx.exception.status, 409)
        conn = self._conn()
        try:
            self.assertEqual(dump_schedules(conn, self.org), snapshot_data)
        finally:
            conn.close()

    def test_commit_requires_applied(self):
        """已 reverted 的快照再 commit → 409（只有 applied 能固化）。"""
        sid = self._post()["snapshot_id"]
        self._revert(sid)
        with self.assertRaises(ApiError) as ctx:
            self._commit(sid)
        self.assertEqual(ctx.exception.status, 409)

    def test_teacher_cannot_commit_403(self):
        """验收 4：教师（天花板 propose）调 commit → 403；但 revert 允许。"""
        result = self._post(headers=self._teacher_headers)
        sid = result["snapshot_id"]
        with self.assertRaises(ApiError) as ctx:
            self._commit(sid, headers=self._teacher_headers)
        self.assertEqual(ctx.exception.status, 403)
        # apply / revert 对教师开放（例外⑧：教师只能用与撤，不能固化）
        self.assertTrue(self._revert(sid, headers=self._teacher_headers)["reverted"])


# ===========================================================================
# 四、快照列表（GET /api/resolve/snapshots）
# ===========================================================================
class TestSnapshotList(SnapshotFixture):
    def test_list_fields_and_order(self):
        sid1 = self._post()["snapshot_id"]
        self._revert(sid1)
        sid2 = self._post()["snapshot_id"]

        out = api_resolve.resolve_snapshots_get(
            "GET", "/api/resolve/snapshots", {}, b"", self._admin_headers, None
        )
        items = out["snapshots"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"], sid2)  # 最近一条在前
        for key in ("id", "created_by_name", "created_at", "status",
                    "applied_count", "remaining_before", "remaining_after"):
            self.assertIn(key, items[0])
        self.assertEqual(items[0]["status"], "applied")
        self.assertEqual(items[1]["status"], "reverted")
        self.assertEqual(items[1]["created_by_name"], self.admin["login"])

    def test_list_limit_20(self):
        # 21 次 POST 会撞「每 token 每分钟 10 次」限频（这是**正确**的防护，
        # 不能为了测试把它拆掉）→ 本用例临时把上限调高，finally 还原。
        old = api_resolve.RATE_LIMIT
        api_resolve.RATE_LIMIT = 1000
        try:
            for _ in range(21):
                self._post()
        finally:
            api_resolve.RATE_LIMIT = old
        out = api_resolve.resolve_snapshots_get(
            "GET", "/api/resolve/snapshots", {}, b"", self._admin_headers, None
        )
        self.assertEqual(len(out["snapshots"]), S.LIST_LIMIT)


# ===========================================================================
# 五、P2：special 作息日上 exception_effective 不许撒谎
# ===========================================================================
def _p2_setup():
    """小组织：默认 10 节/天 + 一套周三只有 6 节的作息方案 + 一条排在周三第 8 节的课。"""
    conn = connect(":memory:")
    init_db(conn)
    org, root = og.create_organization(conn, "测试校")
    cfg = setup_default_slots(conn, org)
    conn.execute(
        "INSERT INTO terms(id, org_id, name, start_date, end_date, week1_parity)"
        " VALUES ('term_p2',?,?,?,?,1)",
        (org, "2026秋", "2026-09-21", "2026-12-31"),
    )
    cls = og.create_object(conn, org, "group", "高一1班")
    og.add_edge(conn, root, cls)
    # 方案：周三只启用 1..6 节
    conn.execute(
        "INSERT INTO period_sets(id, org_id, name) VALUES ('ps_short',?,?)",
        (org, "冬季作息"),
    )
    for idx in range(1, 7):
        conn.execute(
            "INSERT INTO set_slots(id, set_id, day_of_week, idx, start_time, end_time, enabled)"
            " VALUES (?,?,?,?,?,?,1)",
            (f"ps_short_{idx}", "ps_short", 3, idx, f"{8 + (idx - 1):02d}:00",
             f"{8 + (idx - 1):02d}:45"),
        )
    # 一条只在周三第 8 节占用的课（方案里没有第 8 节）
    conn.execute(
        "INSERT INTO schedules(id, org_id, kind, title) VALUES ('sch_p8',?,'periodic','第8节自习')",
        (org,),
    )
    conn.execute(
        "INSERT INTO schedule_periodic(schedule_id, week_parity, slot_bitmap, valid_from, valid_to)"
        " VALUES ('sch_p8',0,?,'2026-09-21','2026-12-31')",
        (mask_to_hex(bit(3, 8)),),
    )
    conn.execute(
        "INSERT INTO schedule_participants(schedule_id, object_id, role)"
        " VALUES ('sch_p8',?,'target')",
        (cls,),
    )
    conn.execute(
        "INSERT INTO schedule_exceptions(id, org_id, scope, target_id, date, mode, set_id)"
        " VALUES ('se_sp',?, 'day', NULL, '2026-09-23', 'special', 'ps_short')",
        (org,),
    )
    conn.commit()
    return conn, org, cls


class TestP2ExceptionEffective(unittest.TestCase):
    WED = "2026-09-23"

    def test_effective_matches_actual_when_period_missing_in_special_scheme(self):
        """special 日该节在方案里不存在 → 取消它并不会改变占用，effective 必须 False。

        修前（第 9 参数传 None）：按默认作息展开 → 第 8 节「存在」→ 返回 True，
        而实际占用早就因为方案没有第 8 节而消失了 —— 字段撒谎。
        """
        conn, org, cls = _p2_setup()
        try:
            cfg = load_slot_config(conn, org)
            row = conn.execute(
                "SELECT sp.week_parity, sp.slot_bitmap, sp.valid_from, sp.valid_to, s.kind "
                "FROM schedule_periodic sp JOIN schedules s ON s.id=sp.schedule_id "
                "WHERE sp.schedule_id='sch_p8'"
            ).fetchone()

            def mask_of(d):
                entries = build_occupancy(conn, org, d, d, cfg=cfg)
                return sum(e.mask for e in entries if e.object_id == cls)

            before = mask_of(date.fromisoformat(self.WED))

            # 实际写入 cancel 例外后，占用是否变化 = 事实
            conn.execute(
                "INSERT INTO schedule_exceptions(id, org_id, scope, target_id, date, mode)"
                " VALUES ('se_cancel', ?, 'schedule', 'sch_p8', ?, 'cancel')",
                (org, self.WED),
            )
            conn.commit()
            after = mask_of(date.fromisoformat(self.WED))
            actually_changed = after != before

            got = K9.exception_effective(conn, org, row, self.WED, cfg, "cancel", None)
            self.assertFalse(got, "special 方案里没有第 8 节，取消它不可能生效")
            self.assertEqual(got, actually_changed, "effective 字段与实际生效不一致")
        finally:
            conn.close()

    def test_effective_true_when_period_exists(self):
        """对照：方案里存在的节次（第 1 节），取消它是真的生效。"""
        conn, org, cls = _p2_setup()
        try:
            cfg = load_slot_config(conn, org)
            conn.execute(
                "UPDATE schedule_periodic SET slot_bitmap=? WHERE schedule_id='sch_p8'",
                (mask_to_hex(bit(3, 1)),),
            )
            conn.commit()
            row = conn.execute(
                "SELECT sp.week_parity, sp.slot_bitmap, sp.valid_from, sp.valid_to, s.kind "
                "FROM schedule_periodic sp JOIN schedules s ON s.id=sp.schedule_id "
                "WHERE sp.schedule_id='sch_p8'"
            ).fetchone()
            self.assertTrue(
                K9.exception_effective(conn, org, row, self.WED, cfg, "cancel", None)
            )
        finally:
            conn.close()

    def test_double_read_consistent_with_expand(self):
        """双读一致：exception_effective 与 expand_periodic 对同一天同结论。"""
        conn, org, _cls = _p2_setup()
        try:
            cfg = load_slot_config(conn, org)
            row = conn.execute(
                "SELECT sp.week_parity, sp.slot_bitmap, sp.valid_from, sp.valid_to, s.kind "
                "FROM schedule_periodic sp JOIN schedules s ON s.id=sp.schedule_id "
                "WHERE sp.schedule_id='sch_p8'"
            ).fetchone()
            from scheduler.occupancy import load_exceptions

            days = expand_periodic(
                row["week_parity"], int(row["slot_bitmap"], 16),
                row["valid_from"], row["valid_to"], cfg,
                load_terms(conn, org), date(2026, 9, 23), date(2026, 9, 23),
                load_exceptions(conn, org),
            )
            got = K9.exception_effective(
                conn, org, row, self.WED, cfg, "cancel", None
            )
            self.assertEqual(got, date(2026, 9, 23) in days)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
