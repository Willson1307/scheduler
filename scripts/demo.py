"""B0 批次的可运行演示：建试用组织 → 排课 → 扫冲突 → 打印报告。

运行：
    python scripts/demo.py

这个脚本的用途是"每批都要产出能看的东西"——
不看代码也能判断这批做得对不对。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import occupancy as occ  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.object_graph import descendants  # noqa: E402
from scheduler.occupancy import busy_mask, find_free_slots, total_load  # noqa: E402
from scheduler.seeds import seed_demo  # noqa: E402
from scheduler.slots import decode_mask, load_slot_config  # noqa: E402

WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def name_of(conn, oid: str) -> str:
    row = conn.execute("SELECT name, kind FROM objects WHERE id = ?", (oid,)).fetchone()
    return f"{row['name']}({row['kind']})" if row else oid


def title_of(conn, sid: str) -> str:
    row = conn.execute("SELECT title FROM schedules WHERE id = ?", (sid,)).fetchone()
    return row["title"] if row else sid


def main() -> None:
    conn = connect(os.environ.get("DEMO_DB", ":memory:"))
    init_db(conn)
    info = seed_demo(conn, classes=3, students_per_class=10, teachers=9, rooms=4)

    print("=" * 68)
    print("日程调度辅助系统 · B0 数据层演示")
    print("=" * 68)
    print(f"组织：{info['org_id']}")
    print(f"根组（全校）：{name_of(conn, info['root_group_id'])}")
    print(
        f"班级 {len(info['class_ids'])} 个 / 教师 {len(info['teacher_ids'])} 位"
        f" / 学生 {len(info['student_ids'])} 名 / 教室 {len(info['room_ids'])} 间"
    )
    print(f"学期：{info['term_start']} ~ {info['term_end']}")
    print(f"管理员登录：{info['admin']['login']} / {info['admin']['password']}")
    root_members = descendants(conn, info["root_group_id"])
    print(f"根组下对象总数（传递闭包一次查询）：{len(root_members)}")

    d0 = date.fromisoformat(info["term_start"])
    d6 = d0 + timedelta(days=6)
    cfg = load_slot_config(conn, info["org_id"])

    print("\n--- 第 1 周占用与冲突扫描 ---")
    entries = occ.build_occupancy(conn, info["org_id"], d0, d6, cfg=cfg)
    print(f"占用条目数：{len(entries)}")
    report = occ.scan(conn, info["org_id"], d0, d6, cfg=cfg)
    print(f"检出冲突：{report.count} 处")

    # 按「哪两条日程 × 哪天 × 哪几节」聚合，避免同一个班 40 名学生刷屏
    groups: dict[tuple, list] = {}
    for c in report.conflicts:
        groups.setdefault((c.schedule_a, c.schedule_b, c.date, c.mask), []).append(c)
    for (a, b, d, mask), items in groups.items():
        slots_txt = "、".join(f"{WEEKDAY[dd - 1]}第{i}节" for dd, i in decode_mask(mask))
        roles = "/".join(sorted({i.role for i in items}))
        print(f"  · {d} {slots_txt}")
        print(
            f"    『{title_of(conn, a)}』 × 『{title_of(conn, b)}』\n"
            f"    受影响对象 {len(items)} 个（角色 {roles}）"
            f"，例：{name_of(conn, items[0].object_id)}"
        )

    print("\n--- 某位教师的空闲时段 ---")
    t = info["teacher_ids"][0]
    print(f"教师：{name_of(conn, t)}，本周占用 {total_load(entries, t)} 节")
    for d in (d0, d0 + timedelta(days=1)):
        free = find_free_slots(entries, [t], d, cfg)
        slots_txt = "、".join(f"第{i}节" for _dd, i in decode_mask(free)) or "（无）"
        print(f"  {d} {WEEKDAY[d.weekday()]}：空闲 {slots_txt}")
        print(f"    占用掩码 = {format(busy_mask(entries, [t], d), 'x')}")

    conn.close()


if __name__ == "__main__":
    main()
