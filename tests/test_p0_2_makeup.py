"""P0-2 回归测试：调休 makeup（schedule_exceptions，scope='day'，mode='as_of'）。

复现背景（修前实测，occupancy.py 旧 126-131 行）：
    calendar_exceptions = {'2026-09-26': 'makeup'}   ← 已写入"周六调休按周一课表"
    该周周期课表展开结果 = {2026-09-21: 1}            ← 只有周一
    周六(调休日)是否有课: ❌ 无 —— dow=6 → day_mask(6)=0 → 课表凭空消失，不报错。

修后语义（队长 2026-10-12 拍板，三条定死）：
    1. 🔴 mode='as_of' 且 as_of_dow 为 NULL → 抛 ValueError，不许静默 continue；
    2. as_of_dow 存 1..7（与 period_slots.day_of_week 同口径）；
    3. 仅 mode='as_of' 消费 as_of_dow，其他 mode（cancel/move/special）本卡忽略。
    旧表 calendar_exceptions 保留不动、只服务 kind='holiday'；调休一律走新表。

运行：python -m unittest tests.test_p0_2_makeup -v
"""

import os
import sqlite3
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.occupancy import (  # noqa: E402
    build_occupancy,
    create_term,
    expand_periodic,
    load_exceptions,
    load_terms,
    scan,
)
from scheduler.slots import bit, mask_to_hex, setup_default_slots  # noqa: E402


def fresh():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def _setup(conn):
    """建组织 + 默认节次（周六周日不排课）+ 学期（2026-09-21 周一起）。"""
    org, _root = og.create_organization(conn, "测试校")
    cfg = setup_default_slots(conn, org)  # 周一~周五 10 节，周六周日 enabled=0
    create_term(conn, org, "2026秋", "2026-09-21", "2026-12-31")
    return org, cfg


def _ins_sched_exc(conn, eid, org, d, mode, as_of_dow=None, scope="day",
                   target_id=None, new_mask=None, set_id=None):
    conn.execute(
        "INSERT INTO schedule_exceptions"
        "(id, org_id, scope, target_id, date, mode, as_of_dow, new_mask, set_id, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (eid, org, scope, target_id, d, mode, as_of_dow, new_mask, set_id, "P0-2测试"),
    )
    conn.commit()


def _ins_cal_exc(conn, eid, org, d, kind):
    conn.execute(
        "INSERT INTO calendar_exceptions(id, org_id, date, kind, note)"
        " VALUES (?,?,?,?,?)",
        (eid, org, d, kind, "P0-2测试"),
    )
    conn.commit()


WEEK = (date(2026, 9, 21), date(2026, 9, 27))  # 周一 ~ 周日


class TestMakeupExpansion(unittest.TestCase):
    """expand_periodic 的 as_of 展开分支。"""

    def test_repro_makeup_saturday_now_has_class(self):
        """卡面复现场景：周六调休按周一课表 → 周六掩码非 0。

        修前：{2026-09-21: 1}，周六凭空消失。
        修后：{2026-09-21: 1, 2026-09-26: 1}。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            # 旧表 makeup 行（修前后都不生效，保留以证不冲突）
            _ins_cal_exc(conn, "cx_legacy", org, "2026-09-26", "makeup")
            # 新表 as_of 行：周六按周一课表
            _ins_sched_exc(conn, "se_mk", org, "2026-09-26", "as_of", as_of_dow=1)
            out = expand_periodic(
                0, bit(1, 1), "2026-09-21", "2026-12-31",
                cfg, load_terms(conn, org), *WEEK,
                load_exceptions(conn, org),
            )
            self.assertEqual(out.get(date(2026, 9, 21)), 1)   # 周一照常
            self.assertEqual(out.get(date(2026, 9, 26)), 1)   # 🔴 修前这里 KeyError
            self.assertNotIn(date(2026, 9, 27), out)          # 周日不受影响
        finally:
            conn.close()

    def test_as_of_dow_null_raises(self):
        """🔴 队长定死的语义 1：as_of_dow NULL 必须抛 ValueError，不许静默。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            _ins_sched_exc(conn, "se_bad", org, "2026-10-03", "as_of", as_of_dow=None)
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            self.assertIn("as_of_dow", str(cm.exception))
            self.assertIn("2026-10-03", str(cm.exception))
        finally:
            conn.close()

    def test_as_of_dow_out_of_range_rejected(self):
        """as_of_dow 越界（8）：DDL CHECK 在写入层直接拦（IntegrityError）。

        注意分工：DDL 允许 as_of_dow=NULL（历史数据兼容），所以 NULL 必须由
        应用层（load_exceptions）拦——见 test_as_of_dow_null_raises；
        越界则 DDL 第一道就拒之门外，load_exceptions 的 1..7 检查是纵深防御。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                _ins_sched_exc(conn, "se_oob", org, "2026-10-03", "as_of", as_of_dow=8)
        finally:
            conn.close()

    def test_conflicting_as_of_same_date_raises(self):
        """同日两条 as_of 且 dow 互相矛盾 → 抛错（矛盾数据必须先修，不许任选一条）。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            _ins_sched_exc(conn, "se_a", org, "2026-09-26", "as_of", as_of_dow=1)
            _ins_sched_exc(conn, "se_b", org, "2026-09-26", "as_of", as_of_dow=2)
            with self.assertRaises(ValueError):
                load_exceptions(conn, org)
        finally:
            conn.close()

    def test_holiday_path_still_works(self):
        """旧表 kind='holiday' 路径不能被改坏：周四放假 → 周四消失，其余照常。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            _ins_cal_exc(conn, "cx_hol", org, "2026-09-24", "holiday")
            bitmap = bit(1, 1) | bit(2, 1) | bit(3, 1) | bit(4, 1) | bit(5, 1)
            out = expand_periodic(
                0, bitmap, "2026-09-21", "2026-12-31",
                cfg, load_terms(conn, org), *WEEK,
                load_exceptions(conn, org),
            )
            for d, dow in [(21, 1), (22, 2), (23, 3), (25, 5)]:
                self.assertEqual(out.get(date(2026, 9, d)), bit(dow, 1))
            self.assertNotIn(date(2026, 9, 24), out)  # 周四放假
        finally:
            conn.close()

    def test_legacy_dict_still_accepted(self):
        """expand_periodic 旧签名（{ISO: kind} 字典）保持兼容：holiday 生效、其他值惰性。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            bitmap = bit(3, 1) | bit(4, 1)  # 周三、周四第 1 节
            # 周四 holiday → 跳过；周三 'makeup'（旧表口径）→ 惰性，照常展开
            out = expand_periodic(
                0, bitmap, "2026-09-21", "2026-12-31",
                cfg, load_terms(conn, org), *WEEK,
                {"2026-09-24": "holiday", "2026-09-23": "makeup"},
            )
            self.assertNotIn(date(2026, 9, 24), out)
            self.assertEqual(out.get(date(2026, 9, 23)), bit(3, 1))
        finally:
            conn.close()

    def test_other_modes_ignored(self):
        """cancel/move/special 与 scope='schedule' 的行：本卡忽略，不抛错不生效。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            # cancel/move/special 均 as_of_dow=NULL —— 不得触发"NULL 抛错"（那仅限 mode='as_of'）
            _ins_sched_exc(conn, "se_c", org, "2026-09-23", "cancel")
            _ins_sched_exc(conn, "se_m", org, "2026-09-24", "move", new_mask="1")
            _ins_sched_exc(conn, "se_s", org, "2026-09-25", "special", set_id="set_x")
            _ins_sched_exc(conn, "se_sc", org, "2026-09-26", "cancel",
                           scope="schedule", target_id="sch_dummy")
            exc = load_exceptions(conn, org)
            self.assertEqual(exc.as_of, {})
            out = expand_periodic(
                0, bit(1, 1), "2026-09-21", "2026-12-31",
                cfg, load_terms(conn, org), *WEEK, exc,
            )
            self.assertEqual(out.get(date(2026, 9, 21)), 1)
            self.assertNotIn(date(2026, 9, 26), out)  # 未配 as_of → 周六照旧无课
        finally:
            conn.close()

    def test_org_isolation(self):
        """A 组织的 as_of 例外不得影响 B 组织（组织级数据隔离红线）。"""
        conn = fresh()
        try:
            org_a, cfg_a = _setup(conn)
            org_b, _root_b = og.create_organization(conn, "测试校B")
            _ins_sched_exc(conn, "se_a", org_a, "2026-09-26", "as_of", as_of_dow=1)
            self.assertEqual(load_exceptions(conn, org_b).as_of, {})
            self.assertEqual(len(load_exceptions(conn, org_a).as_of), 1)
        finally:
            conn.close()


class TestMakeupE2E(unittest.TestCase):
    """经 build_occupancy / scan 的端到端：占用条目 + 假阴性冲突复现。"""

    def _add_periodic(self, conn, org, sid, title, bitmap, vf, vt, obj_id):
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
        conn.execute(
            "INSERT INTO schedule_participants(schedule_id, object_id, role)"
            " VALUES (?,?,?)",
            (sid, obj_id, "target"),
        )
        conn.commit()

    def test_makeup_creates_occupancy_and_conflict(self):
        """两门课都排周一第 1 节：调休前只周一撞；周六调休按周一时周六也撞。

        修前：scan 只报 9-21 一处 —— 周六的相撞是假阴性（系统说没冲突、实际有）。
        修后：报 9-21 + 9-26 两处。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生甲")
            self._add_periodic(conn, org, "sch_yw", "语文", bit(1, 1),
                               "2026-09-21", "2026-12-31", stu)
            self._add_periodic(conn, org, "sch_sx", "数学", bit(1, 1),
                               "2026-09-21", "2026-12-31", stu)
            _ins_sched_exc(conn, "se_mk", org, "2026-09-26", "as_of", as_of_dow=1)

            entries = build_occupancy(conn, org, *WEEK)
            sat_entries = [e for e in entries if e.date == date(2026, 9, 26)]
            self.assertTrue(sat_entries, "修后周六必须有占用条目（修前为空）")
            self.assertEqual(sat_entries[0].mask, 1)

            report = scan(conn, org, *WEEK)
            days = sorted({c.date for c in report.conflicts})
            self.assertEqual(days, [date(2026, 9, 21), date(2026, 9, 26)])
        finally:
            conn.close()


class TestLegacyDbBootstrap(unittest.TestCase):
    """第 7 处静默失效的坑：旧库没有新表 → 读路径必须兜底建表，不许 500。"""

    def test_load_exceptions_recreates_missing_table(self):
        """模拟旧库：DROP 掉 schedule_exceptions 后，load_exceptions 幂等补表。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            conn.execute("DROP TABLE schedule_exceptions")
            conn.commit()
            # 修前（无 ensure）：这里直接 OperationalError: no such table
            exc = load_exceptions(conn, org)
            self.assertEqual(exc.as_of, {})
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='schedule_exceptions'"
            ).fetchone()
            self.assertIsNotNone(row, "ensure 兜底后表必须真实存在")
        finally:
            conn.close()

    def test_scan_on_missing_table(self):
        """缺表旧库上 scan 全链路可用（doctor/契约测试都测不出的场景）。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            conn.execute("DROP TABLE schedule_exceptions")
            conn.commit()
            report = scan(conn, org, *WEEK)
            self.assertEqual(report.count, 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
