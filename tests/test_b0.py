"""B0 批次验收测试。

运行方式（两种都行，测试只用标准库 unittest 写成，零第三方依赖）：
    python -m unittest discover -s tests -v
    pytest tests -v

验收标准：全部通过。这批测试是后续批次的底座——它不通过就不许往上层盖。
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler import occupancy as occ  # noqa: E402
from scheduler import schedules as sch  # noqa: E402
from scheduler import slots as sl  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.seeds import seed_demo  # noqa: E402


def fresh():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------------------
class TestSlots(unittest.TestCase):
    def test_bit_roundtrip(self):
        m = sl.bit(1, 1) | sl.bit(5, 10)
        self.assertEqual(sl.decode_mask(m), [(1, 1), (5, 10)])

    def test_hex_roundtrip(self):
        m = sl.bit(3, 2) | sl.bit(7, 16)
        self.assertEqual(sl.hex_to_mask(sl.mask_to_hex(m)), m)

    def test_popcount(self):
        m = sl.bit(1, 1) | sl.bit(1, 2) | sl.bit(2, 5)
        self.assertEqual(sl.popcount(m), 3)

    def test_bit_position_stable(self):
        """位序必须与配置无关：改节次数不会让已有掩码错位。"""
        self.assertEqual(sl.bit_index(2, 1), sl.MAX_PERIODS_PER_DAY)
        self.assertEqual(sl.bit_index(1, 3), 2)

    def test_mask_for_range(self):
        conn = fresh()
        org, _root = og.create_organization(conn, "测试校")
        sl.setup_default_slots(conn, org, periods_per_day=10)
        cfg = sl.load_slot_config(conn, org)
        mask, aligned = cfg.mask_for_range(1, "08:00", "08:45")
        self.assertEqual(sl.decode_mask(mask), [(1, 1)])
        self.assertTrue(aligned)
        mask2, aligned2 = cfg.mask_for_range(1, "08:00", "09:40")
        self.assertEqual(sl.decode_mask(mask2), [(1, 1), (1, 2)])
        self.assertFalse(aligned2)  # 跨了两节，非整节
        conn.close()


# ---------------------------------------------------------------------------
class TestObjectGraph(unittest.TestCase):
    def setUp(self):
        self.conn = fresh()
        self.org, self.root = og.create_organization(self.conn, "测试校")

    def tearDown(self):
        self.conn.close()

    def test_root_group_created(self):
        self.assertIsNotNone(self.root)
        row = self.conn.execute(
            "SELECT is_root FROM objects WHERE id = ?", (self.root,)
        ).fetchone()
        self.assertEqual(row["is_root"], 1)

    def test_self_edge(self):
        u = og.create_object(self.conn, self.org, "user", "张三")
        self.assertTrue(og.is_ancestor(self.conn, u, u))

    def test_multi_parent(self):
        """一个学生同时属于班级和社团——单亲树做不到，DAG 可以。"""
        cls = og.create_object(self.conn, self.org, "group", "高一(1)班")
        club = og.create_object(self.conn, self.org, "group", "机器人社")
        stu = og.create_object(self.conn, self.org, "user", "李四")
        og.add_edge(self.conn, self.root, cls)
        og.add_edge(self.conn, self.root, club)
        og.add_edge(self.conn, cls, stu)
        og.add_edge(self.conn, club, stu)
        self.assertIn(stu, og.descendants(self.conn, cls))
        self.assertIn(stu, og.descendants(self.conn, club))
        self.assertEqual(len(og.groups_of(self.conn, stu)), 3)  # 两个组 + 根组

    def test_transitive_closure(self):
        grade = og.create_object(self.conn, self.org, "group", "高一")
        cls = og.create_object(self.conn, self.org, "group", "高一(1)班")
        stu = og.create_object(self.conn, self.org, "user", "王五")
        og.add_edge(self.conn, self.root, grade)
        og.add_edge(self.conn, grade, cls)
        og.add_edge(self.conn, cls, stu)
        # 年级能直接查到孙节点学生
        self.assertIn(stu, og.descendants(self.conn, grade))
        rows = self.conn.execute(
            "SELECT depth FROM object_edges WHERE ancestor_id = ? AND descendant_id = ?",
            (grade, stu),
        ).fetchone()
        self.assertEqual(rows["depth"], 2)

    def test_cycle_rejected(self):
        a = og.create_object(self.conn, self.org, "group", "A")
        b = og.create_object(self.conn, self.org, "group", "B")
        og.add_edge(self.conn, a, b)
        with self.assertRaises(og.CycleError):
            og.add_edge(self.conn, b, a)

    def test_remove_edge(self):
        cls = og.create_object(self.conn, self.org, "group", "班级")
        stu = og.create_object(self.conn, self.org, "user", "赵六")
        og.add_edge(self.conn, cls, stu)
        self.assertIn(stu, og.descendants(self.conn, cls))
        og.remove_edge(self.conn, cls, stu)
        self.assertNotIn(stu, og.descendants(self.conn, cls))

    def test_object_without_account(self):
        """1500 个学生可以只有 objects 行、没有 users 行。"""
        stu = og.create_object(self.conn, self.org, "user", "无账号学生")
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM users WHERE object_id = ?", (stu,)
        ).fetchone()
        self.assertEqual(row["c"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM objects WHERE id = ?", (stu,))
            .fetchone()["c"],
            1,
        )

    def test_password(self):
        u = og.create_object(self.conn, self.org, "user", "教师甲")
        og.create_user(self.conn, u, "jia", "secret", role="teacher")
        self.assertIsNotNone(og.authenticate(self.conn, "jia", "secret"))
        self.assertIsNone(og.authenticate(self.conn, "jia", "wrong"))


# ---------------------------------------------------------------------------
class TestOccupancy(unittest.TestCase):
    def setUp(self):
        self.conn = fresh()
        self.org, self.root = og.create_organization(self.conn, "测试校")
        self.cfg = sl.setup_default_slots(self.conn, self.org, periods_per_day=10)
        self.start = monday(date(2026, 9, 7))
        occ.create_term(
            self.conn,
            self.org,
            "2026 秋",
            self.start.isoformat(),
            (self.start + timedelta(days=20 * 7 - 1)).isoformat(),
        )
        self.cls = og.create_object(self.conn, self.org, "group", "高一(1)班")
        self.teacher = og.create_object(self.conn, self.org, "user", "李老师")
        self.room = og.create_object(self.conn, self.org, "resource", "教室101", capacity=50)
        og.add_edge(self.conn, self.root, self.cls)

    def tearDown(self):
        self.conn.close()

    def _week(self, n=0):
        return self.start + timedelta(days=7 * n), self.start + timedelta(days=7 * n + 6)

    def test_periodic_expansion(self):
        sch.create_periodic(
            self.conn,
            sch.NewSchedule(
                title="语文",
                org_id=self.org,
                participants={"target": [self.cls], "teacher": [self.teacher]},
                resources=[self.room],
            ),
            slot_bitmap=sl.bit(1, 1),
            valid_from=self.start.isoformat(),
            valid_to=(self.start + timedelta(days=60)).isoformat(),
        )
        d0, d6 = self._week(0)
        entries = occ.build_occupancy(self.conn, self.org, d0, d6, cfg=self.cfg)
        monday_entries = [e for e in entries if e.date == d0]
        self.assertTrue(any(e.object_id == self.cls for e in monday_entries))
        self.assertTrue(any(e.object_id == self.room for e in monday_entries))
        # 教师每一天都被占用 → 一周 1 次
        teacher_days = sorted(e.date for e in entries if e.object_id == self.teacher)
        self.assertEqual(teacher_days, [d0])

    def test_week_parity(self):
        sch.create_periodic(
            self.conn,
            sch.NewSchedule(
                title="单周生物",
                org_id=self.org,
                participants={"target": [self.cls]},
            ),
            slot_bitmap=sl.bit(2, 3),
            week_parity=1,  # 仅单周
            valid_from=self.start.isoformat(),
            valid_to=(self.start + timedelta(days=60)).isoformat(),
        )
        entries = occ.build_occupancy(
            self.conn, self.org, self.start, self.start + timedelta(days=27), cfg=self.cfg
        )
        days = sorted({e.date for e in entries})
        # 第 1、3 周出现（周二），第 2、4 周不出现
        self.assertEqual(len(days), 2)
        self.assertEqual(days[0], self.start + timedelta(days=1))
        self.assertEqual(days[1], self.start + timedelta(days=15))

    def test_conflict_detection(self):
        """同一教师同一时间排两个班 → 必须检出冲突，且能指出是哪一节。"""
        c2 = og.create_object(self.conn, self.org, "group", "高一(2)班")
        og.add_edge(self.conn, self.root, c2)
        spec = lambda t: sch.NewSchedule(  # noqa: E731
            title=t,
            org_id=self.org,
            participants={"target": [self.cls], "teacher": [self.teacher]},
        )
        valid_to = (self.start + timedelta(days=60)).isoformat()
        spec2 = sch.NewSchedule(
            title="另一班的课",
            org_id=self.org,
            participants={"target": [c2], "teacher": [self.teacher]},
        )
        sch.create_periodic(
            self.conn, spec("物理A"), slot_bitmap=sl.bit(3, 1),
            valid_from=self.start.isoformat(), valid_to=valid_to,
        )
        sch.create_periodic(
            self.conn, spec2, slot_bitmap=sl.bit(3, 1),
            valid_from=self.start.isoformat(), valid_to=valid_to,
        )
        d0, d6 = self._week(0)
        report = occ.scan(self.conn, self.org, d0, d6, cfg=self.cfg)
        self.assertGreater(report.count, 0)
        c = report.conflicts[0]
        self.assertEqual(c.object_id, self.teacher)
        self.assertEqual(c.slots, [(3, 1)])  # 周三第 1 节

    def test_no_false_conflict(self):
        """不同时间不应误报。"""
        valid_to = (self.start + timedelta(days=60)).isoformat()
        sch.create_periodic(
            self.conn,
            sch.NewSchedule(
                title="语文", org_id=self.org, participants={"teacher": [self.teacher]}
            ),
            slot_bitmap=sl.bit(1, 1),
            valid_from=self.start.isoformat(),
            valid_to=valid_to,
        )
        sch.create_periodic(
            self.conn,
            sch.NewSchedule(
                title="数学", org_id=self.org, participants={"teacher": [self.teacher]}
            ),
            slot_bitmap=sl.bit(1, 2),
            valid_from=self.start.isoformat(),
            valid_to=valid_to,
        )
        d0, d6 = self._week(0)
        self.assertEqual(occ.scan(self.conn, self.org, d0, d6, cfg=self.cfg).count, 0)

    def test_activity_conflicts_with_timetable(self):
        """核心需求：一次性活动必须能被周期课表看见，否则排课会撞车。"""
        valid_to = (self.start + timedelta(days=60)).isoformat()
        sch.create_periodic(
            self.conn,
            sch.NewSchedule(
                title="语文",
                org_id=self.org,
                participants={"target": [self.cls], "teacher": [self.teacher]},
            ),
            slot_bitmap=sl.bit(1, 1),
            valid_from=self.start.isoformat(),
            valid_to=valid_to,
        )
        d = self.start  # 周一
        sch.create_once(
            self.conn,
            sch.NewSchedule(
                title="全年级大会",
                org_id=self.org,
                participants={"participant": [self.cls], "teacher": [self.teacher]},
            ),
            start_at=f"{d.isoformat()}T08:00",
            end_at=f"{d.isoformat()}T08:45",
        )
        report = occ.scan(self.conn, self.org, d, d, cfg=self.cfg)
        self.assertGreater(report.count, 0)

    def test_free_slots_and_load(self):
        sch.create_periodic(
            self.conn,
            sch.NewSchedule(
                title="语文", org_id=self.org, participants={"teacher": [self.teacher]}
            ),
            slot_bitmap=sl.bit(1, 1) | sl.bit(1, 2),
            valid_from=self.start.isoformat(),
            valid_to=(self.start + timedelta(days=60)).isoformat(),
        )
        d0, d6 = self._week(0)
        entries = occ.build_occupancy(self.conn, self.org, d0, d6, cfg=self.cfg)
        free = occ.find_free_slots(entries, [self.teacher], d0, self.cfg)
        self.assertEqual(free & sl.bit(1, 1), 0)
        self.assertNotEqual(free & sl.bit(1, 3), 0)
        self.assertEqual(occ.total_load(entries, self.teacher), 2)

    def test_cross_day_activity(self):
        """跨天活动按两条 once 存，展开后落在两天。"""
        d1 = self.start + timedelta(days=10)  # 周三
        d2 = d1 + timedelta(days=1)  # 周四
        gid = "evt_test"
        sch.create_once(
            self.conn,
            sch.NewSchedule(
                title="运动会", org_id=self.org, participants={"participant": [self.cls]}
            ),
            start_at=f"{d1.isoformat()}T08:00",
            end_at=f"{d1.isoformat()}T17:00",
            group_id=gid,
        )
        sch.create_once(
            self.conn,
            sch.NewSchedule(
                title="运动会", org_id=self.org, participants={"participant": [self.cls]}
            ),
            start_at=f"{d2.isoformat()}T08:00",
            end_at=f"{d2.isoformat()}T17:00",
            group_id=gid,
        )
        entries = occ.build_occupancy(self.conn, self.org, d1, d2, cfg=self.cfg)
        self.assertEqual(len({e.date for e in entries}), 2)


# ---------------------------------------------------------------------------
class TestSeeds(unittest.TestCase):
    def test_seed_demo_runs(self):
        conn = fresh()
        info = seed_demo(conn, classes=2, students_per_class=3, teachers=3, rooms=2)
        d0 = date.fromisoformat(info["term_start"])
        report = occ.scan(conn, info["org_id"], d0, d0 + timedelta(days=6))
        self.assertGreater(report.count, 0)  # 故意制造的冲突确实能被检出
        self.assertIsNotNone(og.authenticate(conn, "admin", "admin123"))
        conn.close()

    def test_cascade_delete(self):
        conn = fresh()
        info = seed_demo(conn, classes=1, students_per_class=2, teachers=1, rooms=1)
        sid = info["schedule_ids"][0]
        conn.execute("DELETE FROM schedules WHERE id = ?", (sid,))
        conn.commit()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) c FROM schedule_participants WHERE schedule_id = ?", (sid,)
            ).fetchone()["c"],
            0,
        )
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
