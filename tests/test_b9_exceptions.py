"""B9（S21）验收测试：/api/exceptions 临时微调（周期日程 × 单日 cancel/move）。

运行：python -m unittest tests.test_b9_exceptions -v

---------------------------------------------------------------------------
🔴🔴 本文件钉死的四处口径（每一处都对应一个「不报错但结果错」的真实风险）
---------------------------------------------------------------------------

  1. **掩码两套位序**：API 收/出单日掩码（1-based），入库周掩码（0-based）。
     混用会让课表整体错位且不报错——本文件用黄金用例 + 反向断言钉死。
  2. **cancel 只取消那一天**：例外键是 (schedule_id, ISO)。若有人改成按
     (周几) 或 (周次) 定位，同周几的其他周会被连带取消——这是「改一周
     结果三周课没了」的静默事故，Q2 的结构性保证由 test_no_cross_week_*
     两条守死。
  3. **DELETE 完全还原**：删掉例外后占用必须逐位恢复原状。教师不敢用的
     根因就是「改了回不去」，这条是验收主线。
  4. **冲突立即消失**：cancel 后 /api/conflicts 必须真的少一处，而不是
     「占用显示空着、排课时按有课处理」（docs/07 §11 实现约束 1）。

---------------------------------------------------------------------------
测试基建说明
---------------------------------------------------------------------------

领域层用内存库（与 test_p0_2_makeup / test_p0_3_signal 同一套 fresh/_setup），
接口层直接调 handler（与 test_server_authz_e2e 的 token 签发同源），
不起真实 HTTP 服务——断言粒度更细、跑得更快，且 401/403/400/404/409
每条状态码都有对应用例。
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
from scheduler.occupancy import (  # noqa: E402
    ISSUE_MOVE_NO_SLOT,
    DayExceptions,
    apply_schedule_exceptions,
    build_occupancy,
    create_term,
    find_conflicts,
    load_exceptions,
    load_terms,
    scan,
)
from scheduler.slots import (  # noqa: E402
    bit,
    mask_to_hex,
    setup_default_slots,
)
from scheduler.solver import m2  # noqa: E402
from server import api_exceptions as API  # noqa: E402
from server import auth, b9_kernels as K  # noqa: E402
from server import config  # noqa: E402
from server.config import ServerConfig  # noqa: E402

#: 学期起点（周一）。与 test_p0_2_makeup 同一套日期，便于对照。
MONDAY = date(2026, 9, 21)
WED = date(2026, 9, 23)          # 本周三
WED_NEXT = date(2026, 9, 30)     # 下周三（同 dow，另一周）
SAT = date(2026, 9, 26)          # 周六（默认不排课）


# ---------------------------------------------------------------------------
# 领域层基建
# ---------------------------------------------------------------------------


def fresh():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def _setup(conn, term_end="2026-12-31"):
    """组织 + 默认节次（周一~周五 10 节，周末 enabled=0）+ 单学期。"""
    org, _root = og.create_organization(conn, "测试校")
    cfg = setup_default_slots(conn, org)
    create_term(conn, org, "2026秋", "2026-09-21", term_end)
    return org, cfg


def _add_periodic(conn, org, sid, title, bitmap, vf="2026-09-21", vt="2026-12-31",
                  parity=0, obj_id=None, role="target"):
    conn.execute(
        "INSERT INTO schedules(id, org_id, kind, title) VALUES (?,?,?,?)",
        (sid, org, "periodic", title),
    )
    conn.execute(
        "INSERT INTO schedule_periodic"
        "(schedule_id, week_parity, slot_bitmap, valid_from, valid_to)"
        " VALUES (?,?,?,?,?)",
        (sid, parity, mask_to_hex(bitmap), vf, vt),
    )
    if obj_id is not None:
        conn.execute(
            "INSERT INTO schedule_participants(schedule_id, object_id, role)"
            " VALUES (?,?,?)",
            (sid, obj_id, role),
        )
    conn.commit()


def _add_once(conn, org, sid, title, start_at, end_at, obj_id=None):
    conn.execute(
        "INSERT INTO schedules(id, org_id, kind, title) VALUES (?,?,?,?)",
        (sid, org, "once", title),
    )
    conn.execute(
        "INSERT INTO schedule_once(schedule_id, start_at, end_at) VALUES (?,?,?)",
        (sid, start_at, end_at),
    )
    if obj_id is not None:
        conn.execute(
            "INSERT INTO schedule_participants(schedule_id, object_id, role)"
            " VALUES (?,?,?)",
            (sid, obj_id, "target"),
        )
    conn.commit()


def _ins_exc(conn, eid, org, d, mode, target_id=None, new_mask=None, scope="schedule",
             set_id=None, note="B9测试"):
    conn.execute(
        "INSERT INTO schedule_exceptions"
        "(id, org_id, scope, target_id, date, mode, new_mask, set_id, note)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (eid, org, scope, target_id, d, mode, new_mask, set_id, note),
    )
    conn.commit()


def _conflict_dates(conn, org, cfg, lo=MONDAY, hi=date(2026, 9, 27)):
    """某窗口内冲突涉及的全部 (日程对, 日期)，排序后返回（用于逐字比对）。"""
    entries = build_occupancy(conn, org, lo, hi, cfg=cfg)
    return sorted(
        (c.date, c.schedule_a, c.schedule_b) for c in find_conflicts(entries)
    )


# ===========================================================================
# 一、掩码口径（🔴 本项目最贵的坑）
# ===========================================================================


class TestMaskOrder(unittest.TestCase):
    """两套位序：API 单日掩码（1-based）vs 入库周掩码（0-based）。"""

    def test_week_mask_bit_order_is_0based(self):
        """周掩码：周三第 1 节 = 位号 (3-1)*16+0 = 32 → 0x100000000。"""
        self.assertEqual(bit(3, 1), 0x100000000)

    def test_golden_case_api_to_week(self):
        """黄金用例：API "0x000002"（周三第1节）→ 周掩码 0x100000000。"""
        self.assertEqual(K.api_mask_to_week(3, 0x000002), 0x100000000)
        self.assertEqual(K.api_mask_to_week(3, 0x000008), bit(3, 3))
        self.assertEqual(K.api_mask_to_week(3, 0x000006), bit(3, 1) | bit(3, 2))

    def test_golden_case_week_to_api(self):
        """黄金用例反向：周掩码 0x100000000 → 单日掩码 0x000002。"""
        self.assertEqual(K.week_to_api_mask(3, 0x100000000), 0x000002)
        self.assertEqual(K.week_to_api_mask(3, bit(3, 1) | bit(3, 3)), 0x00000A)

    def test_two_orders_are_never_equal(self):
        """🔴 反向断言：两个值绝不相等。

        这条是本文件最重要的断言之一——若有人把入库/出参改成同一套位序，
        上面两条 assertEqual 仍可能各自成立，只有这条能抓住「用错了函数」。
        """
        self.assertNotEqual(0x000002, 0x100000000)
        self.assertNotEqual(K.api_mask_to_week(3, 0x000002), 0x000002)

    def test_week_to_api_drops_other_days(self):
        """周掩码里属于其他天的位必须被丢掉（按天编址，不是全局累加）。"""
        mixed = bit(3, 1) | bit(4, 2)  # 周三第1 + 周四第2
        self.assertEqual(K.week_to_api_mask(3, mixed), 0x000002)
        self.assertEqual(K.week_to_api_mask(4, mixed), 0x000004)

    def test_roundtrip_is_idempotent(self):
        """Q3 幂等：week→api→week 与 api→week→api 都不漂移。"""
        for api in (0x2, 0x6, 0x8, 0x200, 0x1FFFE):
            self.assertEqual(
                K.week_to_api_mask(3, K.api_mask_to_week(3, api)), api
            )
        for week in (bit(3, 1), bit(3, 1) | bit(3, 5), bit(3, 10)):
            self.assertEqual(
                K.api_mask_to_week(3, K.week_to_api_mask(3, week)), week
            )

    def test_day_mask_hex_width_matches_b3(self):
        """出参宽度与 /api/schedules grid 一致（0x%06x），前端已在用。"""
        from server import b3_kernels as B3

        self.assertEqual(B3.day_mask_hex(0x2), "0x000002")
        self.assertEqual(B3.day_mask_hex(0x8), "0x000008")


# ===========================================================================
# 二、DayExceptions / load_exceptions（第三类语义落地）
# ===========================================================================


class TestDayExceptionsModel(unittest.TestCase):
    """DayExceptions 新字段 + load_exceptions 的 schedule 级读取。"""

    def test_new_fields_default_empty(self):
        """cancels/moves 默认空；旧字典 coerce 不受影响。"""
        exc = DayExceptions()
        self.assertEqual(exc.cancels, set())
        self.assertEqual(exc.moves, {})
        self.assertTrue(exc.is_holiday("2026-09-24") is False)

    def test_coerce_legacy_dict_still_works(self):
        """旧签名 {ISO: kind} 兼容不破：holiday 生效，其余惰性。"""
        exc = DayExceptions.coerce({"2026-09-24": "holiday", "2026-09-23": "makeup"})
        self.assertEqual(exc.holidays, {"2026-09-24"})
        self.assertEqual(exc.cancels, set())
        self.assertEqual(exc.moves, {})

    def test_helper_methods(self):
        exc = DayExceptions(
            cancels={("sch_a", "2026-09-23")},
            moves={("sch_b", "2026-09-23"): bit(3, 3)},
        )
        self.assertTrue(exc.is_cancelled("sch_a", "2026-09-23"))
        self.assertFalse(exc.is_cancelled("sch_a", "2026-09-24"))
        self.assertEqual(exc.moved_mask("sch_b", "2026-09-23"), bit(3, 3))
        self.assertIsNone(exc.moved_mask("sch_b", "2026-09-24"))

    def test_load_reads_cancel(self):
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _ins_exc(conn, "se_c", org, "2026-09-23", "cancel", target_id="sch_a")
            exc = load_exceptions(conn, org)
            self.assertEqual(exc.cancels, {("sch_a", "2026-09-23")})
            self.assertEqual(exc.moves, {})
        finally:
            conn.close()

    def test_load_reads_move_as_int_week_mask(self):
        """DB 存 hex 文本 → 读出来必须是 int 周掩码（不是字符串！）。"""
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _ins_exc(conn, "se_m", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 3)))
            exc = load_exceptions(conn, org)
            self.assertEqual(exc.moves, {("sch_a", "2026-09-23"): bit(3, 3)})
            # 🔴 反向断言：绝不是那个 hex 字符串本身
            self.assertNotEqual(exc.moves[("sch_a", "2026-09-23")], mask_to_hex(bit(3, 3)))
        finally:
            conn.close()

    def test_load_move_null_mask_raises(self):
        """mode='move' 但 new_mask 为 NULL → ValueError（P0-3：配置错误宁可报错）。"""
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _ins_exc(conn, "se_m", org, "2026-09-23", "move", target_id="sch_a")
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            self.assertIn("new_mask", str(cm.exception))
        finally:
            conn.close()

    def test_load_move_bad_hex_raises(self):
        """new_mask 不是合法十六进制 → ValueError，绝不静默当 0（0=取消，语义全变）。"""
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _ins_exc(conn, "se_m", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask="zzzz")
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            self.assertIn("十六进制", str(cm.exception))
        finally:
            conn.close()

    def test_load_conflicting_moves_raise(self):
        """同 key 两条 move 且掩码不同 → ValueError（矛盾数据必须先修）。

        🔴 这条是 409 重复检查的存在理由：不拦的话一条脏数据就能让全组织
        /api/conflicts 500。
        """
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _ins_exc(conn, "se_m1", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 3)))
            _ins_exc(conn, "se_m2", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 4)))
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            self.assertIn("矛盾", str(cm.exception))
        finally:
            conn.close()

    def test_load_identical_moves_ok(self):
        """同 key 两条 move 且掩码相同 → 不抛错（冗余但一致，非矛盾数据）。"""
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _ins_exc(conn, "se_m1", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 3)))
            _ins_exc(conn, "se_m2", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 3)))
            exc = load_exceptions(conn, org)
            self.assertEqual(exc.moves, {("sch_a", "2026-09-23"): bit(3, 3)})
        finally:
            conn.close()

    def test_load_schedule_scope_null_target_raises(self):
        """scope='schedule' 但 target_id 为 NULL → ValueError（不知道改哪条日程）。"""
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _ins_exc(conn, "se_x", org, "2026-09-23", "cancel", target_id=None)
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            self.assertIn("target_id", str(cm.exception))
        finally:
            conn.close()

    def test_load_special_missing_set_raises(self):
        """🔴 B-1 变更：mode='special' 不再「忽略不报错」。

        修前口径（P0-2 时代）：special 不落地、不抛错。
        修后（卡 rOKKES）：special 会被读取；`set_id` 指向不存在的方案或
        scope != 'day' 一律 ValueError —— 「查不到就当默认作息用」是典型
        静默失效（课表与配置不一致且无人知晓）。
        完整语义用例见 tests/test_special_schedule.py。
        """
        conn = fresh()
        try:
            org, _cfg = _setup(conn)
            _ins_exc(conn, "se_s", org, "2026-09-25", "special", set_id="set_x")
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            # 该行默认 scope='schedule' → 先被 scope 校验拦下（不许静默忽略）
            self.assertIn("scope", str(cm.exception))

            conn.execute("UPDATE schedule_exceptions SET scope = 'day' WHERE id = 'se_s'")
            conn.commit()
            with self.assertRaises(ValueError) as cm:
                load_exceptions(conn, org)
            self.assertIn("set_x", str(cm.exception))
        finally:
            conn.close()

    def test_load_org_isolation(self):
        """A 组织的例外不得被 B 组织读到（组织级数据隔离红线）。"""
        conn = fresh()
        try:
            org_a, _cfg = _setup(conn)
            org_b, _root_b = og.create_organization(conn, "乙校")
            _ins_exc(conn, "se_a", org_a, "2026-09-23", "cancel", target_id="sch_a")
            exc_b = load_exceptions(conn, org_b)
            self.assertEqual(exc_b.cancels, set())
            self.assertEqual(exc_b.moves, {})
        finally:
            conn.close()


# ===========================================================================
# 三、apply_schedule_exceptions（Q2：不误伤其他周 / 跨 term 边界）
# ===========================================================================


class TestApplyScheduleExceptions(unittest.TestCase):
    """逐日应用的语义边界。"""

    def test_cancel_removes_only_that_day(self):
        exc = DayExceptions(cancels={("sch_a", "2026-09-23")})
        days = {date(2026, 9, 21): bit(1, 1), WED: bit(3, 1), WED_NEXT: bit(3, 1)}
        out = apply_schedule_exceptions(days, "sch_a", exc)
        self.assertEqual(out, {date(2026, 9, 21): bit(1, 1), WED_NEXT: bit(3, 1)})
        self.assertNotIn(WED, out)

    def test_move_overrides_only_that_day(self):
        exc = DayExceptions(moves={("sch_a", "2026-09-23"): bit(3, 3)})
        days = {WED: bit(3, 1), WED_NEXT: bit(3, 1)}
        out = apply_schedule_exceptions(days, "sch_a", exc)
        self.assertEqual(out[WED], bit(3, 3))       # 改点生效
        self.assertEqual(out[WED_NEXT], bit(3, 1))  # 下周同 dow 不动

    def test_other_schedule_unaffected(self):
        """例外只打中它声明的那条日程——同一天另一条课不受影响。"""
        exc = DayExceptions(cancels={("sch_a", "2026-09-23")})
        days = {WED: bit(3, 1)}
        out = apply_schedule_exceptions(days, "sch_b", exc)
        self.assertEqual(out, {WED: bit(3, 1)})

    def test_no_cross_week_same_dow(self):
        """🔴 Q2 主断言：取消周三，不得牵连下周三（同 dow、另一周）。

        若实现按 (dow) 或 (周次) 定位而非 ISO 日期，这里会一次挂两条——
        「取消一节课，三周课没了」的静默事故。
        """
        exc = DayExceptions(cancels={("sch_a", "2026-09-23")})
        days = {WED: bit(3, 1), WED_NEXT: bit(3, 1), date(2026, 10, 7): bit(3, 1)}
        out = apply_schedule_exceptions(days, "sch_a", exc)
        self.assertEqual(sorted(out), [WED_NEXT, date(2026, 10, 7)])

    def test_no_cross_term_boundary(self):
        """🔴 Q2 第二条：跨 term 边界的同 dow 日期互不牵连。

        2026-10-05 是 term1 最后一个周一，2026-10-12 是 term2 第一个周一。
        取消前者，后者必须照常。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn, term_end="2026-10-11")
            create_term(conn, org, "2026冬", "2026-10-12", "2026-12-31")
            obj = og.create_object(conn, org, "group", "高一1班")
            _add_periodic(conn, org, "sch_a", "周一早课", bit(1, 1), obj_id=obj)
            _ins_exc(conn, "se_c", org, "2026-10-05", "cancel", target_id="sch_a")
            entries = build_occupancy(
                conn, org, date(2026, 10, 5), date(2026, 10, 12), cfg=cfg
            )
            days = {e.date: e.mask for e in entries if e.schedule_id == "sch_a"}
            self.assertNotIn(date(2026, 10, 5), days)            # term1 尾：已取消
            self.assertIn(date(2026, 10, 12), days)              # term2 头：照常
            self.assertEqual(days[date(2026, 10, 12)], bit(1, 1))
        finally:
            conn.close()

    def test_exception_narrower_than_days_is_noop(self):
        """例外指向的日期不在 days 里（周末/假期/term 外）→ 无操作、不报错。

        老师可以预先取消一节还没排的课；这不是数据缺陷，不该抛错。
        """
        exc = DayExceptions(cancels={("sch_a", "2026-09-26")})
        days = {date(2026, 9, 21): bit(1, 1)}
        out = apply_schedule_exceptions(days, "sch_a", exc)
        self.assertEqual(out, days)

    def test_empty_exceptions_fast_path(self):
        """没有任何 schedule 级例外时原样返回（省一次遍历）。"""
        days = {WED: bit(3, 1)}
        self.assertIs(apply_schedule_exceptions(days, "sch_a", DayExceptions()), days)

    def test_move_to_disabled_slot_records_issue(self):
        """🔴 改到当天未启用槽位 → 记 move_no_slot 信号，且当天保留（不静默丢掉）。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            # 把周三第 5 节设为未启用（模拟午休/当日不用的节次）
            conn.execute(
                "UPDATE period_slots SET enabled = 0"
                " WHERE org_id = ? AND day_of_week = 3 AND idx = 5",
                (org,),
            )
            conn.commit()
            from scheduler.slots import load_slot_config

            cfg = load_slot_config(conn, org)
            obj = og.create_object(conn, org, "group", "高一1班")
            _add_periodic(conn, org, "sch_a", "周三课", bit(3, 1), obj_id=obj)
            _ins_exc(conn, "se_m", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 5)))
            issues: list = []
            entries = build_occupancy(
                conn, org, MONDAY, date(2026, 9, 27), cfg=cfg, issues=issues
            )
            codes = [i.code for i in issues]
            self.assertIn(ISSUE_MOVE_NO_SLOT, codes)
            iss = next(i for i in issues if i.code == ISSUE_MOVE_NO_SLOT)
            self.assertEqual(iss.dates, ["2026-09-23"])
            self.assertEqual(iss.count, 1)
            self.assertEqual(iss.schedule_id, "sch_a")
            # 当天条目仍在（掩码带着那个未启用位）——不是静默消失
            masks = [e.mask for e in entries if e.schedule_id == "sch_a"]
            self.assertIn(bit(3, 5), masks)
        finally:
            conn.close()

    def test_move_inside_enabled_slots_no_issue(self):
        """正常改点不得误报 move_no_slot（防信号泛滥成狼来了）。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            obj = og.create_object(conn, org, "group", "高一1班")
            _add_periodic(conn, org, "sch_a", "周三课", bit(3, 1), obj_id=obj)
            _ins_exc(conn, "se_m", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 3)))
            issues: list = []
            build_occupancy(conn, org, MONDAY, date(2026, 9, 27), cfg=cfg, issues=issues)
            self.assertEqual([i.code for i in issues], [])
        finally:
            conn.close()


# ===========================================================================
# 四、build_occupancy 端到端：冲突立即消失 + DELETE 完全还原
# ===========================================================================


class TestOccupancyEndToEnd(unittest.TestCase):
    """验收主线：cancel → 冲突消失；删例外 → 完全还原。"""

    def _two_conflicting(self, conn, org):
        obj = og.create_object(conn, org, "group", "高一1班")
        _add_periodic(conn, org, "sch_a", "数学", bit(3, 1), obj_id=obj)
        _add_periodic(conn, org, "sch_b", "英语", bit(3, 1), obj_id=obj)
        return obj

    def test_cancel_makes_conflict_disappear_then_restore(self):
        """🔴 验收主线：有冲突 → cancel 后 0 冲突 → 删例外后冲突逐字还原。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            self._two_conflicting(conn, org)
            before = _conflict_dates(conn, org, cfg)
            self.assertEqual(len(before), 1)
            self.assertEqual(before[0][0], WED)

            _ins_exc(conn, "se_c", org, "2026-09-23", "cancel", target_id="sch_a")
            self.assertEqual(_conflict_dates(conn, org, cfg), [])

            # DELETE 即撤销：完全还原（不是"差不多还原"）
            conn.execute("DELETE FROM schedule_exceptions WHERE id = 'se_c'")
            conn.commit()
            self.assertEqual(_conflict_dates(conn, org, cfg), before)
        finally:
            conn.close()

    def test_cancel_only_that_week_others_keep_conflict(self):
        """取消本周三，下周三的冲突必须还在（否则就是误伤）。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            self._two_conflicting(conn, org)
            _ins_exc(conn, "se_c", org, "2026-09-23", "cancel", target_id="sch_a")
            got = _conflict_dates(conn, org, cfg, WED_NEXT, date(2026, 10, 3))
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0][0], WED_NEXT)
        finally:
            conn.close()

    def test_move_resolves_conflict_to_new_slot(self):
        """改点到不撞的节次 → 当天冲突消失，且新课表落在新节次上。

        🔴 只断言**改点那天**没了冲突：下周三两条课仍各占第 1 节，冲突必须
        还在。写成「两周都没冲突」是错的——那意味着改点泄漏到了其他周，
        恰是 Q2 要防的事故。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            self._two_conflicting(conn, org)
            _ins_exc(conn, "se_m", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 4)))
            # 本周三：改点生效 → 无冲突
            self.assertEqual(
                _conflict_dates(conn, org, cfg, MONDAY, date(2026, 9, 27)), []
            )
            # 下周三：未改 → 冲突必须还在
            self.assertEqual(
                _conflict_dates(conn, org, cfg, WED_NEXT, date(2026, 10, 3)),
                [(WED_NEXT, "sch_a", "sch_b")],
            )
            entries = build_occupancy(conn, org, MONDAY, date(2026, 10, 3), cfg=cfg)
            masks = {e.date: e.mask for e in entries if e.schedule_id == "sch_a"}
            self.assertEqual(masks[WED], bit(3, 4))       # 改到第 4 节
            self.assertEqual(masks[WED_NEXT], bit(3, 1))  # 下周不动
        finally:
            conn.close()

    def test_move_into_conflict_still_conflicts(self):
        """改点改成仍与另一条课相撞 → 冲突必须还在（不许悄悄吞掉）。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            obj = og.create_object(conn, org, "group", "高一1班")
            _add_periodic(conn, org, "sch_a", "数学", bit(3, 1), obj_id=obj)
            _add_periodic(conn, org, "sch_b", "英语", bit(3, 1) | bit(3, 2), obj_id=obj)
            _ins_exc(conn, "se_m", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 2)))
            got = _conflict_dates(conn, org, cfg)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0][0], WED)
        finally:
            conn.close()

    def test_scan_issues_surface_move_no_slot(self):
        """scan() 也要把 move_no_slot 上抛（P0-3 信号链完整）。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            conn.execute(
                "UPDATE period_slots SET enabled = 0"
                " WHERE org_id = ? AND day_of_week = 3 AND idx = 5",
                (org,),
            )
            conn.commit()
            from scheduler.slots import load_slot_config

            # 🔴 必须重新加载 cfg：build_occupancy/scan 都用传入的 cfg 判
            # 「当天有哪些启用槽位」，用改之前的旧 cfg 会漏报 move_no_slot。
            cfg = load_slot_config(conn, org)
            obj = og.create_object(conn, org, "group", "高一1班")
            _add_periodic(conn, org, "sch_a", "周三课", bit(3, 1), obj_id=obj)
            _ins_exc(conn, "se_m", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 5)))
            report = scan(conn, org, MONDAY, date(2026, 9, 27), cfg=cfg)
            codes = [i.code for i in report.issues]
            self.assertIn(ISSUE_MOVE_NO_SLOT, codes)
        finally:
            conn.close()

    def test_once_schedule_also_honours_exception(self):
        """领域层对一次性活动同样应用例外（接口层才按卡面只放行周期日程）。

        这样即便将来自DB直接写入或换入口，行为也不会分叉。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            obj = og.create_object(conn, org, "group", "高一1班")
            # 08:10~08:40 完整落在第 1 节（默认 8:00 起、45 分钟一节）
            _add_once(conn, org, "sch_o", "讲座", "2026-09-23T08:10",
                      "2026-09-23T08:40", obj_id=obj)
            _add_periodic(conn, org, "sch_a", "数学", bit(3, 1), obj_id=obj)
            self.assertEqual(len(_conflict_dates(conn, org, cfg)), 1)
            _ins_exc(conn, "se_c", org, "2026-09-23", "cancel", target_id="sch_o")
            self.assertEqual(_conflict_dates(conn, org, cfg), [])
        finally:
            conn.close()

    def test_m2_schedule_masks_respects_cancel(self):
        """🔴 消解器盲区修复：cancel 后 schedule_masks 必须返回空。

        修前本函数直接调 expand_periodic，绕过例外应用 → 消解器对一个
        已经不存在的冲突提案「把这条日程挪走」，用户看到「改了却没消失、
        还让我再改一次」——正是本项目要消灭的缺陷。
        """
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            self._two_conflicting(conn, org)
            before = m2.schedule_masks(conn, "sch_a", cfg, [WED])
            self.assertEqual(before.get(WED), bit(3, 1))
            _ins_exc(conn, "se_c", org, "2026-09-23", "cancel", target_id="sch_a")
            after = m2.schedule_masks(conn, "sch_a", cfg, [WED])
            self.assertEqual(after, {})
            # 下周不受影响
            self.assertEqual(
                m2.schedule_masks(conn, "sch_a", cfg, [WED_NEXT]).get(WED_NEXT),
                bit(3, 1),
            )
        finally:
            conn.close()

    def test_m2_schedule_masks_respects_move(self):
        """改点后 schedule_masks 必须返回新掩码（消解器按新位置找空位）。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            self._two_conflicting(conn, org)
            _ins_exc(conn, "se_m", org, "2026-09-23", "move",
                     target_id="sch_a", new_mask=mask_to_hex(bit(3, 4)))
            got = m2.schedule_masks(conn, "sch_a", cfg, [WED])
            self.assertEqual(got.get(WED), bit(3, 4))
        finally:
            conn.close()

    def test_build_occupancy_without_issues_arg_unchanged(self):
        """不传 issues（solver 旧调用点）= 主动放弃信号，行为与修前一致。"""
        conn = fresh()
        try:
            org, cfg = _setup(conn)
            self._two_conflicting(conn, org)
            _ins_exc(conn, "se_c", org, "2026-09-23", "cancel", target_id="sch_a")
            # 不传 issues 也不能炸
            self.assertEqual(_conflict_dates(conn, org, cfg), [])
        finally:
            conn.close()


# ===========================================================================
# 五、new_mask 校验链（Q1：全部 400，绝不静默兜底）
# ===========================================================================


class TestParseApiMask(unittest.TestCase):
    """Q1 定论的四条校验链 + 顺序。"""

    def setUp(self):
        self.conn = fresh()
        self.org, self.cfg = _setup(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_accepts_prefixed_and_unprefixed_hex(self):
        self.assertEqual(K.parse_api_mask("0x000002", 3, self.cfg), bit(3, 1))
        self.assertEqual(K.parse_api_mask("000002", 3, self.cfg), bit(3, 1))
        self.assertEqual(K.parse_api_mask("0X2", 3, self.cfg), bit(3, 1))

    def test_rejects_non_string(self):
        for bad in (None, 2, 0.5, [], {}, True):
            with self.assertRaises(Exception):
                K.parse_api_mask(bad, 3, self.cfg)

    def test_rejects_empty_and_zero(self):
        with self.assertRaises(Exception) as cm:
            K.parse_api_mask("", 3, self.cfg)
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(Exception) as cm:
            K.parse_api_mask("0x0", 3, self.cfg)
        self.assertIn("cancel", cm.exception.message)

    def test_rejects_bit_zero(self):
        """位 0 无意义（节次从 1 开始）——收了会把「第0节」写进掩码。"""
        with self.assertRaises(Exception) as cm:
            K.parse_api_mask("0x1", 3, self.cfg)
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("位 0", cm.exception.message)

    def test_rejects_beyond_16_periods(self):
        with self.assertRaises(Exception) as cm:
            K.parse_api_mask("0x20000", 3, self.cfg)  # 位 17
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("宽度", cm.exception.message)

    def test_rejects_unconfigured_period(self):
        """第 11 节没配置（默认只建 10 节）→ 400，不是悄悄截掉。"""
        with self.assertRaises(Exception) as cm:
            K.parse_api_mask("0x0800", 3, self.cfg)  # 位 11
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("未配置", cm.exception.message)

    def test_rejects_disabled_slot(self):
        """已配置但当天未启用（午休）→ 400：改过去这节课会直接不可见。"""
        self.conn.execute(
            "UPDATE period_slots SET enabled = 0"
            " WHERE org_id = ? AND day_of_week = 3 AND idx = 5",
            (self.org,),
        )
        self.conn.commit()
        cfg = self.conn.execute("SELECT 1").fetchone() and self.cfg
        from scheduler.slots import load_slot_config

        cfg = load_slot_config(self.conn, self.org)
        with self.assertRaises(Exception) as cm:
            K.parse_api_mask("0x0020", 3, cfg)  # 位 5
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("未启用", cm.exception.message)

    def test_rejects_saturday(self):
        """周六默认 enabled=0 → 任何非零掩码都被拒（周末不排课）。"""
        with self.assertRaises(Exception) as cm:
            K.parse_api_mask("0x000002", 6, self.cfg)
        self.assertEqual(cm.exception.status, 400)

    def test_accepts_multi_period_move(self):
        """一次改两节是合法的（连堂）。"""
        self.assertEqual(
            K.parse_api_mask("0x000006", 3, self.cfg), bit(3, 1) | bit(3, 2)
        )


class TestParseDate(unittest.TestCase):
    """date 严格 ISO：前端拼错参数必须查得出来。"""

    def test_accepts_iso(self):
        self.assertEqual(K.parse_date("2026-09-23"), "2026-09-23")
        self.assertEqual(K.parse_date(" 2026-09-23 "), "2026-09-23")

    def test_rejects_compact_form(self):
        """🔴 不用 fromisoformat 的宽松解析（3.11+ 接受 '20260907'）。"""
        with self.assertRaises(Exception) as cm:
            K.parse_date("20260923")
        self.assertEqual(cm.exception.status, 400)

    def test_rejects_nonexistent_date(self):
        with self.assertRaises(Exception) as cm:
            K.parse_date("2026-02-30")
        self.assertEqual(cm.exception.status, 400)


# ===========================================================================
# 六、接口层（直接调 handler，token 用真实签发链路）
# ===========================================================================


def _build_api_db(db_path):
    """可控小组织：一个行政组 + 一个班 + 一个任课教师 + 两条相撞的周期课。"""
    conn = connect(db_path)
    init_db(conn)
    org_id, root_id = og.create_organization(conn, "测试校")
    grade = og.create_object(conn, org_id, "group", "高一")
    cls = og.create_object(conn, org_id, "group", "高一1班")
    teacher_obj = og.create_object(conn, org_id, "user", "教师甲")
    og.add_edge(conn, root_id, grade)
    og.add_edge(conn, grade, cls)
    uid = og.create_user(conn, teacher_obj, "teacher_a", "pw", role="teacher")
    authz.grant_role(conn, org_id, cls, uid, "propose")
    cfg = setup_default_slots(conn, org_id)
    create_term(conn, org_id, "2026秋", "2026-09-21", "2026-12-31")
    _add_periodic(conn, org_id, "sch_a", "数学", bit(3, 1), obj_id=cls)
    _add_periodic(conn, org_id, "sch_b", "英语", bit(3, 1), obj_id=cls)
    # 🔴 一次性活动不放进共用 fixture：它和 sch_b 也撞，会让「冲突数」类
    # 断言从 1 变 3。只有 test_once_schedule_400 需要它，在那里单独建。
    conn.commit()
    return conn, org_id, cls, uid, cfg


def _body(**kw):
    return json.dumps(kw).encode("utf-8")


class ApiFixture(unittest.TestCase):
    """接口层共用基建：真实 token + 真实 handler 调用。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b9_api_")
        cls.db_path = os.path.join(cls.tmp, "b9.db")
        (cls.conn, cls.org, cls.cls, cls.uid, cls.cfg) = _build_api_db(cls.db_path)
        cls.cfg_srv = ServerConfig(
            host="127.0.0.1", port=0, db_path=cls.db_path,
            today=date(2026, 9, 22), seed_if_missing=False,
        )
        config.set_current_config(cls.cfg_srv)
        cls.token = auth.issue_token(cls.uid, "teacher_a", "teacher", "obj_x")
        # 🔴 必须小写 "authorization"：auth.resolve_context 查的就是小写键
        # （http.client 发请求时会归一化大小写，直调 handler 不会）。
        cls.headers = {"authorization": f"Bearer {cls.token}"}

    @classmethod
    def tearDownClass(cls):
        config.set_current_config(None)
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 每个用例从干净状态开始：清掉全部例外行（不只是本文件写的——
        # 接口层写进来的 note 是 NULL，按 note 过滤会漏）。
        self.conn.execute("DELETE FROM schedule_exceptions")
        self.conn.commit()

    def _post(self, **kw):
        return API.create_exception(
            "POST", "/api/exceptions", {}, _body(**kw), self.headers,
            context={"user_id": self.uid, "login": "teacher_a", "role": "teacher"},
        )

    def _delete(self, exc_id):
        return API.revoke_exception(
            "DELETE", "/api/exceptions", {"id": [exc_id]}, b"", self.headers,
            context={"user_id": self.uid, "login": "teacher_a", "role": "teacher"},
        )

    def _get(self, schedule_id):
        return API.list_exceptions(
            "GET", "/api/exceptions", {"schedule_id": [schedule_id]}, b"", self.headers,
            context={"user_id": self.uid, "login": "teacher_a", "role": "teacher"},
        )


class TestApiAuth(ApiFixture):
    """401 / 403：鉴权与组织隔离。"""

    def test_unauthenticated_401(self):
        """不带 Authorization → 401（require_auth 拦在最外层）。"""
        status, payload = API.create_exception(
            "POST", "/api/exceptions", {},
            _body(schedule_id="sch_a", date="2026-09-23", mode="cancel"),
            {}, context=None,
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_bad_token_401(self):
        status, _ = API.create_exception(
            "POST", "/api/exceptions", {},
            _body(schedule_id="sch_a", date="2026-09-23", mode="cancel"),
            {"authorization": "Bearer not-a-real-token"}, context=None,
        )
        self.assertEqual(status, 401)

    def test_delete_unauthenticated_401(self):
        status, _ = API.revoke_exception(
            "DELETE", "/api/exceptions", {"id": ["exc_x"]}, b"", {}, context=None,
        )
        self.assertEqual(status, 401)


class TestApiCreate(ApiFixture):
    """POST 成功路径 + 入库口径。"""

    def test_cancel_ok(self):
        payload = self._post(schedule_id="sch_a", date="2026-09-23", mode="cancel")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["exception_id"].startswith("exc_"))
        self.assertEqual(payload["affected_dates"], ["2026-09-23"])
        self.assertEqual(payload["mode"], "cancel")
        self.assertTrue(payload["effective"])

    def test_cancel_makes_conflict_disappear(self):
        """🔴 接口层验收主线：POST 之后冲突真的少了。"""
        before = _conflict_dates(self.conn, self.org, self.cfg)
        self.assertEqual(len(before), 1)
        self._post(schedule_id="sch_a", date="2026-09-23", mode="cancel")
        # 重新开连接读（handler 自己 open/close，避免读到旧快照）
        conn = connect(self.db_path)
        try:
            cfg = setup_default_slots.__globals__["load_slot_config"](conn, self.org)
            after = _conflict_dates(conn, self.org, cfg)
        finally:
            conn.close()
        self.assertEqual(after, [])

    def test_move_ok_and_stored_as_week_mask_hex(self):
        """🔴 Q3：DB 里必须是周掩码裸 hex，不是 API 的单日掩码串。"""
        payload = self._post(
            schedule_id="sch_a", date="2026-09-23", mode="move", new_mask="0x000008"
        )
        self.assertTrue(payload["ok"])
        row = self.conn.execute(
            "SELECT new_mask, scope, target_id, date, mode FROM schedule_exceptions"
            " WHERE id = ?", (payload["exception_id"],),
        ).fetchone()
        self.assertEqual(row["scope"], "schedule")
        self.assertEqual(row["target_id"], "sch_a")
        self.assertEqual(row["date"], "2026-09-23")
        self.assertEqual(row["mode"], "move")
        # 周三第 3 节 → 周掩码 0x400000000（位号 (3-1)*16 + (3-1) = 34）
        self.assertEqual(row["new_mask"], mask_to_hex(bit(3, 3)))
        self.assertEqual(row["new_mask"], "400000000")
        self.assertEqual(int(row["new_mask"], 16), 0x400000000)
        # 🔴 反向断言：绝不是 API 那个 "0x000008"
        self.assertNotEqual(row["new_mask"], "0x000008")

    def test_move_resolves_conflict(self):
        self._post(
            schedule_id="sch_a", date="2026-09-23", mode="move", new_mask="0x000010"
        )
        conn = connect(self.db_path)
        try:
            cfg = setup_default_slots.__globals__["load_slot_config"](conn, self.org)
            self.assertEqual(_conflict_dates(conn, self.org, cfg), [])
        finally:
            conn.close()

    def test_effective_false_for_saturday(self):
        """周六不排课 → 写入成功但 effective=False（如实回报，不假装生效）。"""
        payload = self._post(schedule_id="sch_a", date="2026-09-26", mode="cancel")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["effective"])

    def test_effective_false_for_same_mask_move(self):
        """改到的掩码与原掩码相同 → effective=False（写了但没改变任何东西）。"""
        payload = self._post(
            schedule_id="sch_a", date="2026-09-23", mode="move", new_mask="0x000002"
        )
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["effective"])

    def test_target_id_alias_accepted(self):
        """docs/12 的示例用过 target_id——兼容接收。"""
        payload = self._post(target_id="sch_a", date="2026-09-23", mode="cancel")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schedule_id"], "sch_a")

    def test_conflicting_field_names_400(self):
        """两个字段都给且不一致 → 明确 400，不静默选一个。

        ApiError 由 http_api 的分发层转成 (status, body)；直调 handler 时
        以异常形式抛出（与 B3/B4 同一条约定）。
        """
        with self.assertRaises(Exception) as cm:
            API.create_exception(
                "POST", "/api/exceptions", {},
                _body(schedule_id="sch_a", target_id="sch_b",
                      date="2026-09-23", mode="cancel"),
                self.headers, context={"user_id": self.uid},
            )
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("不一致", cm.exception.message)


class TestApiValidation(ApiFixture):
    """400 / 404 / 409：参数与状态校验。"""

    def test_missing_schedule_id_400(self):
        with self.assertRaises(Exception) as cm:
            self._post(date="2026-09-23", mode="cancel")
        self.assertEqual(cm.exception.status, 400)

    def test_missing_date_400(self):
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_a", mode="cancel")
        self.assertEqual(cm.exception.status, 400)

    def test_bad_mode_400(self):
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_a", date="2026-09-23", mode="special")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("special", cm.exception.message)

    def test_move_without_mask_400(self):
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_a", date="2026-09-23", mode="move")
        self.assertEqual(cm.exception.status, 400)

    def test_cancel_with_mask_400(self):
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_a", date="2026-09-23", mode="cancel",
                       new_mask="0x000008")
        self.assertEqual(cm.exception.status, 400)

    def test_move_to_lunch_slot_400(self):
        """改到未启用节次 → 400（P0-3：不许制造假阴性）。"""
        self.conn.execute(
            "UPDATE period_slots SET enabled = 0"
            " WHERE org_id = ? AND day_of_week = 3 AND idx = 5",
            (self.org,),
        )
        self.conn.commit()
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_a", date="2026-09-23", mode="move",
                       new_mask="0x0020")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("未启用", cm.exception.message)

    def test_move_to_unconfigured_period_400(self):
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_a", date="2026-09-23", mode="move",
                       new_mask="0x0800")  # 第 11 节，未配置
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("未配置", cm.exception.message)

    def test_bad_date_format_400(self):
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_a", date="20260923", mode="cancel")
        self.assertEqual(cm.exception.status, 400)

    def test_once_schedule_400(self):
        """卡面只放行周期日程；一次性活动请改活动本身。"""
        # 🔴 expand_once 的格式是 "%Y-%m-%dT%H:%M"（无秒），带 ":00" 会
        # ValueError: unconverted data remains（实测踩过，勿改回）。
        _add_once(self.conn, self.org, "sch_o", "讲座", "2026-09-23T08:10",
                  "2026-09-23T08:40", obj_id=self.cls)
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_o", date="2026-09-23", mode="cancel")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("周期日程", cm.exception.message)

    def test_unknown_schedule_404(self):
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_nope", date="2026-09-23", mode="cancel")
        self.assertEqual(cm.exception.status, 404)

    def test_duplicate_409(self):
        """🔴 同 (日程, 日期) 已有例外 → 409，且报出已有 id（不拦会静默失效/500）。"""
        first = self._post(schedule_id="sch_a", date="2026-09-23", mode="cancel")
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_a", date="2026-09-23", mode="move",
                       new_mask="0x000008")
        self.assertEqual(cm.exception.status, 409)
        self.assertIn(first["exception_id"], cm.exception.message)

    def test_duplicate_same_key_different_schedule_ok(self):
        """不同日程同一天可以各有例外（key 含 schedule_id）。"""
        self._post(schedule_id="sch_a", date="2026-09-23", mode="cancel")
        payload = self._post(schedule_id="sch_b", date="2026-09-23", mode="cancel")
        self.assertTrue(payload["ok"])


class TestApiOrgIsolation(ApiFixture):
    """跨组织：404（不是 400——不泄露 id 是否存在）。"""

    def test_cross_org_schedule_404(self):
        other_org, _root = og.create_organization(self.conn, "乙校")
        _add_periodic(self.conn, other_org, "sch_evil_a", "乙校数学", bit(3, 1))
        with self.assertRaises(Exception) as cm:
            self._post(schedule_id="sch_evil_a", date="2026-09-23", mode="cancel")
        self.assertEqual(cm.exception.status, 404)

    def test_cross_org_exception_rows_invisible(self):
        """乙校的例外行不得出现在甲校教师的 GET 结果里。"""
        other_org, _root = og.create_organization(self.conn, "乙校")
        _add_periodic(self.conn, other_org, "sch_evil_b", "乙校数学", bit(3, 1))
        _ins_exc(self.conn, "se_evil_b", other_org, "2026-09-23", "cancel",
                 target_id="sch_evil_b")
        payload = self._get("sch_a")
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["items"], [])


class TestApiDelete(ApiFixture):
    """DELETE 即撤销（改错了必须能回退）。"""

    def test_delete_restores_conflict(self):
        """🔴 撤销后冲突逐字还原——教师不敢用的根因就是「改了回不去」。"""
        before = _conflict_dates(self.conn, self.org, self.cfg)
        created = self._post(schedule_id="sch_a", date="2026-09-23", mode="cancel")
        payload = self._delete(created["exception_id"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["exception_id"], created["exception_id"])
        self.assertEqual(payload["affected_dates"], ["2026-09-23"])
        self.assertEqual(_conflict_dates(self.conn, self.org, self.cfg), before)

    def test_delete_unknown_404(self):
        with self.assertRaises(Exception) as cm:
            self._delete("exc_nope")
        self.assertEqual(cm.exception.status, 404)

    def test_delete_missing_id_400(self):
        with self.assertRaises(Exception) as cm:
            self._delete("")
        self.assertEqual(cm.exception.status, 400)

    def test_delete_cross_org_404(self):
        """撤销他组织的例外 → 404（组织隔离，同样不泄露存在性）。"""
        other_org, _root = og.create_organization(self.conn, "乙校")
        _add_periodic(self.conn, other_org, "sch_evil_c", "乙校数学", bit(3, 1))
        _ins_exc(self.conn, "se_evil_c", other_org, "2026-09-23", "cancel",
                 target_id="sch_evil_c")
        with self.assertRaises(Exception) as cm:
            self._delete("se_evil_c")
        self.assertEqual(cm.exception.status, 404)
        # 数据必须还在（没被误删）
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM schedule_exceptions WHERE id = 'se_evil_c'"
        ).fetchone()
        self.assertEqual(row["c"], 1)


class TestApiList(ApiFixture):
    """GET：让前端能标出「这天有临时取消/改点」并敢点撤销。"""

    def test_list_empty(self):
        payload = self._get("sch_a")
        self.assertEqual(payload, {"count": 0, "items": []})

    def test_list_returns_readable(self):
        """🔴 readable 必须给：撤销按钮上不能只显示一串 hex。"""
        self._post(schedule_id="sch_a", date="2026-09-23", mode="move",
                   new_mask="0x000008")
        payload = self._get("sch_a")
        self.assertEqual(payload["count"], 1)
        item = payload["items"][0]
        self.assertEqual(item["mode"], "move")
        self.assertEqual(item["date"], "2026-09-23")
        self.assertEqual(item["schedule_id"], "sch_a")
        self.assertEqual(item["title"], "数学")
        # 出参是单日掩码（不是入库的周掩码）
        self.assertEqual(item["new_mask"], "0x000008")
        self.assertEqual(item["readable"], [{"dow": 3, "period": 3}])

    def test_list_cancel_has_no_mask(self):
        self._post(schedule_id="sch_a", date="2026-09-23", mode="cancel")
        item = self._get("sch_a")["items"][0]
        self.assertIsNone(item["new_mask"])
        self.assertIsNone(item["readable"])

    def test_list_two_calls_identical(self):
        """两次调用逐字一致（uuid4 不定序会破坏这条硬要求）。"""
        self._post(schedule_id="sch_a", date="2026-09-23", mode="cancel")
        self._post(schedule_id="sch_a", date="2026-09-30", mode="cancel")
        self.assertEqual(self._get("sch_a"), self._get("sch_a"))

    def test_list_sorted_by_date(self):
        self._post(schedule_id="sch_a", date="2026-09-30", mode="cancel")
        self._post(schedule_id="sch_a", date="2026-09-23", mode="cancel")
        items = self._get("sch_a")["items"]
        self.assertEqual([i["date"] for i in items], ["2026-09-23", "2026-09-30"])

    def test_list_unknown_schedule_404(self):
        with self.assertRaises(Exception) as cm:
            self._get("sch_nope")
        self.assertEqual(cm.exception.status, 404)

    def test_list_missing_param_400(self):
        with self.assertRaises(Exception) as cm:
            API.list_exceptions(
                "GET", "/api/exceptions", {}, b"", self.headers,
                context={"user_id": self.uid},
            )
        self.assertEqual(cm.exception.status, 400)


class TestRoutesRegistration(unittest.TestCase):
    """约定式路由扫描必须看见三个方法（少一个启动即 RuntimeError）。"""

    def test_routes_non_empty_and_complete(self):
        self.assertEqual(
            sorted(API.ROUTES),
            [
                ("DELETE", "/api/exceptions"),
                ("GET", "/api/exceptions"),
                ("POST", "/api/exceptions"),
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

    def test_doctor_expected_routes_include_exceptions(self):
        """doctor 的 EXPECTED_ROUTES 必须登记三条（同 ASSET_VERSION 同步约定）。"""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        try:
            import doctor
        finally:
            sys.path.pop()
        for key in API.ROUTES:
            self.assertIn(key, doctor.EXPECTED_ROUTES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
