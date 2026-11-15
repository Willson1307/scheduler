"""一句话改课入口（NL-ENTRY）：静态约定 + 后端路由。

为什么要有这组断言：
  这个入口由**三处**拼起来（index.html 的 DOM、app.js 的 nlRun/bind、api_nl 的路由），
  任何一处漏掉，界面上表现为「输入后没反应」——典型的静默失效（不报错、只是不对）。
  所以这里把三处都钉住，少一处就红。

⛔ 只做静态断言，不打网络：能跑在断网机器上，也不会因为演示数据变化而红。
"""

from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestNlEntryDom(unittest.TestCase):
    """index.html：入口在周视图头部，且是**独立**的一套控件。"""

    def test_ids_present(self):
        html = _read("web/index.html")
        for dom_id in ("nlText", "nlBtn", "nlPanel", "nlRule", "nlCands", "nlErr"):
            self.assertIn('id="%s"' % dom_id, html, "缺少 #%s" % dom_id)

    def test_entry_is_inside_week_section(self):
        html = _read("web/index.html")
        start = html.index('id="secWeek"')
        end = html.index("</section>", start)
        block = html[start:end]
        self.assertIn('id="nlText"', block, "一句话入口必须在周视图区块里（不是问一下抽屉）")
        self.assertNotIn('id="chatQ"', block, "周视图区块里不该出现问一下的输入框：两个功能不能混")

    def test_not_in_chat_sheet(self):
        html = _read("web/index.html")
        sheet = html[html.index('id="chatSheet"'):]
        sheet = sheet[:sheet.index("</div>\n</body>") if "</div>\n</body>" in sheet else len(sheet)]
        self.assertNotIn('id="nlText"', sheet, "一句话入口不能塞进问一下抽屉")


class TestNlEntryJs(unittest.TestCase):
    """app.js：函数、绑定、来源标注、以及「不直接提交」的红线。"""

    def setUp(self):
        self.js = _read("web/app.js")

    def test_functions_exist(self):
        for fn in ("function nlRun(", "function renderNlCands(", "function nlPropose(",
                   "function addDaysIso("):
            self.assertIn(fn, self.js, "缺少 %s" % fn)

    def test_button_bound(self):
        self.assertRegex(
            self.js,
            r"nlBtn\.addEventListener\('click',\s*function\s*\(\)\s*\{\s*nlRun\(\);",
            "「解析并求解」按钮没有绑定 nlRun（点了不会有反应）",
        )
        self.assertRegex(
            self.js,
            r"nlText\.addEventListener\('keydown'[\s\S]{0,120}nlRun\(\);",
            "输入框回车没有触发 nlRun",
        )

    def test_calls_right_endpoints(self):
        self.assertIn("apiPost('/api/nl'", self.js)
        self.assertIn("apiGet('/api/slots'", self.js)
        self.assertIn("apiGet('/api/schedules/detail'", self.js)

    def test_source_is_labeled(self):
        # 🔴 本地规则解析不能标成「模型解析」，这是诚信要求
        self.assertIn("本地规则解析", self.js)
        self.assertIn("模型解析", self.js)

    def test_propose_goes_through_modal(self):
        # 候选点击 → 打开改期弹层，⛔ 不能直接提交
        self.assertIn("openProposeModal(targets", self.js)
        body = self.js[self.js.index("async function nlPropose("):]
        body = body[:body.index("\n}\n")]
        self.assertNotIn("/api/approvals", body, "nlPropose 不允许直接提交审批，只能打开弹层")


class TestNlRoute(unittest.TestCase):
    """后端：路由注册 + 本地解析函数可用。"""

    def test_route_registered(self):
        src = _read("src/server/api_nl.py")
        self.assertIn('("POST", "/api/nl"): nl', src)
        self.assertIn("ROUTES = {", src)

    def test_local_parser_exists(self):
        src = _read("src/server/b_nl_kernels.py")
        self.assertIn("def parse_local(", src)
        self.assertIn("SLOT_KEYS", src)


class TestNlLocalParser(unittest.TestCase):
    """本地解析器行为（纯函数，不碰库、不碰网络）。"""

    def setUp(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from server import b_nl_kernels as NL
        self.NL = NL
        self.names = ["高一(1)班", "高一(2)班", "教室01"]
        self.dates = {3: "2026-09-09", 4: "2026-09-10"}
        self.periods = {"2026-09-09": [1, 2, 3], "2026-09-10": [1, 2, 3]}

    def test_full_sentence(self):
        r = self.NL.parse_local("高一(1)班周三第1节的课想换个时间", self.names, self.dates, self.periods)
        self.assertIsNotNone(r)
        self.assertEqual(r["intent_id"], "reschedule")
        self.assertEqual(r["slots"], {"object": "高一(1)班", "date": "2026-09-09", "period": 1})

    def test_longest_name_wins(self):
        r = self.NL.parse_local("高一(2)班周四第2节", self.names, self.dates, self.periods)
        self.assertEqual(r["slots"]["object"], "高一(2)班")

    def test_cn_period(self):
        r = self.NL.parse_local("高一(1)班周三第三节", self.names, self.dates, self.periods)
        self.assertEqual(r["slots"]["period"], 3)

    def test_missing_pieces_return_none(self):
        # 缺周几 / 缺节次 / 缺对象 —— 一律 None，绝不猜
        self.assertIsNone(self.NL.parse_local("高一(1)班第1节", self.names, self.dates, self.periods))
        self.assertIsNone(self.NL.parse_local("高一(1)班周三", self.names, self.dates, self.periods))
        self.assertIsNone(self.NL.parse_local("周三第1节", self.names, self.dates, self.periods))

    def test_period_out_of_config_rejected(self):
        # 当天只上 1-3 节，说「第 9 节」必须挡掉（与取值关同口径）
        self.assertIsNone(self.NL.parse_local("高一(1)班周三第9节", self.names, self.dates, self.periods))


if __name__ == "__main__":
    unittest.main()
