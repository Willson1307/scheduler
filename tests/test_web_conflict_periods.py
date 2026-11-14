"""冲突卡片「节次」显示验收测试（卡 rEz9Ms）。

问题：`renderSide()` 原先把 `side.slots`（形如 "0x0002" 的位图掩码）直接打印，
老师看到的是「节次 0x0002」—— 掩码不是数值，读不出来是哪几节。

本文件钉死四条，任何一条被改回去都必须立刻变红：

1. `renderSide()` 从 `side.readable` 取节次，不再把 `slots` 当文本输出；
2. 掩码原文只出现在 `title`（悬停可核对），不出现在正文；
3. `readable` 缺失时降级为「节次未给出」，**绝不回退打印 hex**（回退等于没修）；
4. `ASSET_VERSION` 与 `web/index.html` 三处 `?v=` 完全一致 ——
   漏一处 = 浏览器继续跑旧 JS，现场照样显示 hex。这类漂移历史上已发生多次
   （见 `src/server/config.py` 的版本沿革注释），且**不报错**，属静默失效，
   只能靠断言兜住。

为什么是「读源码」而不是「起浏览器跑一遍」：
  项目硬约束是 Python 标准库 + 零第三方依赖（AGENTS §5），仓库里没有、也不该有
  浏览器驱动。既有前端断言（`tests/test_announcements.py` 的
  `test_no_innerhtml_assignment_anywhere`）用的就是同一套源码静态断言思路。

运行：python -m unittest tests.test_web_conflict_periods -v
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from server import config  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _function_body(src: str, name: str) -> str:
    """取 `function <name>(...) { ... }` 的函数体（按花括号配平，零依赖）。

    与 `tests/test_announcements.py` 里的同名辅助同构，故意不抽公共模块：
    抽出去以后两个测试文件互相牵连，改动一处要同时改两处。
    """
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
    """去掉 `//` 与 `/* */` 注释，只留代码。

    必须去注释再扫约定：`renderSide()` 的注释里**故意**写着
    「0x000a = 第 1+3 节」来解释为什么不能把 hex 当数值 —— 那是解释文字，
    不是换算代码。不剥离就会把讲解误判成违规，逼着下一个人删掉注释。
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


class TestRenderSideShowsPeriods(unittest.TestCase):
    """`renderSide()` 必须把掩码换成「第 N 节」。"""

    @classmethod
    def setUpClass(cls):
        cls.body = _function_body(_read(os.path.join("web", "app.js")), "renderSide")
        cls.code = _strip_comments(cls.body)

    def test_reads_readable(self):
        """节次必须来自后端给的 readable，而不是前端自己解掩码。"""
        self.assertIn("arr(side, 'readable')", self.body)
        self.assertIn("readablePeriod(", self.body)

    def test_mask_not_printed_as_text(self):
        """掩码原文不得进入正文（旧写法是 `<code>{ text: s }</code>`）。"""
        self.assertNotIn("el('code'", self.code, "renderSide 又把 slots 当 <code> 打印了")
        self.assertIsNone(
            re.search(r"text:\s*s\b", self.code),
            "renderSide 里仍有把 slots 原样当文本输出的写法",
        )

    def test_mask_moved_to_title(self):
        """掩码降级到悬停提示，悬停仍可核对（与 renderMasks 同一写法）。"""
        self.assertIn("'占用掩码 ' + s", self.code)
        self.assertIn("title:", self.code)

    def test_degrade_text_when_readable_missing(self):
        """readable 缺失/非法时显式说「节次未给出」，不回退打印 hex。"""
        self.assertIn("节次未给出", self.code)

    def test_periods_deduped_and_sorted(self):
        """多项用「、」连接，先去重再升序（后端顺序不保证）。"""
        self.assertIn("indexOf(p) < 0", self.code, "没有去重")
        self.assertRegex(self.code, r"periods\.sort\(")
        self.assertIn("periods.join('、')", self.code)

    def test_no_bitwise_or_hex_conversion(self):
        """🔴 前端不做位运算、不自拼 hex、不把掩码当数字换算（AGENTS §8 / §9b）。

        ⚠️ 这里不拦单个 `&`：`title && sideTitle(...)` 里的 `&&` 也会命中，
        拦下去只会逼人写坏代码。真正要拦的是「拿掩码做位运算」的组合形态，
        由 `0x` / `<<` / `parseInt(` / `toString(16)` 四个特征词覆盖。
        """
        for bad in ("0x", "toString(16)", "parseInt(", "<<"):
            self.assertNotIn(bad, self.code, f"renderSide 出现位运算/hex 换算痕迹：{bad}")

    def test_no_innerhtml(self):
        """动态文本一律 textContent（经 el() 封装）。"""
        for bad in ("innerHTML", "insertAdjacentHTML", "document.write", "outerHTML"):
            self.assertNotIn(bad, self.code)


class TestAssetVersionConsistency(unittest.TestCase):
    """缓存键必须三处同值 —— 漏一处就是「改了前端偶尔不生效」的静默失效。"""

    def test_config_matches_all_index_refs(self):
        html = _read(os.path.join("web", "index.html"))
        refs = re.findall(r"\?v=([0-9]+)", html)
        self.assertEqual(len(refs), 3, "index.html 的 ?v= 引用数量变了，断言需同步")
        self.assertEqual(
            len(set(refs)), 1,
            f"index.html 三处 ?v= 不一致：{refs} —— 自相矛盾，浏览器会混用新旧文件",
        )
        self.assertEqual(
            refs[0], config.ASSET_VERSION,
            "config.ASSET_VERSION 与 index.html 的 ?v= 不一致："
            f"{config.ASSET_VERSION} vs {refs[0]}。漏改一处 = 浏览器继续用旧 app.js",
        )

    def test_asset_version_is_digits(self):
        """版本号只允许纯数字（页面里直接拼进 URL，混入其它字符会拼出坏地址）。"""
        self.assertRegex(config.ASSET_VERSION, r"^\d+$")


if __name__ == "__main__":
    unittest.main()
