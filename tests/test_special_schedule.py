"""B-1 夏冬作息（多套方案模型）测试 —— 卡 rOKKES 的五条必验 + 三条破坏法的落点。

复现背景与本次语义变更（2026-11-10）：
    修前 `occupancy.py:201` 写着「mode='special' 不落地（忽略不报错）」——
    老师换冬季作息后，课表照旧按夏季节次算，**接口 200、页面正常、零报错**。
    修后：该日节次配置换成 period_sets/set_slots 那套，并且
    「某节在方案里不存在」必须产出 special_slot_missing（不许静默消失，
    否则就是 PR #15 那类事故：数据没了、没人知道）。

本文件对应的五条必验（卡面原文）：
    1. special 日取方案时间：set 把第 1 节改到 09:00 → 该日第 1 节生效时间是 09:00
    2. 非 special 日不受影响：同一日程在普通日仍用 period_slots 默认时间
    3. 🔴 双读一致：expand_periodic() 与 hard_violations() 对同一天结论一致
    4. 🔴 缺节不静默：方案无 idx=8、有日程排第 8 节 → advisory 非空且含受影响条数
    5. 越界防护：idx=17 写不进去（CHECK）；方案不存在时 special 行 → 报错不静默

三条破坏法的「必须变红」用例：D1 → 必验 3（本文件 test_double_read_*）；
D2 → 必验 4（test_missing_slot_emits_advisory）；D3 → 必验 5（test_idx_bounds）。

运行：python -m unittest tests.test_special_schedule -v
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.occupancy import (  # noqa: E402
    ISSUE_SPECIAL_SLOT_MISSING,
    build_occupancy,
    effective_slot_config,
    ensure_period_set_tables,
    expand_periodic,
    load_exceptions,
    load_terms,
    scan,
)
from scheduler.seeds import seed_demo  # noqa: E402
from scheduler.slots import bit, load_slot_config, mask_to_hex, setup_default_slots  # noqa: E402
from scheduler.solver.constraints import hard_violations  # noqa: E402
from scheduler.solver.m2 import build_context  # noqa: E402
from server import auth, config  # noqa: E402
from server import api_period_sets  # noqa: E402
from server.config import ServerConfig  # noqa: E402

#: 学期与扫描窗口：2026-09-21 是周一
TERM_START, TERM_END = "2026-09-21", "2026-12-31"
WEEK = (date(2026, 9, 21), date(2026, 9, 27))
TUE = date(2026, 9, 22)  # 窗口内的周二：绑作息方案的目标日


def fresh():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def _setup(conn):
    """建组织 + 默认 10 节/天（周一到周五启用，08:00 起 45 分钟）+ 学期。"""
    org, _root = og.create_organization(conn, "测试校")
    cfg = setup_default_slots(conn, org)
    conn.execute(
        "INSERT INTO terms(id, org_id, name, start_date, end_date, week1_parity)"
        " VALUES (?,?,?,?,?,?)",
        ("term_1", org, "2026秋", TERM_START, TERM_END, 1),
    )
    conn.commit()
    return org, cfg


def _make_set(conn, org, set_id, name, rows):
    """rows: [(dow, idx, start, end, enabled)]"""
    conn.execute(
        "INSERT INTO period_sets(id, org_id, name, note) VALUES (?,?,?,?)",
        (set_id, org, name, None),
    )
    for i, (dow, idx, st, et, enabled) in enumerate(rows):
        conn.execute(
            "INSERT INTO set_slots(id, set_id, day_of_week, idx, start_time, end_time, enabled)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"{set_id}_s{i}", set_id, dow, idx, st, et, 1 if enabled else 0),
        )
    conn.commit()


def _ins_special(conn, org, iso, set_id, eid="se_sp", scope="day"):
    conn.execute(
        "INSERT INTO schedule_exceptions(id, org_id, scope, target_id, date, mode, set_id)"
        " VALUES (?,?,?,NULL,?,?,?)",
        (eid, org, scope, iso, "special", set_id),
    )
    conn.commit()


def _add_periodic(conn, org, sid, bitmap, vf, vt, obj_id, title="日程"):
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
        "INSERT INTO schedule_participants(schedule_id, object_id, role)"
        " VALUES (?,?,?)",
        (sid, obj_id, "target"),
    )
    conn.commit()


# ===========================================================================
# 表结构与旧库兜底
# ===========================================================================


class TestPeriodSetSchema(unittest.TestCase):
    def test_init_db_idempotent(self):
        """init_db 可重复调用（schema.sql 只许幂等 DDL，不许 ALTER）。"""
        conn = fresh()
        try:
            init_db(conn)
            init_db(conn)
            names = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertIn("period_sets", names)
            self.assertIn("set_slots", names)
        finally:
            conn.close()

    def test_ensure_adds_tables_to_old_db(self):
        """旧库缺两张新表 → ensure_period_set_tables 补上（不动已有数据）。"""
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            conn.execute("DROP TABLE set_slots")
            conn.execute("DROP TABLE period_sets")
            conn.commit()
            missing = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'period_sets'"
            ).fetchone()
            self.assertIsNone(missing)

            ensure_period_set_tables(conn)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'period_sets'"
                ).fetchone()
            )
            # 组织数据没被重建清空
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM organizations").fetchone()["n"], 1
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM period_slots WHERE org_id = ?", (org,)
                ).fetchone()["n"],
                70,
            )
        finally:
            conn.close()

    def test_idx_bounds(self):
        """必验 5①：idx=16 能写、idx=17 被 CHECK 拦住。

        🔴 破坏法 D3 的落点：把 schema.sql 的 `idx <= 16` 改成 `idx <= 8`，
        这里"idx=16 写入成功"就会变成 IntegrityError → 本用例变红。
        """
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _make_set(conn, org, "set_x", "边界方案", [(1, 16, "08:00", "08:45", True)])
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM set_slots WHERE set_id = 'set_x'"
                ).fetchone()["n"],
                1,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO set_slots"
                    "(id, set_id, day_of_week, idx, start_time, end_time, enabled)"
                    " VALUES ('ss_bad','set_x',1,17,'09:00','09:45',1)"
                )
        finally:
            conn.close()

    def test_enabled_zero_equals_missing_row(self):
        """语义定死：set_slots 只认 enabled=1；enabled=0 与缺行完全等价。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生甲")
            _add_periodic(
                conn, org, "sch_a", bit(2, 1) | bit(2, 2), TERM_START, TERM_END, stu
            )
            # 方案 A：(2,1) 启用，(2,2) 显式 enabled=0
            _make_set(
                conn, org, "set_off", "禁用行",
                [(2, 1, "09:00", "09:45", True), (2, 2, "09:50", "10:35", False)],
            )
            # 方案 B：只写 (2,1)，(2,2) 缺行
            _make_set(conn, org, "set_gap", "缺行",
                      [(2, 1, "09:00", "09:45", True)])

            _ins_special(conn, org, "2026-09-22", "set_off", eid="se_off")
            _ins_special(conn, org, "2026-09-29", "set_gap", eid="se_gap")
            exc = load_exceptions(conn, org)
            terms = load_terms(conn, org)
            a = expand_periodic(
                0, bit(2, 1) | bit(2, 2), TERM_START, TERM_END, cfg, terms,
                date(2026, 9, 22), date(2026, 9, 22), exc,
            )
            b = expand_periodic(
                0, bit(2, 1) | bit(2, 2), TERM_START, TERM_END, cfg, terms,
                date(2026, 9, 29), date(2026, 9, 29), exc,
            )
            self.assertEqual(a[date(2026, 9, 22)], bit(2, 1))
            self.assertEqual(a[date(2026, 9, 22)], b[date(2026, 9, 29)])
        finally:
            conn.close()


# ===========================================================================
# 必验 5②③④：special 行的错误语义（配置错误宁可报错）
# ===========================================================================


class TestLoadSpecialErrors(unittest.TestCase):
    def test_missing_set_id_raises(self):
        """必验 5②：set_id 指向不存在的方案 → ValueError，不静默当默认作息。"""
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _ins_special(conn, org, "2026-09-22", "set_nope")
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            self.assertIn("period_sets", str(cm.exception))
            self.assertIn("set_nope", str(cm.exception))
        finally:
            conn.close()

    def test_null_set_id_raises(self):
        """必验 5③：mode='special' 但 set_id 为 NULL → ValueError。"""
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _ins_special(conn, org, "2026-09-22", None)
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            self.assertIn("set_id", str(cm.exception))
        finally:
            conn.close()

    def test_conflicting_specials_raise(self):
        """必验 5④：同一天两条 special 且方案矛盾 → ValueError。"""
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _make_set(conn, org, "set_a", "方案A", [(2, 1, "08:00", "08:45", True)])
            _make_set(conn, org, "set_b", "方案B", [(2, 1, "09:00", "09:45", True)])
            _ins_special(conn, org, "2026-09-22", "set_a", eid="se_1")
            _ins_special(conn, org, "2026-09-22", "set_b", eid="se_2")
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            self.assertIn("互相矛盾", str(cm.exception))
        finally:
            conn.close()

    def test_same_set_twice_is_idempotent(self):
        """同一天同方案重复写 → 去重，不报错（幂等）。"""
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _make_set(conn, org, "set_a", "方案A", [(2, 1, "08:00", "08:45", True)])
            _ins_special(conn, org, "2026-09-22", "set_a", eid="se_1")
            _ins_special(conn, org, "2026-09-22", "set_a", eid="se_2")
            exc = load_exceptions(conn, org)
            self.assertEqual(exc.specials, {"2026-09-22": "set_a"})
        finally:
            conn.close()

    def test_schedule_scope_special_raises(self):
        """scope='schedule' + special 是没实现的组合 → 报错，不静默忽略。

        「过滤掉」等于静默跳过例外行，与「缺节不静默」同一条纪律。
        """
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _make_set(conn, org, "set_a", "方案A", [(2, 1, "08:00", "08:45", True)])
            _ins_special(conn, org, "2026-09-22", "set_a", scope="schedule")
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            self.assertIn("scope", str(cm.exception))
        finally:
            conn.close()

    def test_org_isolation(self):
        """A 组织的方案与绑定不得影响 B 组织（组织隔离红线）。"""
        conn = fresh()
        try:
            org_a, _cfg = _setup(conn)
            org_b, _root_b = og.create_organization(conn, "测试校B")
            _make_set(conn, org_a, "set_a", "方案A", [(2, 1, "09:00", "09:45", True)])
            _ins_special(conn, org_a, "2026-09-22", "set_a")
            self.assertEqual(load_exceptions(conn, org_a).specials, {"2026-09-22": "set_a"})
            self.assertEqual(load_exceptions(conn, org_b).specials, {})
        finally:
            conn.close()


# ===========================================================================
# 必验 1 / 2：special 日取方案时间、非 special 日不受影响
# ===========================================================================


class TestSpecialTakesEffect(unittest.TestCase):
    def test_special_day_uses_set_times(self):
        """必验 1：方案把第 1 节改到 09:00 → 该日第 1 节生效时间就是 09:00。

        `expand_periodic` 只返回掩码（位序与时间无关），所以「时间」的可断言
        口径就是 `effective_slot_config`——它也是 expand_periodic 内部用的
        同一个函数（唯一事实来源，不是另算一遍）。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            _make_set(conn, org, "set_w", "冬季作息",
                      [(2, 1, "09:00", "09:45", True), (2, 2, "09:55", "10:40", True)])
            _ins_special(conn, org, "2026-09-22", "set_w")
            exc = load_exceptions(conn, org)

            self.assertEqual(cfg.get(2, 1)["start_time"], "08:00")  # 默认方案
            special_day = effective_slot_config(exc, cfg, "2026-09-22")
            self.assertEqual(special_day.get(2, 1)["start_time"], "09:00")
            self.assertEqual(special_day.get(2, 1)["end_time"], "09:45")
        finally:
            conn.close()

    def test_normal_day_untouched(self):
        """必验 2：非 special 日仍用 period_slots 默认时间，掩码逐字节不变。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生甲")
            _add_periodic(conn, org, "sch_a", bit(2, 1), TERM_START, TERM_END, stu)
            terms = load_terms(conn, org)
            before = expand_periodic(
                0, bit(2, 1), TERM_START, TERM_END, cfg, terms,
                date(2026, 9, 21), date(2026, 9, 27),
            )

            _make_set(conn, org, "set_w", "冬季作息", [(2, 1, "09:00", "09:45", True)])
            _ins_special(conn, org, "2026-09-22", "set_w")
            exc = load_exceptions(conn, org)
            after = expand_periodic(
                0, bit(2, 1), TERM_START, TERM_END, cfg, terms,
                date(2026, 9, 21), date(2026, 9, 27), exc,
            )
            # 未绑定的日期（含 09-29 之外的整个窗口）完全一致
            for d in (date(2026, 9, 21), date(2026, 9, 27)):
                self.assertEqual(before.get(d), after.get(d))
            # 未绑定的周二（09-29 在本窗口之外，但配置来源必须是默认 cfg 本身）
            self.assertIs(effective_slot_config(exc, cfg, "2026-09-29"), cfg)
            # 09-22 绑定的是「只有第 1 节、时间 09:00」的方案 → 掩码不变但来源换了
            self.assertEqual(after[date(2026, 9, 22)], bit(2, 1))
            self.assertIsNot(effective_slot_config(exc, cfg, "2026-09-22"), cfg)
        finally:
            conn.close()


# ===========================================================================
# 必验 4：缺节不静默（advisory）
# ===========================================================================


class TestMissingSlotAdvisory(unittest.TestCase):
    def test_missing_slot_emits_advisory(self):
        """必验 4：方案无 idx=8、日程排在第 8 节 → advisory 非空且含受影响条数。

        🔴 破坏法 D2 的落点：把 expand_periodic 里这条 append 改成 continue，
        本用例立刻变红（修前就是这样：课表少一节课，接口 200，零报错）。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生甲")
            _add_periodic(conn, org, "sch_a", bit(2, 8), TERM_START, TERM_END, stu)
            # 方案只有第 1、2 节
            _make_set(conn, org, "set_w", "冬季作息",
                      [(2, 1, "08:00", "08:45", True), (2, 2, "08:55", "09:40", True)])
            _ins_special(conn, org, "2026-09-22", "set_w")
            exc = load_exceptions(conn, org)

            issues = []
            days = expand_periodic(
                0, bit(2, 8), TERM_START, TERM_END, cfg, load_terms(conn, org),
                date(2026, 9, 22), date(2026, 9, 22), exc, issues=issues,
            )
            self.assertEqual(days, {})  # 不占用是对的
            self.assertEqual([i.code for i in issues], [ISSUE_SPECIAL_SLOT_MISSING])
            iss = issues[0]
            self.assertEqual(iss.count, 1)
            self.assertEqual(iss.dates, ["2026-09-22"])
            self.assertIn("第 8 节", iss.detail)
            self.assertIn("冬季作息", iss.detail)
            # 走 build_occupancy 也要能带出来（/api/conflicts 的 warnings 来源）
            issues2 = []
            build_occupancy(conn, org, *WEEK, cfg=cfg, issues=issues2)
            self.assertEqual(
                [i.code for i in issues2], [ISSUE_SPECIAL_SLOT_MISSING]
            )
            self.assertEqual(issues2[0].schedule_id, "sch_a")
            self.assertEqual(issues2[0].to_payload()["count"], 1)
        finally:
            conn.close()

    def test_partial_loss_also_reported(self):
        """部分丢节也要报：日程占第 1 与第 8 节，方案只有第 1 节。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生乙")
            _add_periodic(
                conn, org, "sch_b", bit(2, 1) | bit(2, 8), TERM_START, TERM_END, stu
            )
            _make_set(conn, org, "set_w", "冬季作息", [(2, 1, "08:00", "08:45", True)])
            _ins_special(conn, org, "2026-09-22", "set_w")
            exc = load_exceptions(conn, org)
            issues = []
            days = expand_periodic(
                0, bit(2, 1) | bit(2, 8), TERM_START, TERM_END, cfg,
                load_terms(conn, org), date(2026, 9, 22), date(2026, 9, 22), exc,
                issues=issues,
            )
            self.assertEqual(days[date(2026, 9, 22)], bit(2, 1))  # 第 1 节仍上
            self.assertEqual([i.code for i in issues], [ISSUE_SPECIAL_SLOT_MISSING])
            self.assertIn("第 8 节", issues[0].detail)
        finally:
            conn.close()

    def test_empty_set_is_legal_but_advisory(self):
        """方案存在但零节次行 = 全天无课（合法），必须报 advisory 而不是抛错。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            stu = og.create_object(conn, org, "user", "学生丙")
            _add_periodic(conn, org, "sch_c", bit(2, 1), TERM_START, TERM_END, stu)
            conn.execute(
                "INSERT INTO period_sets(id, org_id, name) VALUES ('set_holiday',?,'假期方案')",
                (org,),
            )
            conn.commit()
            _ins_special(conn, org, "2026-09-22", "set_holiday")
            exc = load_exceptions(conn, org)
            issues = []
            days = expand_periodic(
                0, bit(2, 1), TERM_START, TERM_END, cfg, load_terms(conn, org),
                date(2026, 9, 22), date(2026, 9, 22), exc, issues=issues,
            )
            self.assertEqual(days, {})
            self.assertEqual([i.code for i in issues], [ISSUE_SPECIAL_SLOT_MISSING])
            self.assertIn("假期方案", issues[0].detail)
        finally:
            conn.close()


# ===========================================================================
# 必验 3：双读一致（expand_periodic ↔ hard_violations）
# ===========================================================================


class TestDoubleRead(unittest.TestCase):
    """🔴 本项目最贵的坑：只改一处 → 占用显示空着、排课按有课。

    这里的做法是从 hard_violations 的真实入口走一遍
    （build_occupancy → build_context → ctx.occupancy → hard_violations），
    而不是只断言 expand_periodic 的返回值。
    """

    def _build(self, conn):
        org, cfg = _setup(conn)
        stu = og.create_object(conn, org, "user", "学生甲")
        # 两条日程都占周二第 8 节：默认作息下必然冲突
        _add_periodic(conn, org, "sch_a", bit(2, 8), TERM_START, TERM_END, stu, "社团A")
        _add_periodic(conn, org, "sch_b", bit(2, 8), TERM_START, TERM_END, stu, "社团B")
        # 正向对照：第 1 节的日程（方案里仍然存在）
        _add_periodic(conn, org, "sch_c", bit(2, 1), TERM_START, TERM_END, stu, "班会")
        return org, cfg, stu

    def test_double_read_consistent_after_special(self):
        conn = fresh()
        try:
            org, cfg, stu = self._build(conn)
            # 默认作息：周二第 8 节两条日程真冲突（前置条件，不是结论）
            self.assertGreaterEqual(scan(conn, org, TUE, TUE).count, 1)

            _make_set(conn, org, "set_w", "冬季作息",
                      [(2, 1, "08:00", "08:45", True)])  # 只有第 1、无第 8 节
            _ins_special(conn, org, "2026-09-22", "set_w")

            # ① 显示侧（expand_periodic）：第 8 节不再展开
            exc = load_exceptions(conn, org)
            out = expand_periodic(
                0, bit(2, 8), TERM_START, TERM_END, cfg, load_terms(conn, org),
                TUE, TUE, exc,
            )
            self.assertEqual(out, {})

            # ② 排课侧（hard_violations）：同一天同一节也必须判「没占用」
            entries = build_occupancy(conn, org, TUE, TUE, cfg=cfg)
            ctx = build_context(conn, org, entries, cfg=cfg)
            self.assertEqual(
                hard_violations(ctx, [stu], TUE, bit(2, 8)), []
            )
            # ③ 正向对照：第 1 节在方案里存在 → 仍要报占用（防「两边都空」假绿）
            self.assertNotEqual(
                hard_violations(ctx, [stu], TUE, bit(2, 1)), []
            )
        finally:
            conn.close()

    def test_no_special_keeps_conflict_both_sides(self):
        """不绑方案时两条链结论一致（回归：默认日必须照旧报冲突）。"""
        conn = fresh()
        try:
            org, cfg, stu = self._build(conn)
            entries = build_occupancy(conn, org, TUE, TUE, cfg=cfg)
            ctx = build_context(conn, org, entries, cfg=cfg)
            self.assertNotEqual(
                hard_violations(ctx, [stu], TUE, bit(2, 8)), []
            )
            self.assertGreaterEqual(scan(conn, org, TUE, TUE).count, 1)
        finally:
            conn.close()


# ===========================================================================
# 接口：三个最小路由 + 鉴权 + 隔离
# ===========================================================================


class TestPeriodSetApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_cfg = config._current
        cls.tmpdir = tempfile.mkdtemp(prefix="b1_api_")
        cls.db_path = os.path.join(cls.tmpdir, "t.db")
        conn = connect(cls.db_path)
        init_db(conn)
        seed_demo(
            conn, classes=1, students_per_class=2, teachers=2, rooms=1,
            term_start="2026-09-07",
        )
        conn.commit()
        admin = conn.execute(
            "SELECT id, login FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()
        teacher = conn.execute(
            "SELECT id, login FROM users WHERE role = 'teacher' LIMIT 1"
        ).fetchone()
        cls.admin_id, cls.admin_login = admin["id"], admin["login"]
        cls.teacher_id, cls.teacher_login = teacher["id"], teacher["login"]
        conn.close()
        config.set_current_config(
            ServerConfig(db_path=cls.db_path, today=date(2026, 9, 9), seed_if_missing=False)
        )

    @classmethod
    def tearDownClass(cls):
        config._current = cls._old_cfg
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _headers(self, user_id, login, role, oid="obj_x"):
        return {"authorization": f"Bearer {auth.issue_token(user_id, login, role, oid)}"}

    def _create(self, headers, name="冬季作息", slots=None):
        if slots is None:
            slots = [
                {"day_of_week": 2, "idx": 1, "start_time": "09:00", "end_time": "09:45"},
                {"day_of_week": 2, "idx": 2, "start_time": "09:55", "end_time": "10:40"},
            ]
        body = json.dumps({"name": name, "slots": slots}).encode("utf-8")
        return api_period_sets.create_period_set(
            "POST", "/api/period-sets", {}, body, headers, None
        )

    def test_routes_registered(self):
        """ROUTES 非空且三条都在 doctor 的期望清单里（否则 doctor 查不出缺失）。"""
        self.assertEqual(
            set(api_period_sets.ROUTES),
            {
                ("POST", "/api/period-sets"),
                ("GET", "/api/period-sets"),
                ("GET", "/api/period-sets/detail"),
            },
        )
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        import doctor  # noqa: E402

        for key in api_period_sets.ROUTES:
            self.assertIn(key, doctor.EXPECTED_ROUTES)

    def test_requires_login(self):
        status, payload = api_period_sets.create_period_set(
            "POST", "/api/period-sets", {}, b"{}", {}, None
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_teacher_forbidden(self):
        """教师天花板 = propose → 建方案 403（不是 401、不是静默成功）。"""
        status, payload = self._create(
            self._headers(self.teacher_id, self.teacher_login, "teacher")
        )
        self.assertEqual(status, 403)
        self.assertIn("own", payload["error"]["message"])

    def test_admin_create_list_detail(self):
        headers = self._headers(self.admin_id, self.admin_login, "admin")
        # 成功路径返回 dict（分发层统一成 200），失败路径返回 (status, payload)
        created = self._create(headers, name="考试周")
        self.assertTrue(created["ok"])
        set_id = created["set_id"]
        self.assertEqual(created["slot_count"], 2)
        self.assertEqual(created["enabled_slot_count"], 2)

        listing = api_period_sets.list_period_sets(
            "GET", "/api/period-sets", {}, b"", headers, None
        )
        self.assertIn(set_id, [i["id"] for i in listing["items"]])
        detail = api_period_sets.period_set_detail(
            "GET", "/api/period-sets/detail", {"id": [set_id]}, b"", headers, None
        )
        self.assertEqual(detail["set"]["name"], "考试周")
        self.assertEqual(detail["slot_count"], 2)
        self.assertEqual(detail["slots"][0]["start_time"], "09:00")

    def test_idx_out_of_range_rejected(self):
        headers = self._headers(self.admin_id, self.admin_login, "admin")
        status, payload = self._create(
            headers,
            name="越界",
            slots=[{"day_of_week": 2, "idx": 17, "start_time": "09:00", "end_time": "09:45"}],
        )
        self.assertEqual(status, 400)
        self.assertIn("16", payload["error"]["message"])

    def test_duplicate_slot_rejected(self):
        headers = self._headers(self.admin_id, self.admin_login, "admin")
        row = {"day_of_week": 3, "idx": 4, "start_time": "09:00", "end_time": "09:45"}
        status, payload = self._create(headers, name="重复", slots=[row, dict(row)])
        self.assertEqual(status, 400)
        self.assertIn("重复", payload["error"]["message"])

    def test_detail_missing_id_and_not_found(self):
        headers = self._headers(self.admin_id, self.admin_login, "admin")
        status, payload = api_period_sets.period_set_detail(
            "GET", "/api/period-sets/detail", {}, b"", headers, None
        )
        self.assertEqual(status, 400)
        status, payload = api_period_sets.period_set_detail(
            "GET", "/api/period-sets/detail", {"id": ["set_nope"]}, b"", headers, None
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
