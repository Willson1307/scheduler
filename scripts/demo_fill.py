"""M3 局部补排的现场演示脚本：给某个班/某位老师「找可排位置」。

用途与设计意图（答辩口径）：
  1. 现场能当场演示「周五下午这张老师有空，可以补」——问题不再是"撞车了怎么办"，
     而是"我该安排在哪"，这是「补排」与「消解」两个能力的区别所在；
  2. 只读：脚本不写任何表，跑多少次结果都一样（可以反复演示，不会污染演示库）；
  3. 只用 Python 标准库，不启动 HTTP，网页端万一没跑通也能演。

运行：
    python scripts/demo_fill.py                                  # 默认 data/demo.db，自动挑一个班
    python scripts/demo_fill.py --target "示例1班" --duration 2
    python scripts/demo_fill.py --from 2026-09-21 --to 2026-09-25 --limit 5

⚠️ `--target` 可以写对象名（模糊匹配，唯一命中才用），也可以直接写 object_id。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler.db import connect  # noqa: E402
from scheduler.solver.m2 import find_slots  # noqa: E402

WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_target(conn, org_id: str, text: str) -> str:
    """对象名 → object_id。唯一命中才用，否则把候选列出来让人挑（不猜）。"""
    if conn.execute(
        "SELECT 1 FROM objects WHERE id = ? AND org_id = ?", (text, org_id)
    ).fetchone():
        return text
    rows = conn.execute(
        "SELECT id, kind, name FROM objects WHERE org_id = ? AND name LIKE ? ORDER BY kind, id",
        (org_id, f"%{text}%"),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if not rows:
        raise SystemExit(f"找不到名字包含 {text!r} 的对象；先用 --list 看看有哪些")
    listing = "\n".join(f"  {r['id']:<28} {r['kind']:<6} {r['name']}" for r in rows)
    raise SystemExit(f"{text!r} 命中多个对象，请用 object_id 指定其中一个：\n{listing}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M3：按班/按老师找可排位置（只读）")
    ap.add_argument("--db", default=os.path.join(REPO, "data", "demo.db"))
    ap.add_argument("--org", default=None, help="组织 id，默认取库里第一个")
    ap.add_argument("--target", default=None, help="班级/教师的名字片段或 object_id")
    ap.add_argument("--from", dest="d0", default=None, help="起始日期 YYYY-MM-DD")
    ap.add_argument("--to", dest="d1", default=None, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--duration", type=int, default=1, help="需要连续几节（默认 1）")
    ap.add_argument("--limit", type=int, default=8, help="最多显示几个候选（默认 8）")
    ap.add_argument("--list", action="store_true", help="只列出对象，不查空位")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        raise SystemExit(f"数据库不存在：{args.db}")
    conn = connect(args.db)

    org = args.org or conn.execute(
        "SELECT id FROM organizations ORDER BY id LIMIT 1"
    ).fetchone()["id"]

    if args.list:
        for r in conn.execute(
            "SELECT id, kind, name FROM objects WHERE org_id = ? ORDER BY kind, id", (org,)
        ).fetchall():
            print(f"{r['id']:<28} {r['kind']:<6} {r['name']}")
        return 0

    target = args.target
    if target is None:
        row = conn.execute(
            "SELECT id, name FROM objects WHERE org_id = ? AND kind = 'group'"
            " ORDER BY id LIMIT 1",
            (org,),
        ).fetchone()
        if row is None:
            raise SystemExit("库里没有班级对象，先用 --list 看看")
        target = row["id"]
        print(f"（未指定 --target，自动选第一个班级：{row['name']}）")
    target_id = _resolve_target(conn, org, target)
    tname = conn.execute("SELECT name, kind FROM objects WHERE id = ?", (target_id,)).fetchone()

    term = conn.execute(
        "SELECT start_date, end_date FROM terms WHERE org_id = ? ORDER BY start_date LIMIT 1",
        (org,),
    ).fetchone()
    if term is None:
        raise SystemExit("库里没有学期（terms），无法判断可排区间")
    d0 = date.fromisoformat(args.d0) if args.d0 else date.fromisoformat(term["start_date"])
    d1 = date.fromisoformat(args.d1) if args.d1 else date.fromisoformat(term["end_date"])
    # 演示脚本默认只看两周，整学期会把终端刷满
    if args.d0 is None and args.d1 is None:
        d1 = date.fromordinal(d0.toordinal() + 13)

    print("=" * 62)
    print(f"找空位：{tname['name']}（{tname['kind']}）  连续 {args.duration} 节")
    print(f"区间：{d0.isoformat()} ~ {d1.isoformat()}")
    print("=" * 62)

    got = find_slots(
        conn, org, target=target_id, date_range=(d0, d1),
        duration=args.duration, limit=args.limit,
    )
    if not got:
        print("这段时间没有「连续 N 节全空」的位置。（这是正常结果，不是报错）")
        return 0

    print(f"{'日期':<12}{'周几':<6}{'节次':<14}{'代价':<8}理由")
    print("-" * 62)
    for c in got:
        wd = WEEKDAY[c["day_of_week"] - 1]
        periods = "、".join(f"第{p}节" for p in c["periods"])
        print(f"{c['date']:<12}{wd:<6}{periods:<14}{c['cost']:<8.2f}{'／'.join(c['reasons'])}")
    print("-" * 62)
    print(f"共 {len(got)} 个候选（代价升序；本脚本只读，不写库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
