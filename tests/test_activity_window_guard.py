"""一次性活动窗口哨兵（2026-10-27，ACTIVITY-WINDOW-GUARD）。

锁住的语义（对应 `src/server/api_conflicts.py::activity_window_advisory`）：
    /api/conflicts 除了计算窗口内的冲突，还必须回答一个问题——
    **「本组织的一次性活动，到底落不落在这次扫描的窗口里？」**

为什么这条断言值钱（实测，勿重新论证）：
    同一份试用库：
      --today 2026-10-14 → raw_count=30
      --today 2026-11-11 → raw_count=3      ← 主场景「活动撞课表」整体消失
    接口 200、页面正常、零报错。演示库同样：demo.db 裸起在 2026-11-16 只有 1 处
    冲突（带 --demo 才是 23 处）。守着这个的**当时只有测试与部署脚本**，
    `src/` 里没有任何一条会在老师/评委打开页面时提醒「活动已过期」。

本测试覆盖的 6 种情形（互相对照，防假绿）：
    ① 活动在窗口内            → gap_days=0，advisory=None（不误报）
    ② 活动全在窗口之前（过期） → gap_days>0，advisory 写明「早于」+ 实际天数
    ③ 活动全在窗口之后（锚未来）→ advisory 写明「晚于」
    ④ 差 1~7 天               → 不告警（窗口边缘的正常抖动，不制造噪音）
    ⑤ 边界：活动正好压在窗口首/末日 → 不告警
    ⑥ 库里一个一次性活动都没有 → has_once=False，advisory 说明场景缺失

⛔ 破坏法（AGENTS §4.3，本文件必须能判红）：
    把 `if gap > ACTIVITY_GAP_WARN_DAYS:` 改成 `if False:` → ②③ 必红；
    把 `if count == 0` 分支的 advisory 置 None → ⑥ 必红。

只用 Python 标准库；库建在 tempfile，测完删除，不碰 data/。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(REPO, "src"), os.path.join(REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scheduler.db import connect  # noqa: E402
from scheduler.schedules import NewSchedule, create_once  # noqa: E402
from scheduler.seeds import seed_demo  # noqa: E402
from server.api_conflicts import ACTIVITY_GAP_WARN_DAYS, activity_window_advisory  # noqa: E402

#: 学期起点（周一），与试用口径一致。
TERM_START = "2026-11-09"
#: 试用首周窗口（resolve_window 的口径：today 所在周的周一~周日）。
WEEK_1 = {"from": "2026-11-09", "to": "2026-11-15", "today": "2026-11-11"}
#: 现场评选那周的窗口（2026-11-16 是周一）。
WEEK_ON_SITE = {"from": "2026-11-16", "to": "2026-11-22", "today": "2026-11-16"}


class ActivityWindowGuardTest(unittest.TestCase):
    _tmp: str
    db: str

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="activity-window-")
        cls.db = os.path.join(cls._tmp, "g.db")
        conn = connect(cls.db)
        try:
            info = seed_demo(
                conn,
                classes=1,
                students_per_class=2,
                teachers=3,
                term_start=TERM_START,
                with_conflict=False,
            )
            cls.org_id = info["org_id"]
            cls.teacher_id = info["teacher_ids"][0]
            # 种子可能自带一次性活动 → 清空，让每个用例自己造，互不串味。
            # 先删扩展表再删主表行，避免留下孤儿（孤儿另有 warnings 机制管）。
            conn.execute("DELETE FROM schedule_once")
            conn.execute("DELETE FROM schedules WHERE kind = 'once'")
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # -- 工具 -------------------------------------------------------------

    def _add_activity(self, day: str) -> str:
        """在 day 那天造一条一次性活动，返回 schedule_id。"""
        conn = connect(self.db)
        try:
            sid = create_once(
                conn,
                NewSchedule(
                    title="示例活动",
                    org_id=self.org_id,
                    created_by=None,
                    participants={"teacher": [self.teacher_id]},
                ),
                start_at=f"{day}T08:00",
                end_at=f"{day}T09:00",
            )
            return sid
        finally:
            conn.close()

    def _clear_activities(self) -> None:
        conn = connect(self.db)
        try:
            conn.execute("DELETE FROM schedule_once")
            conn.execute("DELETE FROM schedules WHERE kind = 'once'")
            conn.commit()
        finally:
            conn.close()

    def _advisory(self, window: dict) -> dict:
        conn = connect(self.db)
        try:
            return activity_window_advisory(conn, self.org_id, window)
        finally:
            conn.close()

    # -- 用例 -------------------------------------------------------------

    def test_01_activity_inside_window_is_silent(self):
        """① 活动在窗口内 → 不告警（防止「改成永远告警」的假修复）。"""
        self._clear_activities()
        self._add_activity("2026-11-11")
        r = self._advisory(WEEK_1)
        self.assertTrue(r["has_once"])
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["earliest"], "2026-11-11")
        self.assertEqual(r["gap_days"], 0)
        self.assertIsNone(r["advisory"], f"活动就在窗口内却告警了：{r['advisory']}")

    def test_02_expired_activity_warns(self):
        """② 活动已过期（旧学期锚点）→ 必须告警，且写明「早于」与真实天数。"""
        self._clear_activities()
        self._add_activity("2026-10-14")
        r = self._advisory(WEEK_ON_SITE)
        self.assertEqual(r["gap_days"], 33)  # 10-14 → 11-16
        self.assertIsNotNone(r["advisory"], "活动已过期却没有任何告警——哨兵失效比没有哨兵更危险")
        self.assertIn("早于", r["advisory"])
        self.assertIn("33 天", r["advisory"])
        # 告警必须能指导下一步动作，不能只说「有问题」
        self.assertIn("--term-start", r["advisory"])
        self.assertIn("--today", r["advisory"])

    def test_03_future_activity_warns(self):
        """③ 活动全在窗口之后（学期起点锚到未来）→ 告警写明「晚于」。"""
        self._clear_activities()
        self._add_activity("2026-12-01")
        r = self._advisory(WEEK_ON_SITE)
        self.assertEqual(r["gap_days"], 9)  # 11-22 → 12-01
        self.assertIsNotNone(r["advisory"])
        self.assertIn("晚于", r["advisory"])

    def test_04_near_miss_does_not_warn(self):
        """④ 只差 1 天（下周一的窗口看本周一晚的活动）→ 不制造噪音。"""
        self._clear_activities()
        self._add_activity("2026-11-23")
        r = self._advisory(WEEK_ON_SITE)
        self.assertEqual(r["gap_days"], 1)
        self.assertLessEqual(ACTIVITY_GAP_WARN_DAYS, 7)  # 阈值口径没被偷偷抬高
        self.assertIsNone(r["advisory"])

    def test_05_edges_of_window_do_not_warn(self):
        """⑤ 边界：活动正好压在窗口首日 / 末日 → 都算「在窗口内」。"""
        self._clear_activities()
        self._add_activity("2026-11-16")
        self.assertIsNone(self._advisory(WEEK_ON_SITE)["advisory"])
        self._clear_activities()
        self._add_activity("2026-11-22")
        self.assertIsNone(self._advisory(WEEK_ON_SITE)["advisory"])

    def test_06_no_activity_at_all(self):
        """⑥ 一条一次性活动都没有 → 如实说「场景缺失」，不许静默返回空。"""
        self._clear_activities()
        r = self._advisory(WEEK_1)
        self.assertFalse(r["has_once"])
        self.assertEqual(r["count"], 0)
        self.assertIsNone(r["earliest"])
        self.assertIsNotNone(r["advisory"])
        self.assertIn("没有任何一次性活动", r["advisory"])


if __name__ == "__main__":
    unittest.main()
