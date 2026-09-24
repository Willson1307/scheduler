"""B4 批次验收测试：硬/软约束 + M2 冲突消解。

验收标准：
    1. 给出的方案必须真的消除冲突（应用后再扫描，冲突数下降）；
    2. 方案必须满足硬约束（不能把课挪到另一个已占用的位置）；
    3. 方案按代价排序，"最小改动"排在前面。
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import occupancy as occ  # noqa: E402
from scheduler import slots as sl  # noqa: E402
from scheduler import schedules as sch  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.object_graph import add_edge, create_object, create_organization  # noqa: E402
from scheduler.solver import constraints as cons  # noqa: E402
from scheduler.solver import m2  # noqa: E402
from scheduler.solver.m2 import apply_plan, resolve  # noqa: E402


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        init_db(self.conn)
        self.org, self.root = create_organization(self.conn, "测试校")
        self.cfg = sl.setup_default_slots(self.conn, self.org, periods_per_day=10)
        self.start = monday(date(2026, 9, 7))
        occ.create_term(
            self.conn,
            self.org,
            "2026 秋",
            self.start.isoformat(),
            (self.start + timedelta(days=8 * 7 - 1)).isoformat(),
        )
        self.teacher = create_object(self.conn, self.org, "user", "李老师")
        self.cls = create_object(self.conn, self.org, "group", "高一(1)班")
        add_edge(self.conn, self.root, self.cls)
        self.valid_to = (self.start + timedelta(days=8 * 7)).isoformat()

    def tearDown(self):
        self.conn.close()

    def week(self):
        return self.start, self.start + timedelta(days=6)


class TestConstraints(Base):
    def test_hard_conflict_detected(self):
        sch.create_periodic(
            self.conn,
            sch.NewSchedule(
                title="语文", org_id=self.org,
                participants={"teacher": [self.teacher]},
            ),
            slot_bitmap=sl.bit(1, 1),
            valid_from=self.start.isoformat(),
            valid_to=self.valid_to,
        )
        d0, _ = self.week()
        entries = occ.build_occupancy(self.conn, self.org, d0, d0 + timedelta(days=6), cfg=self.cfg)
        ctx = m2.build_context(self.conn, self.org, entries, cfg=self.cfg)
        self.assertTrue(
            cons.hard_violations(ctx, [self.teacher], d0, sl.bit(1, 1))
        )
        self.assertFalse(
            cons.hard_violations(ctx, [self.teacher], d0, sl.bit(1, 3))
        )

    def test_capacity_is_hard(self):
        room = create_object(self.conn, self.org, "resource", "小教室", capacity=20)
        d0, _ = self.week()
        ctx = m2.build_context(self.conn, self.org, [], cfg=self.cfg)
        self.assertTrue(
            cons.hard_violations(ctx, [room], d0, sl.bit(1, 1), group_size=45)
        )

    def test_daily_limit_is_soft(self):
        """单日超上限算软约束（有代价），不是硬约束。"""
        d0, _ = self.week()
        ctx = m2.build_context(self.conn, self.org, [], cfg=self.cfg)
        ctx.daily_limit = 2
        ctx.occupancy[(self.teacher, d0)] = sl.bit(1, 1) | sl.bit(1, 2)
        problems = cons.hard_violations(ctx, [self.teacher], d0, sl.bit(1, 3))
        self.assertEqual(problems, [])
        self.assertGreater(cons.soft_cost(ctx, [self.teacher], d0, sl.bit(1, 3)), 0)

    def test_weights_affect_cost(self):
        d0, _ = self.week()
        ctx = m2.build_context(self.conn, self.org, [], cfg=self.cfg)
        ctx.weights["morning"] = 5.0
        cheap = cons.soft_cost(ctx, [self.teacher], d0, sl.bit(1, 1))
        dear = cons.soft_cost(ctx, [self.teacher], d0, sl.bit(1, 10))
        self.assertGreater(dear, cheap)


class TestM2(Base):
    def _make_conflict(self):
        """一位教师周三第 1 节被排了两个班 → 冲突。"""
        c2 = create_object(self.conn, self.org, "group", "高一(2)班")
        add_edge(self.conn, self.root, c2)
        a = sch.create_periodic(
            self.conn,
            sch.NewSchedule(
                title="1班物理", org_id=self.org,
                participants={"target": [self.cls], "teacher": [self.teacher]},
            ),
            slot_bitmap=sl.bit(3, 1),
            valid_from=self.start.isoformat(), valid_to=self.valid_to,
        )
        b = sch.create_periodic(
            self.conn,
            sch.NewSchedule(
                title="2班物理", org_id=self.org,
                participants={"target": [c2], "teacher": [self.teacher]},
            ),
            slot_bitmap=sl.bit(3, 1),
            valid_from=self.start.isoformat(), valid_to=self.valid_to,
        )
        return a, b

    def test_plans_generated(self):
        self._make_conflict()
        d0, d1 = self.week()
        res = resolve(self.conn, self.org, d0, d1, cfg=self.cfg)
        self.assertGreater(len(res), 0)
        self.assertGreater(len(res[0].plans), 0)
        # 代价递增
        costs = [p.cost for p in res[0].plans]
        self.assertEqual(costs, sorted(costs))

    def test_plan_is_hard_valid(self):
        """给出的方案不能把课挪到另一个已占用的位置。"""
        self._make_conflict()
        d0, d1 = self.week()
        res = resolve(self.conn, self.org, d0, d1, cfg=self.cfg)
        entries = occ.build_occupancy(self.conn, self.org, d0, d1, cfg=self.cfg)
        ctx = m2.build_context(self.conn, self.org, entries, cfg=self.cfg)
        for r in res:
            for p in r.plans:
                for mv in p.moves:
                    people, resources = m2.related_objects(self.conn, mv.schedule_id)
                    involved = people + resources
                    mask = 0
                    for _w, i in mv.new_slots:
                        mask |= sl.bit(mv.new_date.isoweekday(), i)
                    self.assertEqual(
                        cons.hard_violations(ctx, involved, mv.new_date, mask), [],
                        msg=f"方案违反硬约束：{mv.describe()}",
                    )

    def test_applying_plan_removes_conflict(self):
        """真正的关键：应用方案之后，冲突必须真的消失。"""
        self._make_conflict()
        d0, d1 = self.week()
        before = occ.scan(self.conn, self.org, d0, d1, cfg=self.cfg).count
        self.assertGreater(before, 0)

        res = resolve(self.conn, self.org, d0, d1, cfg=self.cfg)
        target = next((r for r in res if r.plans), None)
        self.assertIsNotNone(target)
        apply_plan(self.conn, target.plans[0], self.cfg, m2._days(d0, d1))

        after = occ.scan(self.conn, self.org, d0, d1, cfg=self.cfg).count
        self.assertEqual(after, 0, msg="应用最优方案后仍存在冲突")

    def test_activity_move_is_preferred(self):
        """活动比课表更好挪，方案里应该优先出现"改期活动"。"""
        sch.create_periodic(
            self.conn,
            sch.NewSchedule(
                title="语文", org_id=self.org,
                participants={"target": [self.cls], "teacher": [self.teacher]},
            ),
            slot_bitmap=sl.bit(1, 1),
            valid_from=self.start.isoformat(), valid_to=self.valid_to,
        )
        d = self.start
        sch.create_once(
            self.conn,
            sch.NewSchedule(
                title="全年级大会", org_id=self.org,
                participants={"participant": [self.cls], "teacher": [self.teacher]},
            ),
            start_at=f"{d.isoformat()}T08:00",
            end_at=f"{d.isoformat()}T08:45",
        )
        res = resolve(self.conn, self.org, d, d + timedelta(days=6), cfg=self.cfg)
        self.assertGreater(len(res), 0)
        best = res[0].plans[0]
        self.assertFalse(best.moves[0].periodic, "应优先挪动一次性活动")
        self.assertIn("大会", best.moves[0].title)


    def test_resolve_and_apply_clears_all(self):
        """一键消解必须把冲突降到 0——这是 M2 的验收底线。"""
        from scheduler.seeds import seed_demo
        from scheduler.slots import load_slot_config

        conn = connect(":memory:")
        init_db(conn)
        info = seed_demo(conn, classes=2, students_per_class=5, teachers=9, rooms=2)
        cfg = load_slot_config(conn, info["org_id"])
        d0 = date.fromisoformat(info["term_start"])
        d1 = d0 + timedelta(days=6)

        before = occ.scan(conn, info["org_id"], d0, d1, cfg=cfg).count
        self.assertGreater(before, 0)
        applied, remaining = m2.resolve_and_apply(conn, info["org_id"], d0, d1, cfg=cfg)
        self.assertGreater(len(applied), 0)
        self.assertEqual(remaining, 0, "一键消解后不应再有冲突")
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
