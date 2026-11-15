"""消解结果卡片的文案口径验收测试（迭代轮数那一行必须消失）。

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

`rounds` 与 `applied` 恒等 → 同屏并排显示两个一样的数字，评委会当成 bug 追着问。
**这一行删掉**（响应字段 `rounds` 仍返回，只是前端不上屏幕）。

## 钉死什么

1. `renderResolveResult()` 的代码里不再出现「迭代轮数」，也不再读 `rounds` 字段；
2. 明细里另外两行**不许一起被删**：删多了等于把「方案数」和「跳过数」两个真实
   信息也丢了，老师就只剩一个冲突变化数字，看不懂发生了什么；
3. 注释里可以写「迭代轮数」来解释为什么删（去注释后再断言，否则逼人删注释）。

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


if __name__ == "__main__":
    unittest.main()
