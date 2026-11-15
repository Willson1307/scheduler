"""新建日程（POST /api/schedules + 周视图「+ 新建日程」）：静态约定与回归断言。

为什么要有：
  1. 这个功能由三处拼成（index.html 按钮、app.js 弹层、api_schedules 路由），
     少一处就表现为「点了没反应」——静默失效，本项目最痛的形态。
  2. 2026-11-15 实测踩到一个**静默错位**：一次性活动的时间字段写成
     `start`/`end`（真实字段是 `start_time`/`end_time`），取到 None 后
     **回落成 08:00**，于是「第 5 节的活动」被建到第 1 节，界面还显示创建成功。
     这里用断言把字段名钉死，防止再犯。

⛔ 只做静态断言 + 纯函数级检查：不打网络、不动数据库，断网也能跑。
"""

from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestCreateEntryDom(unittest.TestCase):
    def test_button_in_week_section(self):
        html = _read("web/index.html")
        start = html.index('id="secWeek"')
        end = html.index("</section>", start)
        block = html[start:end]
        self.assertIn('id="newBtn"', block, "「+ 新建日程」必须挂在周视图区块里")

    def test_button_not_in_chat_sheet(self):
        html = _read("web/index.html")
        idx = html.index('id="chatSheet"')
        tail = html[idx:]
        self.assertNotIn('id="newBtn"', tail, "新建入口不能塞进问一下抽屉")


class TestCreateEntryJs(unittest.TestCase):
    def setUp(self):
        self.js = _read("web/app.js")

    def test_modal_function_and_binding(self):
        self.assertIn("function openNewScheduleModal(", self.js)
        self.assertRegex(
            self.js,
            r"newBtn\.addEventListener\('click',\s*function\s*\(\)\s*\{\s*openNewScheduleModal\(\);",
            "「+ 新建日程」按钮没有绑定弹层（点了不会有反应）",
        )

    def test_posts_to_schedules(self):
        self.assertIn("apiPost('/api/schedules'", self.js)

    def test_supports_both_kinds(self):
        # 活动和课表都要能建：弹层里必须两种类型都有
        self.assertIn("attrs: { value: 'once' }", self.js)
        self.assertIn("attrs: { value: 'periodic' }", self.js)

    def test_refreshes_after_create(self):
        # 建完必须刷新，否则周视图看不到新日程（会以为没建成）
        body = self.js[self.js.index("function openNewScheduleModal("):]
        body = body[:body.index("\n}\n")]
        self.assertIn("loadAll()", body)


class TestCreateRoute(unittest.TestCase):
    def setUp(self):
        self.src = _read("src/server/api_schedules.py")

    def test_route_registered(self):
        self.assertIn('("POST", "/api/schedules"): create_schedule', self.src)

    def test_uses_domain_create_functions(self):
        # ⛔ 不允许在外围自己拼 INSERT（掩码位序、参与者角色必须由领域层负责）
        self.assertIn("create_once(", self.src)
        self.assertIn("create_periodic(", self.src)
        self.assertNotIn('INSERT INTO schedules', self.src)

    def test_requires_own_level(self):
        self.assertIn('if level != "own":', self.src)

    def test_rejects_bad_input(self):
        for frag in ('kind 只能是 periodic 或 once',
                     '缺少 title',
                     'date 必须是 YYYY-MM-DD',
                     'periods 至少要有一节',
                     'targets 至少要有一个对象'):
            self.assertIn(frag, self.src, "缺少校验：%s" % frag)

    def test_time_fields_are_start_time_end_time(self):
        """🔴 回归断言：写成 start/end 会静默回落到 08:00，课落到第 1 节。"""
        self.assertIn('first.get("start_time")', self.src)
        self.assertIn('last.get("end_time")', self.src)
        self.assertNotIn('first.get("start")', self.src)
        self.assertNotIn('last.get("end")', self.src)


if __name__ == "__main__":
    unittest.main()
