"""L11 命令行演示脚本：不启动 HTTP，直接调用领域层，把核心流程演一遍。

用途与设计意图（答辩口径）：
  1. 这是 CP-D 的保底演示 —— 网页端万一没跑通，命令行也能完整演「冲突检测 → 一键消解」；
  2. 所有数字都是当场真跑出来的，没有一处写死或估计，评委现场可复算；
  3. 固定开学日期 + 全内存库，每次运行输出逐字一致，可重复验证；
  4. 只用 Python 标准库，断网可跑，是作品本体而非工具链。

运行：
    python scripts/demo_cli.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler.db import connect  # noqa: E402
from scheduler.occupancy import scan  # noqa: E402
from scheduler.seeds import seed_demo  # noqa: E402
from scheduler.slots import decode_mask, load_slot_config  # noqa: E402
from scheduler.solver import m2 as solver  # noqa: E402

WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 固定开学日期（必须是周一）：种子里的「全年级大会」落在 start+2 天（周三），
# 正好撞上周期物理课。固定它 → 每次运行结果逐字一致。
TERM_START = "2026-09-07"

SEP = "-" * 40


def name_of(conn, oid: str) -> str:
    row = conn.execute("SELECT name FROM objects WHERE id = ?", (oid,)).fetchone()
    return row["name"] if row else oid


def title_of(conn, sid: str) -> str:
    row = conn.execute("SELECT title FROM schedules WHERE id = ?", (sid,)).fetchone()
    return row["title"] if row else sid


def org_name(conn, org_id: str) -> str:
    row = conn.execute("SELECT name FROM organizations WHERE id = ?", (org_id,)).fetchone()
    return row["name"] if row else org_id


def fmt_slots(mask: int) -> str:
    return "、".join(f"{WEEKDAY[w - 1]}第{i}节" for w, i in decode_mask(mask))


def main() -> None:
    conn = connect(":memory:")
    # 🔴 参数必须与 scripts/demo.py 完全一致，否则冲突数与项目书对不上
    info = seed_demo(
        conn, classes=3, students_per_class=10, teachers=9, rooms=4, term_start=TERM_START
    )
    org_id = info["org_id"]
    cfg = load_slot_config(conn, org_id)

    d0 = date.fromisoformat(info["term_start"])
    d6 = d0 + timedelta(days=6)  # 只扫第 1 周，覆盖种子故意造的两类冲突

    print("=" * 40)
    print("日程调度辅助系统 · 现场演示")
    print("课表活动撞车难调，AI 求解一键消解")
    print("=" * 40)

    # ── 第 1 段：建演示库 ──────────────────────────────
    print()
    print("【1】建演示库")
    print(f"  组织：{org_name(conn, org_id)}")
    print(
        f"  规模：{len(info['class_ids'])} 个班 · {len(info['teacher_ids'])} 位教师 · "
        f"{len(info['student_ids'])} 名学生 · {len(info['room_ids'])} 间教室"
    )
    print(f"  学期：{info['term_start']} ~ {info['term_end']}（20 周）")
    print("  说明：开学日期固定 → 每次运行结果完全一致")

    # ── 第 2 段：扫描冲突 ──────────────────────────────
    report1 = scan(conn, org_id, d0, d6, cfg=cfg)
    n = report1.count

    # 按「日程对 × 日期 × 节次」去重，避免同一个班几十名学生刷屏
    groups: dict[tuple, list] = {}
    for c in report1.conflicts:
        groups.setdefault((c.schedule_a, c.schedule_b, c.date, c.mask), []).append(c)

    # 排序键只用标题（确定）而非 id（随机 uuid），保证跨运行逐字一致
    def _sort_key(item):
        (a, b, d, m), items = item
        return (
            0 if items[0].role == "teacher" else 1,
            title_of(conn, a),
            title_of(conn, b),
            d.isoformat(),
            m,
        )

    ordered = sorted(groups.items(), key=_sort_key)

    print()
    print(SEP)
    print("【2】扫描冲突（第 1 周）")
    print(f"  当前冲突：{n} 处")
    print(f"  去重后 {len(ordered)} 组「日程对 × 日期 × 节次」，举例：")
    for i, ((_a, _b, d, mask), items) in enumerate(ordered[:3], 1):
        ex = items[0]
        print(f"    {i}. {name_of(conn, ex.object_id)}｜{d.isoformat()} {fmt_slots(mask)}")
        print(f"       『{title_of(conn, ex.schedule_a)}』 × 『{title_of(conn, ex.schedule_b)}』")

    # ── 第 3 段：一键消解 ──────────────────────────────
    applied, remaining = solver.resolve_and_apply(conn, org_id, d0, d6, cfg=cfg)
    moves = [m for p in applied for m in p.moves]

    print()
    print(SEP)
    print("【3】一键消解")
    print(f"  应用 {len(moves)} 个改动，消解后剩余冲突：{remaining} 处")
    for m in moves:
        print(f"    · {m.describe()}")

    # ── 第 4 段：复核确认 ──────────────────────────────
    report2 = scan(conn, org_id, d0, d6, cfg=cfg)
    m2 = report2.count
    pct = round((n - m2) / n * 100) if n else 0

    print()
    print(SEP)
    print("【4】复核确认")
    print(f"  再扫一遍：{m2} 处（与消解器返回值{'一致' if m2 == remaining else '不一致'}）")
    print(f"  冲突 {n} → {m2}，下降 {pct}%")
    print(f"  改动 {len(moves)} 处，每步都取当前「改动幅度 + 软约束代价」最小的方案")

    conn.close()
    print()
    print(SEP)
    print("演示结束")


if __name__ == "__main__":
    main()
