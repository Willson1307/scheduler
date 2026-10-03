"""L8 演示库重置脚本：一键把演示库恢复到固定初始状态。

用途与设计意图（答辩口径）：
  1. 现场演示只有 5 分钟、没有第二次机会。演示前一条命令回到「固定的、能讲出故事的
     初始状态」，否则前一次演示或自己调试留下的脏数据会让现场翻车。
  2. 固定种子：开学日期固定、规模固定、参数显式传 students_per_class=10，
     每次重置出来的冲突数（23）、对象数（49）逐字一致，现场可复现。
  3. 沙盒隔离：演示库落在独立的 data/demo.db，与试用库 data/trial.db 文件级分开、
     互不污染（不同文件天然不同 org_id，无需复杂的事务回滚）。
  4. 只用 Python 标准库，断网可跑，是作品本体而非工具链。

运行：
    python scripts/reset_demo.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler.db import connect  # noqa: E402
from scheduler.occupancy import scan  # noqa: E402
from scheduler.seeds import seed_demo  # noqa: E402
from scheduler.slots import load_slot_config  # noqa: E402

# 演示库路径：锚定仓库根（脚本所在目录的上一级），不受当前工作目录影响
DEMO_DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "demo.db"))

# 固定开学日期（必须是周一）：种子里的「全年级大会」落在 start+2 天（周三），
# 正好撞上周期物理课。固定它 → 每次重置结果逐字一致。
TERM_START = "2026-09-07"


def main() -> None:
    # ── 1. 删旧库（含 -wal/-shm 残留）──
    for suffix in ("", "-wal", "-shm"):
        p = DEMO_DB + suffix
        if os.path.exists(p):
            os.remove(p)

    # ── 2. 重建 + 固定种子 ──
    # 🔴 students_per_class 必须显式传 10，否则默认 8 → 19 处冲突，与材料里的 23 对不上
    conn = connect(DEMO_DB)
    info = seed_demo(
        conn,
        classes=3,
        students_per_class=10,
        teachers=9,
        rooms=4,
        term_start=TERM_START,
    )
    org_id = info["org_id"]

    # ── 3. 对象数（真查，不硬编码）──
    n_objects = conn.execute("SELECT COUNT(*) AS c FROM objects").fetchone()["c"]

    # ── 4. 扫第 1 周冲突（范围与 demo_cli.py 完全一致）──
    cfg = load_slot_config(conn, org_id)
    d0 = date.fromisoformat(info["term_start"])
    d6 = d0 + timedelta(days=6)
    n_conflicts = scan(conn, org_id, d0, d6, cfg=cfg).count

    conn.close()

    # ── 5. 打印结果，让人一眼确认库是对的 ──
    print("=" * 40)
    print("演示库重置完成 · 沙盒模式")
    print("=" * 40)
    print(f"库文件：{DEMO_DB}")
    print(f"组织：试用学校（org_id={org_id}）")
    print(
        f"规模：{len(info['class_ids'])} 个班 · {len(info['teacher_ids'])} 位教师 · "
        f"{len(info['student_ids'])} 名学生 · {len(info['room_ids'])} 间教室"
    )
    print(f"对象数：{n_objects}")
    print(f"扫描第 1 周（{info['term_start']} ~ {d6.isoformat()}）冲突：{n_conflicts} 处")
    print()
    print("✅ 演示库已就绪：有冲突可看，可立即接入演示流程")


if __name__ == "__main__":
    main()
