"""TRIAL-REANCHOR：一次性「示例活动」必须贴住试用日，过期 = 静默失效。

背景（2026-10-26 卡面实测，勿重新论证）：
    同一份试用库（term 锚 2026-10-12，活动全在 10-14）：
      today=2026-10-14 → /api/conflicts raw_count=30
      today=2026-11-11 → raw_count=3      ← 「活动撞课表」主场景整体消失，接口不报错
    教师试用 11-11~13。学期起点/活动日期不重锚，老师进来看到的就是空场景。

本测试的三条边界（卡面写死）：
    ① 不起服务：直接查库 + 调 scheduler.occupancy.scan，不起任何 HTTP 进程；
    ② 不写 data/：两份试用库都建在 tempfile 临时目录，测完整体删除；
    ③ 判红口径：库里最早/最晚的一次性活动日期与给定 today 相差 > 7 天即红，
      失败信息必须写出实际日期差与「活动已过期 → 冲突会是 0」。

为什么 7 天：/api/conflicts 的窗口 = today 所在周的周一~周日
（b3_kernels.resolve_window），窗口内任何日期与 today 的距离 ≤ 6 天；
7 天是「活动还落在本周窗口内」的宽松上界，超过它活动必然扫不到。

两个用例互为对照（防假绿）：
    fresh：term 锚 2026-11-09 → 判红器无告警 + scan ≥ 25（主场景在）；
    stale：term 锚 2026-10-12 → 判红器必须报出 28 天差 + scan < 10
           （红是真的红：主场景确实消失，只剩周期性冲突的零头）。

只用 Python 标准库。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(REPO, "scripts"), os.path.join(REPO, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_trial_org as bto  # noqa: E402  # 导入时自行把 src/ 挂进 sys.path
from scheduler.db import connect  # noqa: E402
from scheduler.occupancy import scan  # noqa: E402
from scheduler.slots import load_slot_config  # noqa: E402

#: 教师试用首日（卡面口径：11-11~13）。
TRIAL_DAY = date(2026, 11, 11)
#: 旧锚点：2026-10-26 起已过期（卡面病灶就是它）。
STALE_TERM = "2026-10-12"
#: 正确锚点：试用首日所在周的周一。
FRESH_TERM = "2026-11-09"
#: 活动日期与 today 的最大允许距离（天）。
MAX_WINDOW_DAYS = 7


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def activity_window_problem(conn, today: date) -> str | None:
    """一次性活动日期与 today 相差超限 → 返回问题描述；None = 正常。

    查的是 schedule_once（一次性活动的真实落点，start_at 形如
    'YYYY-MM-DDTHH:MM'）；周期课表不在此列——学期 20 周，周期冲突不会过期，
    会静默过期的只有一次性活动。
    """
    row = conn.execute(
        "SELECT MIN(start_at) AS lo, MAX(start_at) AS hi FROM schedule_once"
    ).fetchone()
    if not row or not row["lo"] or not row["hi"]:
        return (
            "库里没有一次性活动（schedule_once 为空）——「活动撞课表」场景缺失，"
            "/api/conflicts 只剩周期性冲突"
        )
    lo = date.fromisoformat(row["lo"][:10])
    hi = date.fromisoformat(row["hi"][:10])
    worst = max(abs((lo - today).days), abs((hi - today).days))
    if worst > MAX_WINDOW_DAYS:
        return (
            f"一次性活动日期 {lo.isoformat()} ~ {hi.isoformat()} 与 today "
            f"{today.isoformat()} 相差 {worst} 天（> {MAX_WINDOW_DAYS} 天）——"
            f"活动已过期 → 冲突会是 0（实测：term 10-12 时 today 11-11 的 "
            f"raw_count 30→3，接口不报错）。修法：python scripts/build_trial_org.py "
            f"--per-teacher --term-start {monday_of(today).isoformat()} 重建试用库"
        )
    return None


class TrialActivityWindowTest(unittest.TestCase):
    """两种锚点对照：过期必红、贴住必绿，红还要用 scan 证明不是空喊。"""

    stale_db: str
    fresh_db: str
    _tmp: str

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="trial-window-")
        # 测试专用占位口令：admin_password() 只在建库时写入 users 行，
        # 不出本进程、不入日志、与真实口令表无关（绕开「无口令表即 SystemExit」）。
        bto._ADMIN_PW_FROM_STDIN = "test-only-placeholder"
        cls.stale_db = os.path.join(cls._tmp, "stale.db")
        cls.fresh_db = os.path.join(cls._tmp, "fresh.db")
        bto.build(per_teacher=True, db_path=cls.stale_db, term_start=STALE_TERM)
        bto.build(per_teacher=True, db_path=cls.fresh_db, term_start=FRESH_TERM)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _t01_org(self, conn) -> str:
        row = conn.execute(
            "SELECT id FROM organizations WHERE name = '试用学校T01'"
        ).fetchone()
        self.assertIsNotNone(row, "库里缺「试用学校T01」组织（per-teacher 建库异常）")
        return row["id"]

    def _week_conflicts(self, db_path: str, today: date) -> int:
        """T01 组织在 today 所在周的原始冲突条数（scan().count，与 raw_count 同源）。"""
        conn = connect(db_path)
        try:
            org = self._t01_org(conn)
            cfg = load_slot_config(conn, org)
            d0 = monday_of(today)
            return scan(conn, org, d0, d0 + timedelta(days=6), cfg=cfg).count
        finally:
            conn.close()

    def test_fresh_anchor_green(self):
        """正确锚点：判红器无告警，且主场景真的在（≥ 25，基线 30）。"""
        conn = connect(self.fresh_db)
        try:
            problem = activity_window_problem(conn, TRIAL_DAY)
        finally:
            conn.close()
        self.assertIsNone(problem, problem)
        n = self._week_conflicts(self.fresh_db, TRIAL_DAY)
        self.assertGreaterEqual(
            n, 25,
            f"正确锚点（term {FRESH_TERM}）下 T01 试用首周冲突应 ≥ 25，实际 {n} "
            f"——活动虽在窗口内但冲突不足，主场景同样撑不起来",
        )

    def test_stale_anchor_red(self):
        """过期锚点：判红器必须报出实际日期差——这正是防静默失效的哨兵。"""
        conn = connect(self.stale_db)
        try:
            problem = activity_window_problem(conn, TRIAL_DAY)
        finally:
            conn.close()
        self.assertIsNotNone(
            problem,
            "活动已过期（term 2026-10-12，活动 10-14，today 11-11）却没报红——"
            "哨兵失效比没有哨兵更危险",
        )
        self.assertIn("28 天", problem, f"失败信息应写明实际日期差：{problem}")
        self.assertIn("活动已过期", problem)
        # 红是真的红：主场景确实消失，只剩周期性冲突的零头（实测 3）
        n = self._week_conflicts(self.stale_db, TRIAL_DAY)
        self.assertLess(
            n, 10,
            f"过期锚点下应只剩周期性冲突（< 10，实测基线 3），实际 {n}",
        )


if __name__ == "__main__":
    unittest.main()
