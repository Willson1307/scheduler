"""消解结果卡片的文案口径验收测试（处数 / 方案数不许混，迭代轮数不许出现）。

## 为什么有这条断言

截图上的数字曾经自相矛盾：结果条写「已消解 **4** 处（30 → 0）」，明细又写
「已应用方案数 4 / 迭代轮数 4 / 因不在你的可改范围而未尝试 30」。

核对后端后确认**求解器没算错**，是文案把「方案数」当成了「冲突处数」：

| 字段 | 真实含义 | 来源 |
|---|---|---|
| `remaining_before` / `remaining_after` | 冲突**处数**（消解后重扫全库） | `api_resolve.py` |
| `applied` | 应用的**方案数**（每轮只应用 1 个最优方案） | `b4_kernels` |
| `rounds` | 后端直接写 `rounds = applied` —— 与 `applied` **恒等** | `api_resolve.py:257` |
| `filtered_out_count` | 被跳过的**候选方案数**，不是处数 | `api_resolve.py` |

一个方案能同时解开多处冲突（把一场活动挪走，它和 3 个班撞的 3 处一起消失），
所以「4 个方案解掉 30 处」是正常结果。

## 钉死什么（2026-11-15 三处文案 + 版式，一次改完）

屏幕上的最终形态（只有四行，每条一个量）：

```
冲突 30 处 → 0 处（ok）
已应用 4 个调整方案
另有 30 个候选方案因涉及你无权改动的课程而跳过，系统未尝试求解——不是消解失败，是权限边界。
[撤销本次消解]
```

1. **删掉「迭代轮数」**：它与 `applied` 恒等，同屏两个一样的数字会被当成 bug。
   代码里不得再出现「迭代轮数」，也不得再读 `rounds` 字段（响应字段仍返回，兼容旧客户端）。
2. **不再有「键：值」两行**：`applied` 只以一句「已应用 N 个调整方案」出现，
   且**整块只能说一次**；不得再出现「已应用方案数」「已消解 N 处」这类写法
   （后者把方案数说成处数，又重复了 banner 的 30 → 0）。
3. **跳过数不许带「处」**：`filtered_out_count` 是候选方案数，提示句必须说「个候选方案」。
4. **撤销条只放按钮**：数字上面已说完，横幅里再说一遍就是重复。
5. 注释里可以写「迭代轮数」「处」来解释为什么改（去注释后再断言，否则逼人删注释）。

## 为什么读源码而不是起浏览器

项目硬约束是 Python 标准库 + 零第三方依赖（AGENTS §5），仓库里没有浏览器驱动。
既有前端断言（`tests/test_web_conflict_periods.py`、
`tests/test_announcements.py::test_no_innerhtml_assignment_anywhere`）用的是同一套
源码静态断言思路，本文件与之同构。

运行：python -m unittest tests.test_web_resolve_result_text -v
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


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
    """去掉 `/* */` 与 `//` 注释，只留代码。

    必须去注释：函数里**故意**留着一条注释解释「为什么删掉迭代轮数」，
    不清洗就会把这段解释误判成违规，逼着下一个人删掉设计意图。
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


class TestRoundsRowRemoved(unittest.TestCase):
    """「迭代轮数」与「已应用方案数」恒等 → 不上屏幕。"""

    @classmethod
    def setUpClass(cls):
        cls.body = _function_body(_read(os.path.join("web", "app.js")), "renderResolveResult")
        cls.code = _strip_comments(cls.body)

    def test_no_rounds_label(self):
        """代码里不得再出现「迭代轮数」这一行文案。"""
        self.assertNotIn(
            "迭代轮数", self.code,
            "renderResolveResult 又把「迭代轮数」显示出来了 —— 它和「已应用方案数」"
            "是同一个数字（后端 rounds = applied），同屏并列会被当成 bug",
        )

    def test_rounds_field_not_read(self):
        """连 `rounds` 字段都不再读 —— 留着变量迟早有人又把它塞回屏幕上。"""
        self.assertIsNone(
            re.search(r"intOrNull\(\s*body\s*,\s*['\"]rounds['\"]\s*\)", self.code),
            "renderResolveResult 里又读了 rounds 字段",
        )

    def test_applied_said_once(self):
        """「已应用 N 个调整方案」必须还在，且**整块只说一次**（不重复同一数字）。"""
        self.assertNotIn(
            "已应用方案数", self.code,
            "「已应用方案数」这种键值行又被加回来了 —— 版式已定成一句人话",
        )
        self.assertEqual(
            self.code.count("个调整方案"), 1,
            "「个调整方案」出现次数不是 1 —— 同一个数字在屏幕上说了不止一遍，"
            "旧文案「已消解 4 处（30 → 0）」就是这么自相矛盾的",
        )

    def test_filtered_hint_kept(self):
        """跳过候选方案的那句提示必须留着 —— 否则老师以为系统全都试过了。

        ⚠️ 只锁「还在讲『无权改动/可改范围』这件事」，**不锁原文**：
        口径与版式改过两次，写死原文只会让下一次调整连带红一片。
        """
        self.assertRegex(
            self.code, r"(可改范围|无权改动)",
            "renderResolveResult 里「因权限边界而跳过的候选方案」那句提示不见了 —— "
            "少了它，屏幕上的数字就只剩冲突变化，老师看不出系统试过多少",
        )


class TestPlanCountVsConflictCount(unittest.TestCase):
    """`applied` / `filtered_out_count` 都是**方案数**，不许说成「处」。"""

    @classmethod
    def setUpClass(cls):
        cls.body = _function_body(_read(os.path.join("web", "app.js")), "renderResolveResult")
        cls.code = _strip_comments(cls.body)

    def test_undo_strip_has_no_numbers(self):
        """撤销条只放按钮 —— 数字上面已经说过，再说一遍就是重复。

        旧写法「已消解 N 处（30 → 0）」既把方案数说成处数，又重复了 banner 的两个数。
        """
        self.assertNotIn(
            "已消解", self.code,
            "撤销条又写成「已消解 N 处」了 —— applied 是方案数，"
            "说成「处」会和上方「30 → 0 处」直接打架",
        )

    def test_applied_unit_is_plan(self):
        """`applied` 后面跟的单位只能是「个调整方案」，不许是「处」。"""
        self.assertIsNone(
            re.search(r"String\(\s*applied\s*\)[\s\S]{0,30}?['\"]\s*处", self.code),
            "applied 又被当成冲突处数显示了",
        )

    def test_filtered_unit_is_plan(self):
        """`filtered_out_count` 是候选方案数 —— 拼「处」会让老师以为还有 N 处冲突没解。"""
        self.assertIsNone(
            re.search(r"filtered\s*\+\s*['\"]\s*处", self.code),
            "filtered_out_count 又被拼成「N 处」了（它是候选方案数，不是冲突处数）",
        )

    def test_filtered_worded_as_candidate_plans(self):
        """跳过数必须以「个候选方案」出现 —— 单位是方案不是处。"""
        self.assertIn(
            "个候选方案", self.code,
            "跳过数没有说成「个候选方案」—— filtered_out_count 又被当成冲突处数讲了",
        )


class TestAssetVersionSingleDefinition(unittest.TestCase):
    """`ASSET_VERSION` 在 config.py 里只许定义一次。

    🔴 2026-11-15 真踩过：往注释块下面追加版本沿革时，手一抖又写了一行
    `ASSET_VERSION = "..."`，Python 取**最后一行**生效 —— 两行恰好同值，
    所以全量测试一片绿，直到下一次只改了一处才暴露成「浏览器跑旧 JS」。
    这是本项目最痛的静默失效形态：**重复定义不报错，只是默默用了另一个值**。
    """

    def test_only_one_assignment(self):
        src = _read(os.path.join("src", "server", "config.py"))
        hits = re.findall(r"(?m)^ASSET_VERSION\s*=", src)
        self.assertEqual(
            len(hits), 1,
            f"config.py 里 ASSET_VERSION 定义了 {len(hits)} 次 —— 只能留一处，"
            "多的那行会被 Python 静默采用（谁在后面谁生效）",
        )


if __name__ == "__main__":
    unittest.main()
