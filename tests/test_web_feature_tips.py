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
        cls._mount_code = _strip_comments(_function_body(cls.app_js, "mountFeatureTips"))

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

    def test_click_outside_closes_but_inside_does_not(self):
        """点浮层自身（想读 / 选中正文）不许收起，只有点外面才收起。

        2026-11-14 修：原先 document 上的 click 直接调 closeFeatureTips()，
        点开浮层后想读第二段，一点正文就整块消失 —— 触屏上这个浮层等于没法读。
        """
        self.assertIn("closest('.tip-wrap')", self._mount_code,
                      "收起逻辑没有排除「点在浮层内部」这种情况")
        self.assertRegex(
            self._mount_code, r"closest\('\.tip-wrap'\)\)\s*return;",
            "点了浮层内部仍然会收起（缺提前 return）",
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

    def test_hover_bridge_covers_the_gap(self):
        """按钮与浮层之间的 6px 间隙必须有透明接桥，否则鼠标一过 gap 浮层就消失。

        2026-11-14 修：`top: calc(100% + 6px)` 让两者之间空出一条「谁的 hover 都不算」
        的缝隙，指针横跨时 :hover 丢失 —— 正文读不全、也选不中。
        """
        self.assertIn(".tip-box::before", self.html, "缺 hover 接桥（.tip-box::before）")
        self.assertRegex(
            self.html, r"\.tip-box::before\s*\{[^}]*top:\s*-\d+px",
            "接桥没有向上覆盖间隙，等于没接",
        )
        self.assertRegex(
            self.html, r"\.tip-box::before\s*\{[^}]*height:\s*[1-9]\d*px",
            "接桥高度为 0，盖不住间隙",
        )


class TestFeatureTipsPlacement(unittest.TestCase):
    """ⓘ 浮层贴边定位（卡 rw5IzQ）—— 这组测试是给一类「静默失效」上的锁。

    事故：`web/index.html` 第 960 行残留一行合并冲突标记 `=======`，正好压在
    「功能介绍悬停框」样式块最前面。CSS 的解析规则是「选择器非法 → 整条规则丢弃」，
    于是真正被读的规则变成 `======= .tip-wrap { position: relative; ... }` → 整条被丢掉
    → `.tip-box { position: absolute }` 的包含块退化到初始包含块（视口）
    → 6 个浮层全被摆到同一个「贴左边缘、纵向与按钮无关」的位置。

    🔴 为什么这条必须靠测试守住：**当时所有测试都是绿的**。浮层还在、文案还在、
       三条显示条件也还在，只是位置全错 —— 纯视觉缺陷，单靠静态断言查不出来，
       能做的是把「冲突标记」与「关键定位规则」两件事钉死。

    另：光删掉标记还不够。删完实测发现浮层仍会（a）在 375 档把页面撑出横向滚动、
    （b）贴页面底部时下半截落到视口外。所以 app.js 补了 placeTip()：
    下方不足向上翻、左右越界夹取、边界留 8~12px。下面把这几处实现痕迹也钉死。
    """

    #: Git 冲突标记：行首 7 个 `<` / `=` / `>`，后面可跟分支名。
    #: 🔴 不能直接搜 `"======="` —— app.js 里满是 `/* ======...` 分隔注释，会误报。
    MARKER = re.compile(r"^(?:<{7}|={7}|>{7})(?:\s+\S.*)?$")

    @classmethod
    def setUpClass(cls):
        web_dir = os.path.join(ROOT, "web")
        cls.web_files = {}
        for name in sorted(os.listdir(web_dir)):
            if name.endswith(".html") or name.endswith(".js"):
                with open(os.path.join(web_dir, name), encoding="utf-8") as f:
                    cls.web_files[name] = f.read()
        cls.html = cls.web_files["index.html"]
        cls.app_js = cls.web_files["app.js"]
        cls.place = _strip_comments(_function_body(cls.app_js, "placeTip"))
        cls.bounds = _strip_comments(_function_body(cls.app_js, "tipBounds"))
        cls.trigger = _strip_comments(_function_body(cls.app_js, "tipTrigger"))

    def test_web_has_no_merge_conflict_markers(self):
        """⛔ html / js 里出现冲突标记 = 整块样式或脚本可能被浏览器静默丢弃。"""
        hits = []
        for name, text in self.web_files.items():
            for no, line in enumerate(text.splitlines(), 1):
                if self.MARKER.match(line.strip()):
                    hits.append("web/%s:%d  %s" % (name, no, line.strip()[:40]))
        self.assertEqual(
            hits, [],
            "web/ 下出现合并冲突标记（浏览器会整条丢弃受影响的 CSS 规则，且不报错）：\n"
            + "\n".join(hits),
        )

    def test_tip_wrap_is_positioned(self):
        """`.tip-wrap` 必须是定位元素，否则 `.tip-box` 的 absolute 会挂到视口上。

        这条就是本次事故的直接判据：规则被吞 → 浮层全部乱飘。
        ⚠️ 断言的是「规则里真的有 position: relative」，不是「文件里有这个词」——
        注释里写一遍不算数（`=======` 那种情况正好会让人以为写了）。
        """
        css = re.sub(r"/\*.*?\*/", "", self.html, flags=re.S)  # 去注释再断言
        self.assertRegex(
            css, r"\.tip-wrap\s*\{[^}]*position:\s*relative",
            ".tip-wrap 没有 position: relative —— 浮层的包含块会退化成视口，位置全错",
        )

    def test_flip_up_implemented(self):
        """下方空间不足要向上翻：既要加翻转类，也要真的把 top 算成负值。"""
        self.assertIn("tip-flip-up", self.place, "placeTip 没有向上翻转的分支")
        self.assertRegex(
            self.place, r"topPx\s*=\s*-\(\s*boxRect\.height",
            "翻转分支没有把浮层顶到按钮上方（应算出负的 top）",
        )

    def test_flip_up_moves_hover_bridge(self):
        """翻到上方后 6px 间隙在浮层下方，透明接桥必须跟着换方向。

        不换的后果：鼠标从 ⓘ 往上移到浮层时跨过空当，:hover 断掉、浮层提前消失。
        """
        self.assertRegex(
            self.html, r"\.tip-flip-up[^{]*\.tip-box::before\s*\{[^}]*bottom:\s*-\d+px",
            "向上翻时接桥还在上边（缺 bottom 负值），鼠标一移过去浮层就消失",
        )

    def test_horizontal_clamp_implemented(self):
        """左 / 右越界要夹到边界内（375 档不夹就会把页面撑出横向滚动）。"""
        self.assertRegex(
            self.place, r"box\.style\.left\s*=\s*dx\s*\+\s*'px'",
            "placeTip 没有把水平位移落到 left 上",
        )
        self.assertRegex(self.place, r"bounds\.right\s*-\s*TIP_MARGIN",
                         "没有按右边界夹取")
        self.assertRegex(self.place, r"bounds\.left\s*\+\s*TIP_MARGIN",
                         "没有按左边界夹取")

    def test_margin_between_8_and_12(self):
        """队长口径：浮层离视口边留 8~12px。"""
        m = re.search(r"const TIP_MARGIN\s*=\s*(\d+)", self.app_js)
        self.assertIsNotNone(m, "找不到 TIP_MARGIN 常量")
        self.assertGreaterEqual(int(m.group(1)), 8, "边距小于 8px，贴得太死")
        self.assertLessEqual(int(m.group(1)), 12, "边距大于 12px，会显得浮层离边太远")

    def test_bounds_use_layout_width_not_inner_width(self):
        """边界要用 `documentElement.clientWidth`，不能用 `window.innerWidth`。

        375 档实测：内容一溢出，移动端布局视口被撑到 565，拿 innerWidth 当右边界
        就会「夹完还是溢出」。同时要认「最近一个会裁剪的祖先」（抽屉正文是滚动容器，
        absolute 后代照样被它裁），否则抽屉里的那个浮层会被切掉一半。
        """
        self.assertIn("clientWidth", self.bounds,
                      "边界没用 layout 宽度，窄屏溢出时会越夹越宽")
        self.assertIn("overflow", self.bounds,
                      "没有考虑 overflow 祖先的裁剪（抽屉滚动容器里的浮层会被切掉）")

    def test_three_triggers_still_wire_placement(self):
        """三条触发路径都要重算位置：鼠标（mouseenter）/ 键盘（focusin）/ 触屏（click）。"""
        for ev in ("mouseenter", "focusin"):
            self.assertIn(ev, self.trigger, "缺少 %s 时的定位调用（那条触发路径会不贴边）" % ev)
        self.assertRegex(
            self.trigger, r"if \(open\) placeTip\(wrap\)",
            "触屏点开时没有定位：.is-open 加了但位置没算（手机上没有 hover，只有这条路）",
        )

    def test_no_transition_or_animation_on_tip_box(self):
        """全站口径：不加 transition / animation（现场投影可能不稳）。"""
        m = re.search(r"\.tip-box\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(m, "找不到 .tip-box 规则")
        rule = m.group(1)
        self.assertNotIn("transition", rule, ".tip-box 加了 transition（全站禁用）")
        self.assertNotIn("animation", rule, ".tip-box 加了 animation（全站禁用）")
        self.assertIn("display: none", rule, "默认隐藏必须仍是 display:none")

    def test_width_cap_still_holds(self):
        """🔴 768px 铁律：宽度按视口收口（与上一个 class 的断言同义，这里再锁一次）。

        夹取逻辑只能移动浮层、不能改变它的宽度上限；两条同时成立才不会横向滚动。
        """
        self.assertRegex(self.html, r"\.tip-box\s*\{[^}]*max-width:[^;}]*100vw")

    def test_asset_version_synced(self):
        """改了 web/** 必须同步缓存键，且 index.html 三处 `?v=` 一个都不能漏。"""
        cfg = _read(os.path.join("src", "server", "config.py"))
        m = re.search(r'^ASSET_VERSION = "([^"]+)"', cfg, flags=re.M)
        self.assertIsNotNone(m, "config.py 里找不到 ASSET_VERSION")
        version = m.group(1)
        found = re.findall(r"\?v=([A-Za-z0-9._-]+)", self.html)
        self.assertEqual(
            found, [version, version, version],
            "index.html 的 ?v= 与 config.py 的 ASSET_VERSION 不一致（漏一处等于没改）：%s" % found,
        )


if __name__ == "__main__":
    unittest.main()
