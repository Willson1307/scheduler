"""功能介绍悬停框验收测试（卡 rY3Bpn / F-12）。

需求：前端 6 个功能区块（周视图 / 冲突列表 / 查空位 / 消解结果 / 问一下 / 变更请求）
标题右侧各一个 ⓘ，悬停、键盘聚焦、触屏点开都能看到「这是什么 · 怎么用」。

本文件钉死六条，任何一条被改回去都必须立刻变红：

1. `index.html` 里 6 个区块 id 都在；`FEATURE_TIPS` 正好覆盖这 6 个 id ——
   少一个 = 那个功能没有说明，而且**不报错**（静默缺失，本项目最怕的形态）；
2. `boot()` 真的调用了 `mountFeatureTips()` —— 定义了却不挂 = 页面上什么都没有；
3. 浮层的显示条件三条齐全：`:hover`（鼠标）/ `:focus-within`（键盘）/ `.is-open`（触屏）。
   缺 `:focus-within` 键盘用户拿不到，缺 `.is-open` 手机完全看不到；
4. 造浮层的代码里没有任何 `innerHTML` / `insertAdjacentHTML` / `outerHTML` /
   `document.write`（AGENTS §8 防存储型 XSS）；
5. 文案不含接口路径 / 掩码 / 契约编号（承接 F-9 口径，那些留在页面底部技术说明里）；
6. 每个功能至少 2 行说明（只有标题等于没讲「怎么用」）。

为什么是「读源码静态断言」而不是起浏览器：
  项目硬约束是 Python 标准库 + 零第三方依赖（AGENTS §5），仓库里没有也不该有浏览器驱动。
  与 `tests/test_web_conflict_periods.py`、`tests/test_announcements.py` 同一套思路。

运行：python -m unittest tests.test_web_feature_tips -v
"""

from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

#: 6 个功能区块（index.html 的 section id）。⛔ 与 app.js 的 FEATURE_TIPS 必须一一对应。
SECTION_IDS = [
    "secWeek",
    "secConflict",
    "secSlots",
    "secResult",
    "secChat",
    "secRequests",
]


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _function_body(src: str, name: str) -> str:
    """取 `function <name>(...) { ... }` 的函数体（按花括号配平，零依赖）。"""
    start = src.index("function " + name)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("花括号没配平，取不到 " + name + " 的函数体")


def _strip_comments(js: str) -> str:
    """去掉注释再扫约定：文案注释里出现的词不算实现。"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


class TestFeatureTipsCoverage(unittest.TestCase):
    """6 个区块一个都不能漏。"""

    @classmethod
    def setUpClass(cls):
        cls.app_js = _read(os.path.join("web", "app.js"))
        cls.html = _read(os.path.join("web", "index.html"))
        start = cls.app_js.index("const FEATURE_TIPS")
        cls.tips_block = cls.app_js[start:cls.app_js.index("\n];", start) + 3]

    def test_all_sections_exist_in_html(self):
        for sid in SECTION_IDS:
            self.assertIn('id="' + sid + '"', self.html, f"index.html 里找不到区块 {sid}")

    def test_tips_cover_all_sections(self):
        ids = re.findall(r"id:\s*'([A-Za-z]+)'", self.tips_block)
        self.assertEqual(
            sorted(ids), sorted(SECTION_IDS),
            f"FEATURE_TIPS 与 6 个区块对不上：{ids} —— 漏掉的功能在页面上没有说明，且不报错",
        )

    def test_each_tip_has_at_least_two_lines(self):
        names = re.findall(r"name:\s*'([^']+)'", self.tips_block)
        lines = re.findall(r"lines:\s*\[", self.tips_block)
        self.assertEqual(len(names), len(SECTION_IDS), "每个功能都要有中文名（浮层标题）")
        self.assertEqual(len(lines), len(SECTION_IDS), "每个功能都要有自己的说明列表")
        # 「怎么用」至少要说两句：一句定位 + 一句操作
        for chunk in re.split(r"lines:\s*\[", self.tips_block)[1:]:
            body = chunk.split("]")[0]
            self.assertGreaterEqual(
                body.count("'") // 2, 2, "某个功能的说明不足 2 行"
            )

    def test_mounted_in_boot(self):
        boot = _function_body(self.app_js, "boot")
        self.assertIn(
            "mountFeatureTips()", boot,
            "boot() 没调用 mountFeatureTips() —— 定义了不等于挂到页面上（静默失效）",
        )


class TestFeatureTipsSafety(unittest.TestCase):
    """禁 innerHTML + 文案不泄漏开发期口径。"""

    @classmethod
    def setUpClass(cls):
        cls.app_js = _read(os.path.join("web", "app.js"))
        cls.trigger = _strip_comments(_function_body(cls.app_js, "tipTrigger"))
        cls.mount = _strip_comments(_function_body(cls.app_js, "mountFeatureTips"))
        start = cls.app_js.index("const FEATURE_TIPS")
        cls.tips_code = _strip_comments(cls.app_js[start:cls.app_js.index("\n];", start) + 3])

    def test_no_innerhtml(self):
        for bad in ("innerHTML", "insertAdjacentHTML", "document.write", "outerHTML"):
            for name, body in (("tipTrigger", self.trigger), ("mountFeatureTips", self.mount)):
                self.assertNotIn(bad, body, f"{name} 用了 {bad}")

    def test_text_goes_through_el(self):
        """标题与正文必须走 el() 的 text 选项（内部就是 textContent）。"""
        self.assertIn("cls: 'tip-title'", self.trigger)
        self.assertIn("cls: 'tip-line'", self.trigger)
        self.assertIn("text: line", self.trigger)

    def test_copy_has_no_technical_jargon(self):
        for bad in ("/api", "0x", "掩码", "契约 §", "raw_count", "slot_bitmap"):
            self.assertNotIn(
                bad, self.tips_code,
                f"浮层文案出现开发期口径「{bad}」——那些应留在页面底部技术说明里",
            )

    def test_missing_section_does_not_throw(self):
        """区块被改名/删掉时要跳过，不许抛异常把整页脚本打断。"""
        self.assertIn("if (!sec) return;", self.mount)
        self.assertIn("if (!h2) return;", self.mount)


class TestFeatureTipsVisible(unittest.TestCase):
    """浮层必须真的有显示条件，否则等于没做。"""

    @classmethod
    def setUpClass(cls):
        cls.html = _read(os.path.join("web", "index.html"))

    def test_css_has_all_three_triggers(self):
        for sel in (".tip-wrap:hover .tip-box",
                    ".tip-wrap:focus-within .tip-box",
                    ".tip-wrap.is-open .tip-box"):
            self.assertIn(sel, self.html, f"缺显示条件 {sel}")

    def test_css_hides_by_default(self):
        """默认 display:none —— 用 opacity 藏会「看不见却挡住点击」。"""
        self.assertRegex(
            self.html, r"\.tip-box\s*\{[^}]*display:\s*none",
            "浮层默认没有隐藏（会常显或挡住下面的按钮）",
        )

    def test_css_width_capped_by_viewport(self):
        """🔴 768px 铁律：浮层宽度按视口收口，不许把页面撑出横向滚动。"""
        self.assertRegex(self.html, r"\.tip-box\s*\{[^}]*max-width:[^;}]*100vw")


if __name__ == "__main__":
    unittest.main()
