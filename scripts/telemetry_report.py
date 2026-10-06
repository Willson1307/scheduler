"""S14 试用数据报告 + 隐私自检（feat/ops）。

用法：
    python scripts/telemetry_report.py               # 打印四类聚合报告
    python scripts/telemetry_report.py --db <path>   # 指定库（默认 data/trial.db）
    python scripts/telemetry_report.py --assert-clean  # 隐私自检：命中→exit 1

口径唯一来源：docs/08-试用组织数据口径.md。
聚合复用 src/server/telemetry.py 的 aggregate()（单一口径源，与 GET /api/telemetry 同源）。
--assert-clean 对应 docs/21 §4 P1：event_log 无姓名/班级/登录名。

只用 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler.db import connect  # noqa: E402
from server import telemetry  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_DB = os.path.join(ROOT, "data", "trial.db")

# 隐私自检「泄漏词表」：命中即视为可识别文本（对齐 build_trial_org.py 的 LEAK_WORDS）。
LEAK_WORDS = [
    "嘉祥", "锦江", "八年级", "七年级", "九年级",
    "初一", "初二", "初三", "高一", "高二", "高三", "年级",
    "姓名", "登录名", "班级", "学号",
]

#: trial_code 必须是空串或短码 T01..T999。
_TRIAL_CODE_RE = re.compile(r"^$|^T\d{1,3}$")
#: session_id 必须是随机串形态（ASCII，8..64，无中文/无姓名）。
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
#: 中文（CJK）——payload 只放结构化参数，出现中文即疑似姓名/班级/课表内容。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _fmt_seconds(sec) -> str:
    if sec is None:
        return "-"
    s = int(round(sec))
    return f"{s // 60} 分 {s % 60} 秒"


def report(db_path: str) -> dict:
    conn = connect(db_path)
    try:
        return telemetry.aggregate(conn)
    finally:
        conn.close()


def _print_report(agg: dict) -> None:
    print("=" * 60)
    print("试用数据报告（仅统计 org_kind='trial'）")
    print("=" * 60)
    print(f"试用人数         ：{agg['participants']} 人")
    d = agg["duration"]
    print(
        f"平均使用时长     ：单次 {_fmt_seconds(d['avg_session_seconds'])}；"
        f"人均累计 {_fmt_seconds(d['avg_person_seconds'])}"
    )
    print(f"  有效会话 {d['session_count']} 个 / {d['person_count']} 人（≤5 分钟断线规则）")
    c = agg["completion"]
    rate = f"{c['completion_rate'] * 100:.1f}%" if c["completion_rate"] is not None else "-"
    print(f"任务完成率       ：{rate}（成功 {c['success']} / 发起 {c['starts']}，放弃 {c['abandon']}）")
    s = agg["satisfaction"]
    if s["n"]:
        print(f"满意度           ：n={s['n']}，均值 {s['mean']}，≥4 分占比 {s['ge4_ratio'] * 100:.1f}%")
        print(f"  分布：{s['distribution']}")
    else:
        print("满意度           ：暂无数据（n=0）")
    print("=" * 60)


def assert_clean(conn) -> tuple[bool, list[str]]:
    """隐私自检：event_log 无姓名/班级/登录名。返回 (是否通过, 违规行列表)。

    四条规则（命中任一即违规）：
      1. trial_code 必须是空串或短码 T<数字>；
      2. session_id 必须是 ASCII 随机串（无中文）；
      3. payload 不得含中文（中文即疑似姓名/班级/课表内容）；
      4. payload 的键或字符串值不得等于任何登录名（登录名未直写）。
    """
    telemetry.ensure_event_log(conn)
    logins = {r["login"] for r in conn.execute("SELECT login FROM users").fetchall()}

    violations: list[str] = []
    for r in conn.execute(
        "SELECT id, trial_code, session_id, payload FROM event_log"
    ).fetchall():
        problems: list[str] = []
        if not _TRIAL_CODE_RE.match(r["trial_code"]):
            problems.append(f"trial_code={r['trial_code']!r} 非短码")
        if not _SESSION_RE.match(r["session_id"]):
            problems.append(f"session_id={r['session_id']!r} 非随机串")
        raw_payload = r["payload"] or "{}"
        if _CJK_RE.search(raw_payload):
            problems.append("payload 含中文（疑似姓名/班级/课表内容）")
        try:
            obj = json.loads(raw_payload)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in logins or (isinstance(v, str) and v in logins):
                    problems.append(f"payload 含登录名 {k!r}")
                    break
        if problems:
            violations.append(f"{r['id']}: " + "; ".join(problems))
    return (not violations), violations


def _print_clean(ok: bool, violations: list[str]) -> int:
    print("=" * 60)
    print("隐私自检 --assert-clean" + ("　✅ 通过" if ok else "　❌ 未通过"))
    print("=" * 60)
    if violations:
        for v in violations:
            print("  " + v)
    else:
        print("  event_log 抽样全行无姓名/班级/登录名/中文 payload")
    print("=" * 60)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="S14 试用数据报告")
    ap.add_argument("--db", default=DEFAULT_DB, help="数据库路径（默认 data/trial.db）")
    ap.add_argument("--assert-clean", action="store_true",
                    help="隐私自检：event_log 无姓名/班级/登录名（docs/21 §4 P1）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"库不存在：{args.db}", file=sys.stderr)
        return 1

    if args.assert_clean:
        conn = connect(args.db)
        try:
            ok, violations = assert_clean(conn)
        finally:
            conn.close()
        return _print_clean(ok, violations)

    _print_report(report(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
