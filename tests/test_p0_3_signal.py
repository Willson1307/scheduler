"""P0-3 回归测试：静默跳过统一信号（ExpansionIssue）。

复现背景（修前实测，.workbuddy/tmp/p03_probe.py 留档）：
    1. 孤儿行：删掉 schedule_periodic 扩展行后，冲突 1→0 静默发生，
       视图 2 行→1 行，scan 不报任何东西（假阴性）。
    2. 午休/周末活动：mask_for_range 返回 mask=0, aligned=False，
       expand_once 把 aligned 命名成 _aligned 丢弃，活动"不存在"。
    3. 无 term：扫描窗口落在 terms 区间外，课表展开为空，无信号。

修后语义（P0-3 卡面）：
    signal 必须可见——计数（ExpansionIssue）并返回给调用方，
    scan 挂到 ConflictReport.issues，/api/conflicts 以 warnings 透出。
    不抛异常（试用库可能有真实脏数据，一条脏数据不能打死整个扫描）；
    load_exceptions 的 ValueError 是 P0-2 定的另一条路，两不冲突。

设计内、不报警的行为（必须守住，防止刷屏）：
    单双周不匹配、周末 day_mask=0、holiday 放假、跨半节保守取整。

运行：python -m unittest tests.test_p0_3_signal -v
"""

import os
import sys
import unittest
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.occupancy import (  # noqa: E402
    ISSUE_ORPHAN_SCHEDULE,
    ISSUE_ONCE_NO_SLOT,
    ISSUE_PERIODIC_NO_TERM,
    ExpansionIssue,
    build_occupancy,
    create_term,
    expand_once,
    expand_periodic,
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
    cfg = setup_default_slots(conn, org)
    create_term(conn, org, "2026秋", "2026-09-21", "2026-12-31")
    return org, cfg


def _add_periodic(conn, org, sid, title, bitmap, vf, vt, obj_id):
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


def _add_once(conn, org, sid, title, start_at, end_at, obj_id):
    conn.execute(
        "INSERT INTO schedules(id, org_id, kind, title) VALUES (?,?,?,?)",
        (sid, org, "once", title),
    )
    conn.execute(
        "INSERT INTO schedule_once(schedule_id, start_at, end_at, group_id)"
        " VALUES (?,?,?,NULL)",
        (sid, start_at, end_at),
    )
    conn.execute(
        "INSERT INTO schedule_participants(schedule_id, object_id, role)"
        " VALUES (?,?,?)",
        (sid, obj_id, "target"),
    )
    conn.commit()


WEEK = (date(2026, 9, 21), date(2026, 9, 27))  # 周一 ~ 周日


class TestOrphanSchedule(unittest.TestCase):
    """静默点 4：schedules 有主表行、扩展表缺行 → INNER JOIN 视图丢弃。"""

    def test_orphan_reduces_conflicts_and_signals(self):
        """卡面验收 Q2 场景：删扩展行 → 冲突数变化 + 必须有 orphan_schedule 信号。

        修前实测（p03_probe.py）：raw_count 1→0，零信号——假阴性坐实。
        修后：raw_count 仍 1→0（孤儿行没有时间数据，物理上无法展开），
        但 ConflictReport.issues 必须点名这条孤儿。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生甲")
            _add_periodic(conn, org, "sch_yw", "语文", bit(1, 1),
                          "2026-09-21", "2026-12-31", stu)
            _add_periodic(conn, org, "sch_sx", "数学", bit(1, 1),
                          "2026-09-21", "2026-12-31", stu)
            self.assertEqual(scan(conn, org, *WEEK).count, 1)
            self.assertEqual(scan(conn, org, *WEEK).issues, [])

            conn.execute("DELETE FROM schedule_periodic WHERE schedule_id = 'sch_sx'")
            conn.commit()
            report = scan(conn, org, *WEEK)
            # 冲突数下降是孤儿行的物理事实（没有时间数据就展不开）
            self.assertEqual(report.count, 0)
            # 🔴 修前这里 issues 为空；修后必须点名孤儿
            self.assertEqual(len(report.issues), 1)
            iss = report.issues[0]
            self.assertEqual(iss.code, ISSUE_ORPHAN_SCHEDULE)
            self.assertEqual(iss.schedule_id, "sch_sx")
            self.assertEqual(iss.schedule_title, "数学")
            self.assertIn("v_schedule_full", iss.detail)
        finally:
            conn.close()

    def test_orphan_kind_once_also_caught(self):
        """once 类孤儿同样要抓（两类扩展表都缺行）。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生乙")
            _add_once(conn, org, "sch_hd", "周末讲座",
                      "2026-09-26T10:00", "2026-09-26T11:00", stu)
            conn.execute("DELETE FROM schedule_once WHERE schedule_id = 'sch_hd'")
            conn.commit()
            issues: list[ExpansionIssue] = []
            build_occupancy(conn, org, *WEEK, issues=issues)
            self.assertEqual(
                [i.code for i in issues], [ISSUE_ORPHAN_SCHEDULE]
            )
            self.assertEqual(issues[0].schedule_kind, "once")
        finally:
            conn.close()

    def test_org_isolation(self):
        """A 组织的孤儿不得泄漏进 B 组织的 issues（组织隔离红线）。"""
        conn = fresh()
        try:
            org_a, cfg = _setup(conn)
            org_b, _ = og.create_organization(conn, "测试校B")
            stu = og.create_object(conn, org_a, "user", "学生甲")
            _add_periodic(conn, org_a, "sch_yw", "语文", bit(1, 1),
                          "2026-09-21", "2026-12-31", stu)
            conn.execute("DELETE FROM schedule_periodic WHERE schedule_id = 'sch_yw'")
            conn.commit()
            self.assertEqual(scan(conn, org_b, *WEEK).issues, [])
            self.assertEqual(len(scan(conn, org_a, *WEEK).issues), 1)
        finally:
            conn.close()


class TestOnceNoSlot(unittest.TestCase):
    """静默点 1+5：活动不落任何启用槽位 → mask=0 + aligned 被丢弃。"""

    def test_saturday_activity_invisible_but_signalled(self):
        """周六活动（无启用槽位）→ 展开为空 + once_no_slot 信号。

        修前实测：expand_once 返回 {}，零信号——活动"不存在"。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            issues: list[ExpansionIssue] = []
            out = expand_once("2026-09-26T10:00", "2026-09-26T11:00",
                              cfg, issues=issues)
            self.assertEqual(out, {})
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].code, ISSUE_ONCE_NO_SLOT)
            self.assertEqual(issues[0].dates, ["2026-09-26"])
            self.assertEqual(issues[0].count, 1)
        finally:
            conn.close()

    def test_misaligned_activity_still_visible_no_issue(self):
        """卡面验收 Q3：跨半节活动 mask 非 0 时保守取整可见，不记 issue。

        周一 8:20-9:00 同时压住 p1(8:00-8:45) 与 p2(8:55-9:40) 的边，
        mask=p1|p2——只可能多报不可能漏报，属设计内行为，不得刷屏。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            issues: list[ExpansionIssue] = []
            out = expand_once("2026-09-21T08:20", "2026-09-21T09:00",
                              cfg, issues=issues)
            self.assertEqual(out, {date(2026, 9, 21): bit(1, 1) | bit(1, 2)})
            self.assertEqual(issues, [])
        finally:
            conn.close()

    def test_build_occupancy_annotates_schedule_context(self):
        """信号经 build_occupancy 必须补齐日程上下文（id/kind/title）。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生甲")
            _add_once(conn, org, "sch_hd", "周末讲座",
                      "2026-09-26T10:00", "2026-09-26T11:00", stu)
            issues: list[ExpansionIssue] = []
            build_occupancy(conn, org, *WEEK, issues=issues)
            self.assertEqual(len(issues), 1)
            iss = issues[0]
            self.assertEqual(iss.code, ISSUE_ONCE_NO_SLOT)
            self.assertEqual(iss.schedule_id, "sch_hd")
            self.assertEqual(iss.schedule_title, "周末讲座")
            self.assertEqual(iss.schedule_kind, "once")
        finally:
            conn.close()

    def test_legacy_call_without_issues_unchanged(self):
        """旧调用点（不传 issues）行为与修前完全一致——向后兼容底线。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            out = expand_once("2026-09-26T10:00", "2026-09-26T11:00", cfg)
            self.assertEqual(out, {})  # 修前就是这个值
            entries = build_occupancy(conn, org, *WEEK)  # 修前签名可用
            self.assertEqual(entries, [])
        finally:
            conn.close()


class TestPeriodicNoTerm(unittest.TestCase):
    """静默点 2：日期不在任何 term 区间 → 课表凭空不见。"""

    def test_window_outside_term_signals(self):
        """窗口在 term 外（2027-01）→ 不展开 + periodic_no_term 计 7 天。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            issues: list[ExpansionIssue] = []
            out = expand_periodic(
                0, bit(1, 1), "2026-09-21", "2027-01-31",
                cfg, load_terms(conn, org),
                date(2027, 1, 4), date(2027, 1, 10),
                None, issues=issues,
            )
            self.assertEqual(out, {})
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].code, ISSUE_PERIODIC_NO_TERM)
            self.assertEqual(issues[0].count, 7)
        finally:
            conn.close()

    def test_weekend_and_parity_are_design_not_issues(self):
        """term 内整周扫描（含周末、含单双周过滤）不得产生任何 issue。

        周末 day_mask=0、单双周不匹配都是业务规则，不是数据缺陷。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            # week_parity=1（单周）→ 偶数周日期全被过滤，仍不报警
            issues: list[ExpansionIssue] = []
            out = expand_periodic(
                1, bit(1, 1) | bit(6, 1),  # 含周六位（周六无启用槽位）
                "2026-09-21", "2026-12-31",
                cfg, load_terms(conn, org), *WEEK, None, issues=issues,
            )
            self.assertEqual(out, {date(2026, 9, 21): bit(1, 1)})
            self.assertEqual(issues, [])
        finally:
            conn.close()

    def test_holiday_no_signal(self):
        """holiday 放假跳过是设计内，不得报 issue。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            conn.execute(
                "INSERT INTO calendar_exceptions(id, org_id, date, kind, note)"
                " VALUES ('cx_h','{}','2026-09-24','holiday','P0-3测试')".format(org)
            )
            conn.commit()
            issues: list[ExpansionIssue] = []
            out = expand_periodic(
                0, bit(4, 1), "2026-09-21", "2026-12-31",
                cfg, load_terms(conn, org), *WEEK,
                {"2026-09-24": "holiday"}, issues=issues,
            )
            self.assertNotIn(date(2026, 9, 24), out)
            self.assertEqual(issues, [])
        finally:
            conn.close()

    def test_scan_via_build_occupancy_no_term(self):
        """经 scan 全链路：有效期跨过 term 末尾的课表，term 外日期要报。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生甲")
            _add_periodic(conn, org, "sch_yw", "语文", bit(1, 1),
                          "2026-09-21", "2027-01-10", stu)
            report = scan(conn, org, date(2027, 1, 4), date(2027, 1, 10))
            self.assertEqual(report.count, 0)  # term 外物理上无课
            self.assertEqual(len(report.issues), 1)
            self.assertEqual(report.issues[0].code, ISSUE_PERIODIC_NO_TERM)
            self.assertEqual(report.issues[0].schedule_id, "sch_yw")
        finally:
            conn.close()


class TestCleanDataSilent(unittest.TestCase):
    """干净数据必须零信号——防止信号机制本身变成噪声源。"""

    def test_clean_conflicting_data_no_issues(self):
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生甲")
            _add_periodic(conn, org, "sch_yw", "语文", bit(1, 1),
                          "2026-09-21", "2026-12-31", stu)
            _add_periodic(conn, org, "sch_sx", "数学", bit(1, 1),
                          "2026-09-21", "2026-12-31", stu)
            _add_once(conn, org, "sch_hd", "班会",
                      "2026-09-23T10:00", "2026-09-23T10:45", stu)
            report = scan(conn, org, *WEEK)
            self.assertEqual(report.issues, [])
            self.assertGreater(report.count, 0)  # 冲突照常检出
        finally:
            conn.close()


class TestApiWarnings(unittest.TestCase):
    """/api/conflicts 的 warnings 透出（b3_kernels.conflict_payload）。"""

    def _payload(self, conn, org):
        from server import b3_kernels as K

        cfg = SimpleNamespace(today=date(2026, 9, 23))  # 周三 → 扫 09-21~09-27 周
        return K.conflict_payload(conn, org, cfg)

    def test_clean_payload_has_no_warnings_key(self):
        """干净数据：响应保持既有形态（无 warnings 字段），逐字兼容。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            payload = self._payload(conn, org)
            self.assertNotIn("warnings", payload)
            self.assertEqual(payload["count"], 0)
        finally:
            conn.close()

    def test_orphan_surfaces_as_warning(self):
        """孤儿行必须出现在 /api/conflicts 响应的 warnings 里。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生甲")
            _add_periodic(conn, org, "sch_yw", "语文", bit(1, 1),
                          "2026-09-21", "2026-12-31", stu)
            conn.execute("DELETE FROM schedule_periodic WHERE schedule_id = 'sch_yw'")
            conn.commit()
            payload = self._payload(conn, org)
            self.assertIn("warnings", payload)
            self.assertEqual(payload["warnings"][0]["code"], ISSUE_ORPHAN_SCHEDULE)
            self.assertEqual(payload["warnings"][0]["schedule_id"], "sch_yw")
            self.assertIn("detail", payload["warnings"][0])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
