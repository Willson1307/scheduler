"""防复发闸门：scripts/ deploy/ src/ 里不许出现已知弱口令字面量。

背景（2026-10-24/25 两次踩中同一形态）：
  - 2026-10-23 试用库口令全量随机化，但 `scripts/check_trial_isolation.py` 仍写死 "123456"
    → 隔离自查脚本一跑就 401，而它被 docs/21、docs/30、docs/31 三处引用，等于证据链断了。
  - `deploy/trial_smoke.py` 曾写死 "trial123"，导致「改了 admin 口令，验收脚本静默失效」。
根因不是难，是**改口令相关代码时只改了清单上列的文件，没有全仓扫一遍**。
本测试把「全仓扫一遍」变成 CI 级别的一行断言：新增弱口令字面量会直接判红。

判据：
  `scripts/`、`deploy/`、`src/` 下 .py/.sh 文件中，出现 `123456` / `trial123`
  且文件不在 ALLOWED 里 → 失败。

ALLOWED 的两种合法情形（都必须写理由，且文件必须存在）：
  1. 建库初始口令（写完立刻被 reset 脚本覆盖）；
  2. 已标注过期的历史一次性脚本（保留留痕，不参与运行链路）。
"""

from __future__ import annotations

import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 扫描的目录（相对仓库根）。tests/ 不在范围内：测试自建临时库用弱口令是正常的。
SCAN_DIRS = ("scripts", "deploy", "src")

#: 已知弱口令字面量。
WEAK = ("123456", "trial123")

#: 允许出现的位置 → 理由（文件必须存在，否则本测试也会判红，防「白名单烂尾」）。
ALLOWED = {
    "scripts/build_trial_org.py":
        "建库初始教师口令 TEACHER_PASSWORD，建库后必须立刻被 reset_trial_passwords.py 覆盖",
    "scripts/probe_screenshots.py":
        "2026-10-11 一次性诊断脚本，文件头已标「已过期，勿运行」，保留留痕",
    "src/scheduler/seeds.py":
        "演示库(demo.db)教师初始口令；契约测试硬编码依赖它（test_contract.py），"
        "且 src/scheduler 属 AGENTS.md:327-330 只读域层 → 要改须另开卡并同步改契约",
}


def _iter_sources():
    for d in SCAN_DIRS:
        for root, _, files in os.walk(os.path.join(REPO, d)):
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith((".py", ".sh")):
                    path = os.path.join(root, f)
                    yield os.path.relpath(path, REPO).replace(os.sep, "/"), path


class NoHardcodedWeakPasswordTest(unittest.TestCase):
    def test_allowlist_files_exist(self):
        """白名单不能烂尾：列出的文件必须真的存在。"""
        for rel in ALLOWED:
            self.assertTrue(
                os.path.exists(os.path.join(REPO, rel)),
                f"ALLOWED 里列了不存在的文件：{rel}（改口令收口了就把这条白名单删掉）",
            )

    def test_no_weak_password_literal(self):
        """scripts/ deploy/ src/ 下不得残留已知弱口令（除白名单）。"""
        hits = []
        for rel, path in _iter_sources():
            if rel in ALLOWED:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for ln_no, line in enumerate(text.splitlines(), 1):
                for w in WEAK:
                    if w in line:
                        hits.append(f"{rel}:{ln_no} 含弱口令字面量 {w!r} → {line.strip()[:80]}")
        self.assertEqual(
            hits, [],
            "发现口令硬编码（口令已全部随机化，硬编码会静默失效）：\n  "
            + "\n  ".join(hits)
            + "\n正确做法：从 data/trial-passwords.txt 读，或用 --admin-password-stdin / 环境变量传入。",
        )

    def test_weak_list_is_not_empty(self):
        """自检：弱口令清单本身不能被清空（防「删了断言就等于没有」）。"""
        self.assertTrue(WEAK and all(WEAK), "WEAK 清单不能为空")


if __name__ == "__main__":
    unittest.main()
