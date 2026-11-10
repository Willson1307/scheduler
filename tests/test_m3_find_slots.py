"""M3 局部补排：`m2.find_slots()` 的验收用例 —— 卡 rbOAef 的六条必验 + 三条破坏法。

设计意图（为什么这些用例长这样）：

    `find_slots()` 是**只读**能力：给某个班/某位老师 + 一段日期，列出
    「连续 N 节全空」的可排位置。它最容易犯的错不是算错，而是**给出一个
    排不进去的位置**——不报错、返回 200、老师照着排就失败。本项目最痛的
    就是这个形态（PR #15 / P0-3 全是它）。所以本文件的重心是「候选必须是
    真的能排」，而不是「有没有返回点什么」：

      ① `special` 日（冬季作息没有第 8 节）⛔ 不得出现第 7/8 节；
      ② 每个候选都必须过 `hard_violations()`（与占用矩阵交叉验证）；
      ③ 全满时返回 `[]` 而不是抛异常（"没空位"是正常业务结果）。

    另外两条本卡自己加的口径（卡面没写但同属"给不存在的候选"）：
      · 放假当天整日跳过；
      · 不在任何 term 内的日期整日跳过。

运行：python -m unittest tests.test_m3_find_slots -v
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.occupancy import build_occupancy, occupancy_map  # noqa: E402
from scheduler.slots import bit, mask_to_hex, setup_default_slots  # noqa: E402
from scheduler.solver.constraints import hard_violations  # noqa: E402
from scheduler.solver.m2 import build_context, find_slots  # noqa: E402

#: 2026-09-21 是周一；2026-09-26 是周六（as_of 调休用例要用）
TERM_START, TERM_END = "2026-09-21", "2026-12-31"
MON, TUE, FRI, SAT = (
    date(2026, 9, 21),
    date(2026, 9, 22),
    date(2026, 9, 25),
    date(2026, 9, 26),
)
WEEK = (MON, date(2026, 9, 27))

#: 默认作息：周一~周五各 10 节（setup_default_slots 的默认值）
ENABLED_DOWS = range(1, 6)
PERIODS = range(1, 11)

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
)


def fresh():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def _setup(conn):
    """建组织 + 默认 10 节/天（周一~周五启用）+ 一个学期。"""
    org, _root = og.create_organization(conn, "测试校")
    cfg = setup_default_slots(conn, org)
    conn.execute(
        "INSERT INTO terms(id, org_id, name, start_date, end_date, week1_parity)"
        " VALUES (?,?,?,?,?,?)",
        ("term_1", org, "2026秋", TERM_START, TERM_END, 1),
    )
    conn.commit()
    return org, cfg


def _add_periodic(conn, org, sid, bitmap, obj_id, vf=TERM_START, vt=TERM_END, title="日程"):
    conn.execute(
        "INSERT INTO schedules(id, org_id, kind, title) VALUES (?,?,?,?)",
        (sid, org, "periodic", title),
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


def _make_set(conn, org, set_id, name, rows):
    """rows: [(dow, idx, start, end, enabled)]"""
    conn.execute(
        "INSERT INTO period_sets(id, org_id, name) VALUES (?,?,?)", (set_id, org, name)
    )
    for i, (dow, idx, st, et, enabled) in enumerate(rows):
        conn.execute(
            "INSERT INTO set_slots(id, set_id, day_of_week, idx, start_time, end_time, enabled)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"{set_id}_s{i}", set_id, dow, idx, st, et, 1 if enabled else 0),
        )
    conn.commit()


def _bind_special(conn, org, iso, set_id, eid="se_sp"):
    """该日换作息方案（B-1 的 mode='special'）。"""
    conn.execute(
        "INSERT INTO schedule_exceptions(id, org_id, scope, target_id, date, mode, set_id)"
        " VALUES (?,?,?,NULL,?,?,?)",
        (eid, org, "day", iso, "special", set_id),
    )
    conn.commit()


def _bind_as_of(conn, org, iso, dow, eid="se_asof"):
    """调休：该日按 dow 的课表上课（P0-2 的 mode='as_of'）。"""
    conn.execute(
        "INSERT INTO schedule_exceptions(id, org_id, scope, target_id, date, mode, as_of_dow)"
        " VALUES (?,?,?,NULL,?,?,?)",
        (eid, org, "day", iso, "as_of", dow),
    )
    conn.commit()


def _add_holiday(conn, org, iso):
    conn.execute(
        "INSERT INTO calendar_exceptions(id, org_id, date, kind) VALUES (?,?,?,?)",
        (f"ce_{iso}", org, iso, "holiday"),
    )
    conn.commit()


def _dump(conn):
    """整库关键表快照（用于「只读，不写数据」断言）。"""
    snap = {}
    for t in _TABLES:
        snap[t] = [tuple(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()]
    return snap


class FindSlotsBase(unittest.TestCase):
    def _fixture(self):
        conn = fresh()
        self.addCleanup(conn.close)
        org, cfg = _setup(conn)
        klass = og.create_object(conn, org, "group", "示例1班")
        teacher = og.create_object(conn, org, "user", "张老师")
        return conn, org, cfg, klass, teacher

    def _busy_map(self, conn, org, cfg, d0, d1):
        entries = build_occupancy(conn, org, d0, d1, cfg=cfg)
        return occupancy_map(entries)


# ===========================================================================
# 必验 1：有空位时能返回候选，且 periods 确实是空的（build_occupancy 交叉验证）
# ===========================================================================


class TestReturnsRealCandidates(FindSlotsBase):
    def test_half_empty_class_returns_candidates(self):
        """必验 1：把一个班排满周一的 1~8 节，剩下 9、10 节必须能被找到。"""
        conn, org, cfg, klass, _t = self._fixture()
        _add_periodic(conn, org, "sch_am", sum(bit(1, i) for i in range(1, 9)), klass)

        got = find_slots(conn, org, target=klass, date_range=(MON, MON))
        self.assertTrue(got, "周一 9、10 节是空的，必须返回候选")
        self.assertEqual(sorted(c["periods"] for c in got), [[9], [10]])
        # 1~8 节已被占用，⛔ 不得出现
        for c in got:
            self.assertNotIn(True, [p <= 8 for p in c["periods"]])

        # 交叉验证：候选掩码与该班当天占用掩码按位与必须为 0
        busy = self._busy_map(conn, org, cfg, MON, MON)
        for c in got:
            d = date.fromisoformat(c["date"])
            self.assertEqual(busy.get((klass, d), 0) & c["mask"], 0, c)
            for p in c["periods"]:
                self.assertEqual(busy.get((klass, d), 0) & bit(d.isoweekday(), p), 0)

    def test_candidate_mask_matches_bit_vocabulary(self):
        """mask 走 m2 内部口径（周级 0-based），periods 与 mask 必须自洽。"""
        conn, org, _cfg, klass, _t = self._fixture()
        got = find_slots(conn, org, target=klass, date_range=(TUE, TUE), limit=3)
        self.assertTrue(got)
        for c in got:
            expect = sum(bit(c["day_of_week"], p) for p in c["periods"])
            self.assertEqual(c["mask"], expect)
            self.assertEqual(c["mask_hex"], format(expect, "x"))

    def test_teacher_target_supported(self):
        """target 可以是教师（user）：他有课的那些节次不得再被推荐。"""
        conn, org, _cfg, _klass, teacher = self._fixture()
        _add_periodic(conn, org, "sch_t", sum(bit(3, i) for i in range(1, 11)), teacher)

        got = find_slots(conn, org, target=teacher, date_range=(date(2026, 9, 23),) * 2)
        self.assertEqual(got, [], "这位老师周三 1~10 节全满，必须没有候选")

        got2 = find_slots(conn, org, target=teacher, date_range=(TUE, TUE))
        self.assertTrue(got2, "周二他全空，必须返回候选")

    def test_limit_is_respected_and_order_stable(self):
        """limit 生效，且两次调用结果完全一致（可复现）。"""
        conn, org, _cfg, klass, _t = self._fixture()
        a = find_slots(conn, org, target=klass, date_range=WEEK, limit=5)
        b = find_slots(conn, org, target=klass, date_range=WEEK, limit=5)
        self.assertEqual(len(a), 5)
        self.assertEqual(a, b)
        self.assertEqual(find_slots(conn, org, target=klass, date_range=WEEK, limit=0), [])


# ===========================================================================
# 必验 2：全满时返回 []（⛔ 不抛异常）
# ===========================================================================


class TestFullReturnsEmpty(FindSlotsBase):
    def test_full_week_returns_empty_list(self):
        """必验 2：整周所有启用节次都被占 → 返回 []。

        🔴 破坏法 D3 的落点：把 `return []` 改成抛异常（或 assert），本用例立刻变红。
        「没空位」是正常业务结果，不是错误。
        """
        conn, org, _cfg, klass, _t = self._fixture()
        everything = sum(bit(d, i) for d in ENABLED_DOWS for i in PERIODS)
        _add_periodic(conn, org, "sch_all", everything, klass)

        got = find_slots(conn, org, target=klass, date_range=WEEK)
        self.assertEqual(got, [])

        # 连续 2 节同样没有位置
        self.assertEqual(find_slots(conn, org, target=klass, date_range=WEEK, duration=2), [])

    def test_empty_because_weekend_only(self):
        """周六周末默认不排课（day_mask=0）→ 只有周末的区间返回 []。"""
        conn, org, _cfg, klass, _t = self._fixture()
        self.assertEqual(
            find_slots(conn, org, target=klass, date_range=(SAT, date(2026, 9, 27))), []
        )


# ===========================================================================
# 必验 3：按 cost 升序
# ===========================================================================


class TestCostOrdering(FindSlotsBase):
    def test_cost_monotonic_non_decreasing(self):
        """必验 3：cost 单调不降（同 cost 按日期升序）。"""
        conn, org, _cfg, klass, _t = self._fixture()
        got = find_slots(conn, org, target=klass, date_range=WEEK, limit=50)
        self.assertGreater(len(got), 5)
        costs = [c["cost"] for c in got]
        self.assertEqual(costs, sorted(costs))
        # 同 cost 内日期必须升序
        for prev, cur in zip(got, got[1:]):
            if prev["cost"] == cur["cost"]:
                self.assertLessEqual(prev["date"], cur["date"])

    def test_early_period_cheaper_than_last_period(self):
        """软约束「避开末节 / 靠近上午」生效：第 1 节必须比第 10 节便宜。

        这条同时守住「cost 不是常数」——若 cost 恒为 0，排序断言也会绿，是假绿。
        """
        conn, org, _cfg, klass, _t = self._fixture()
        got = find_slots(conn, org, target=klass, date_range=(MON, MON), limit=50)
        by_period = {c["periods"][0]: c["cost"] for c in got}
        self.assertIn(1, by_period)
        self.assertIn(10, by_period)
        self.assertLess(by_period[1], by_period[10])


# ===========================================================================
# 必验 4：候选通过 hard_violations（与占用交叉验证）
# ===========================================================================


class TestNoHardViolations(FindSlotsBase):
    def test_every_candidate_passes_hard_violations(self):
        """必验 4：对每个候选调 hard_violations() → 必须是 []。

        🔴 破坏法 D1 的落点。注意实测结论（已写进交付报告）：
        把这一句删掉**本用例仍会绿** —— 因为候选是从 `day_mask & ~busy`
        里生成的，与 ctx.occupancy 同源，校验在这里是「第二道防线」而非唯一防线。
        能真正打红它的是「让 free mask 忽略占用」同时「去掉校验」两步一起做。
        """
        conn, org, cfg, klass, _t = self._fixture()
        _add_periodic(conn, org, "sch_a", sum(bit(1, i) for i in range(1, 6)), klass)
        _add_periodic(conn, org, "sch_b", sum(bit(1, i) for i in (7, 8)), klass)
        got = find_slots(conn, org, target=klass, date_range=(MON, MON), limit=50)
        self.assertTrue(got)

        ctx = build_context(
            conn, org, build_occupancy(conn, org, MON, MON, cfg=cfg), cfg=cfg
        )
        for c in got:
            d = date.fromisoformat(c["date"])
            self.assertEqual(hard_violations(ctx, [klass], d, c["mask"]), [], c)


# ===========================================================================
# 必验 5：🔴 special 日不得给出不存在的节次（本卡最关键的一条）
# ===========================================================================


class TestSpecialDayHasNoMissingPeriods(FindSlotsBase):
    def test_special_day_limited_to_its_own_periods(self):
        """必验 5：绑定「只有第 1~6 节」的作息方案 → 当天不得出现第 7/8 节。

        🔴 破坏法 D2 的落点：把 `effective_slot_config(...)` 换成外面传进来的
        默认 cfg，本用例立刻变红（默认作息有 10 节 → 会推荐第 7~10 节，
        而那几节在冬季作息那天根本不存在：不报错、返回 200、排不进去）。
        """
        conn, org, _cfg, klass, _t = self._fixture()
        rows = [(2, i, "08:00", "08:45", True) for i in range(1, 7)]  # 周二只有 1~6 节
        _make_set(conn, org, "set_winter", "冬季作息", rows)
        _bind_special(conn, org, TUE.isoformat(), "set_winter")

        got = find_slots(conn, org, target=klass, date_range=(TUE, TUE), limit=50)
        self.assertTrue(got, "1~6 节全空，必须有候选")
        for c in got:
            self.assertLessEqual(max(c["periods"]), 6, c)
            for p in c["periods"]:
                self.assertIn(p, range(1, 7), c)
        # 断言「确实给满了 6 个单节候选」，防止只返回 1 个也能绿
        self.assertEqual(sorted(c["periods"][0] for c in got), [1, 2, 3, 4, 5, 6])

    def test_special_day_duration_two_stays_inside_set(self):
        """special 日的连续 2 节也必须整段落在方案里（不得跨到第 7 节）。"""
        conn, org, _cfg, klass, _t = self._fixture()
        rows = [(2, i, "08:00", "08:45", True) for i in range(1, 7)]
        _make_set(conn, org, "set_winter", "冬季作息", rows)
        _bind_special(conn, org, TUE.isoformat(), "set_winter")

        got = find_slots(conn, org, target=klass, date_range=(TUE, TUE), duration=2, limit=50)
        self.assertTrue(got)
        for c in got:
            self.assertEqual(c["periods"][1] - c["periods"][0], 1)
            self.assertLessEqual(c["periods"][-1], 6, c)

    def test_normal_day_still_gets_all_periods(self):
        """反向对照：没绑方案的周三仍是 10 节全可用（防「一律砍到 6 节」假绿）。"""
        conn, org, _cfg, klass, _t = self._fixture()
        rows = [(2, i, "08:00", "08:45", True) for i in range(1, 7)]
        _make_set(conn, org, "set_winter", "冬季作息", rows)
        _bind_special(conn, org, TUE.isoformat(), "set_winter")

        got = find_slots(conn, org, target=klass, date_range=(date(2026, 9, 23),) * 2, limit=50)
        self.assertEqual(max(c["periods"][0] for c in got), 10)


# ===========================================================================
# 必验 6：连续 duration 正确
# ===========================================================================


class TestDuration(FindSlotsBase):
    def test_duration_two_and_three_are_contiguous(self):
        """必验 6：duration=2/3 返回的 periods 必须连续且长度正确。"""
        conn, org, _cfg, klass, _t = self._fixture()
        # 周一：1~3 节被占 → 只剩 4~10 是 7 连空；先占 5、9 造出缺口，验证不会跨缺口
        _add_periodic(conn, org, "sch_gap", bit(1, 1) | bit(1, 2) | bit(1, 3)
                      | bit(1, 5) | bit(1, 9), klass)

        for need in (2, 3):
            got = find_slots(conn, org, target=klass, date_range=(MON, MON),
                             duration=need, limit=50)
            self.assertTrue(got, f"duration={need} 应能找到位置")
            for c in got:
                self.assertEqual(len(c["periods"]), need)
                self.assertEqual(c["periods"][-1] - c["periods"][0] + 1, need, c)

        got2 = find_slots(conn, org, target=klass, date_range=(MON, MON), duration=2, limit=50)
        periods = sorted(c["periods"][0] for c in got2)
        # 缺口 5、9 两侧都必须被排除：允许的起点只有 4、6、7、8、10-1？
        # 实际可用连续段 = [4,6,7,8,10] 的单点，故 duration=2 只剩 (6,7)/(7,8)
        self.assertEqual(periods, [6, 7])

    def test_duration_bigger_than_free_run_returns_nothing(self):
        """整天空闲只有 1 节时，duration=2 必须返回 []（不是硬凑一个不连续候选）。"""
        conn, org, _cfg, klass, _t = self._fixture()
        _add_periodic(conn, org, "sch_almost", sum(bit(1, i) for i in range(1, 10)), klass)
        got = find_slots(conn, org, target=klass, date_range=(MON, MON), duration=2)
        self.assertEqual(got, [])


# ===========================================================================
# 本卡自加的两条口径：放假 / 无 term 的日期不给候选
# ===========================================================================


class TestSkipNonTeachingDays(FindSlotsBase):
    def test_holiday_is_skipped(self):
        """放假日整日跳过（否则会推荐一个「那天不上课」的位置）。"""
        conn, org, _cfg, klass, _t = self._fixture()
        _add_holiday(conn, org, TUE.isoformat())
        got = find_slots(conn, org, target=klass, date_range=(TUE, TUE))
        self.assertEqual(got, [])

    def test_day_outside_terms_is_skipped(self):
        """不在任何 term 内的日期跳过（无 term 就没有周次，排不进去）。"""
        conn, org, _cfg, klass, _t = self._fixture()
        got = find_slots(conn, org, target=klass, date_range=(MON, MON))
        self.assertTrue(got)
        outside = (date(2027, 3, 1), date(2027, 3, 2))  # 学期结束后
        self.assertEqual(find_slots(conn, org, target=klass, date_range=outside), [])

    def test_as_of_makeup_day_uses_target_dow_vocabulary(self):
        """调休日（周六按周一上课）→ 用 as_of_dow 的槽位，且位号也在那个空间里。"""
        conn, org, _cfg, klass, _t = self._fixture()
        _bind_as_of(conn, org, SAT.isoformat(), 1)
        got = find_slots(conn, org, target=klass, date_range=(SAT, SAT), limit=50)
        self.assertTrue(got, "调休日必须能给出候选，否则补排工具在调休周失效")
        for c in got:
            self.assertEqual(c["date"], SAT.isoformat())
            self.assertEqual(c["day_of_week"], 6)  # 自然周几仍是周六
            self.assertEqual(c["as_of_dow"], 1)    # 按周一的课表排
            self.assertEqual(c["mask"], sum(bit(1, p) for p in c["periods"]))


# ===========================================================================
# 只读承诺 + 参数校验
# ===========================================================================


class TestReadOnlyAndValidation(FindSlotsBase):
    def test_does_not_write_anything(self):
        """只读能力：调用前后关键表逐行一致（含 period_slots / 例外表）。"""
        conn, org, _cfg, klass, _t = self._fixture()
        _add_periodic(conn, org, "sch_a", bit(1, 1) | bit(1, 2), klass)
        before = _dump(conn)
        find_slots(conn, org, target=klass, date_range=(MON, date(2026, 9, 30)),
                   duration=2, limit=20)
        self.assertEqual(before, _dump(conn))

    def test_unknown_target_raises(self):
        """找不到对象 → ValueError（静默返回 [] 会被误读成「真的没空位」）。"""
        conn, org, _cfg, _klass, _t = self._fixture()
        with self.assertRaises(ValueError) as cm:
            find_slots(conn, org, target="obj_nope", date_range=(MON, MON))
        self.assertIn("obj_nope", str(cm.exception))

    def test_bad_arguments_raise(self):
        conn, org, _cfg, klass, _t = self._fixture()
        with self.assertRaises(ValueError):
            find_slots(conn, org, target=klass, date_range=(MON, MON), duration=0)
        with self.assertRaises(ValueError):
            find_slots(conn, org, target=klass, date_range=(MON, MON), duration=17)
        with self.assertRaises(ValueError):
            find_slots(conn, org, target=klass, date_range=(TUE, MON))
        with self.assertRaises(ValueError):
            find_slots(conn, org, target=klass, date_range=[MON, TUE])  # 不是 tuple

    def test_org_isolation(self):
        """A 组织的日程不得影响 B 组织的空位结论。"""
        conn, org_a, _cfg, klass_a, _t = self._fixture()
        org_b, _root = og.create_organization(conn, "测试校B")
        setup_default_slots(conn, org_b)
        conn.execute(
            "INSERT INTO terms(id, org_id, name, start_date, end_date, week1_parity)"
            " VALUES (?,?,?,?,?,?)",
            ("term_b", org_b, "2026秋", TERM_START, TERM_END, 1),
        )
        conn.commit()
        klass_b = og.create_object(conn, org_b, "group", "示例2班")
        _add_periodic(conn, org_a, "sch_a", bit(1, 1), klass_a)

        got_b = find_slots(conn, org_b, target=klass_b, date_range=(MON, MON), limit=50)
        self.assertEqual(max(c["periods"][0] for c in got_b), 10, "B 组织不应被 A 的日程占用")


if __name__ == "__main__":
    unittest.main()
