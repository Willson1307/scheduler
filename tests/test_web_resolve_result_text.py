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

## 钉死什么（2026-11-15 三处文案，一次改完）

1. **删掉「迭代轮数」**：它与 `applied` 恒等，同屏两个一样的数字会被当成 bug。
   代码里不得再出现「迭代轮数」，也不得再读 `rounds` 字段（响应字段仍返回，兼容旧客户端）。
2. **撤销条不许把方案数说成处数**：`applied` 只能以「已应用 N 个调整方案」出现，
   不得再出现「已消解 N 处」这种自相矛盾的写法。
3. **跳过数不许带「处」**：`filtered_out_count` 是候选方案数，
   明细行与提示句都必须说「个候选方案」，不能写成「N 处」。
4. 明细里「已应用方案数」与「跳过候选方案」两行**不许一起被删** —— 删多了等于把
   「方案数」和「跳过数」两个真实信息也丢了，老师只剩一个冲突变化数字。
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

    def test_applied_row_kept(self):
        """「已应用方案数」这一行必须留着 —— 它是「处数」与「方案数」的分母。"""
        self.assertIn("已应用方案数", self.code)

    def test_filtered_row_kept(self):
        """跳过候选方案那一行必须留着 —— 否则老师以为系统全都试过了。

        ⚠️ 只锁「这一行还在、还在讲『无权改动/可改范围』这件事」，**不锁原文**：
        口径复核（2026-11-15）已经把这一行与下方提示句的用词改了一次
        （「处」→「个候选方案」），断言写死原文只会让下一次改口径连带红一片。
        """
        self.assertRegex(
            self.code, r"(可改范围|无权改动)",
            "renderResolveResult 里「因权限边界而跳过的候选方案」那一行不见了 —— "
            "少了它，屏幕上的数字就只剩冲突变化，老师看不出系统试过多少",
        )


class TestPlanCountVsConflictCount(unittest.TestCase):
    """`applied` / `filtered_out_count` 都是**方案数**，不许说成「处」。"""

    @classmethod
    def setUpClass(cls):
        cls.body = _function_body(_read(os.path.join("web", "app.js")), "renderResolveResult")
        cls.code = _strip_comments(cls.body)

    def test_undo_strip_says_plans(self):
        """撤销条必须是「已应用 N 个调整方案（冲突 30 → 0 处）」。

        旧写法「已消解 N 处（30 → 0）」自相矛盾 —— 4 处怎么从 30 变 0？
        """
        self.assertNotIn(
            "已消解", self.code,
            "撤销条又写成「已消解 N 处」了 —— applied 是方案数，"
            "说成「处」会和右边「30 → 0 处」直接打架",
        )
        self.assertIn("个调整方案", self.code, "撤销条没有说明「N 是个方案数」")

    def test_applied_unit_is_plan(self):
        """`applied` 后面跟的单位只能是「个方案/个调整方案」，不许是「处」。"""
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
        """明细行与提示句都要出现「候选方案」（两处，少一处就是单位又混了）。"""
        self.assertGreaterEqual(
            self.code.count("候选方案"), 2,
            "「候选方案」出现不足 2 次 —— 明细行标签与下方提示句至少各要一次，"
            "否则 filtered_out_count 又被当成冲突处数讲了",
        )


if __name__ == "__main__":
    unittest.main()
