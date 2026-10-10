"""L5 / T11 试用组织脱敏数据生成脚本。

用途（答辩口径）：
  1. 生成一套「试用组织」（trial）数据，供 10/8–10/18 教师试用：在这个沙箱里随便试，
     不会污染真实数据；对外展示时也不会泄露任何真实学校信息。
  2. 名称全部**明显虚构 + 中性**：组织「试用学校」、班级「示例N班」、教师「示例教师XX」、
     学生「示例学生XX-XX」、教室「示例教室XX」——不含真实校名、真实年级、真实人名。
  3. 同时生成一个「脱敏盐」data/scrub.salt（随机、不入库）：将来若需要把真实数据脱敏，
     用 HMAC(salt, 真名) 得到**稳定**假名（同一真名→同一假名，跨会话可对齐；盐不泄露则不可反推）。
  4. 自带自检（--check / --dump），配合 grep 可机械验证「无真实信息残留」。

与演示组织（demo，scripts/reset_demo.py）的区别：
  - demo 用 seed_demo 默认名「高一(1)班」，冲突数 23（已写进报名材料，勿动）；
  - trial 是**另一套独立数据**，org_id 不同、规模更大、名称全部「示例」化，
    冲突数是本脚本真跑出来的，与 demo 的 23 无关。

运行：
    python scripts/build_trial_org.py             # 重建 data/trial.db + 自检
    python scripts/build_trial_org.py --check      # 只对现有库跑自检
    python scripts/build_trial_org.py --dump       # 导出全部文本字段（供 grep）
    python scripts/build_trial_org.py --scrub 王小明  # 用脱敏盐稳定脱敏一个名字
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler.authz import grant_role  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.object_graph import (  # noqa: E402
    add_edge,
    create_object,
    create_organization,
    create_user,
)
from scheduler.occupancy import create_term, scan  # noqa: E402
from scheduler.schedules import NewSchedule, create_once, create_periodic  # noqa: E402
from scheduler.slots import bit, load_slot_config, setup_default_slots  # noqa: E402

# ---------------------------------------------------------------------------
# 常量与路径（锚定仓库根，不受当前工作目录影响）
# ---------------------------------------------------------------------------
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TRIAL_DB = os.path.join(ROOT, "data", "trial.db")
SALT_PATH = os.path.join(ROOT, "data", "scrub.salt")

ORG_NAME = "试用学校"
TERM_NAME = "示例学期"
# 🔴 2026-10-08队长决策：试用由 10/8 延期至 **10/12**，故学期起点随之挪到试用首周。
# 原值2026-10-05 会让整个 10/12–10/18 试用期落在「学期第 2 周」，
# 而冲突课表造在第 1 周 → 实测 10/12 起只剩 **1 处冲突**（10/05 起才有 32 处）。
# 1 处冲突撑不起「AI 求解一键消解」的说服力，且老师不会去点消解 → 采不到完成率。
TERM_START = "2026-10-12"  # 周一：让10/12–10/18 整个试用期都落在学期第 1 周内

CLASSES = 6
STUDENTS_PER_CLASS = 30
TEACHERS = 12
ROOMS = 8
PERIODS_PER_DAY = 10
DAYS = 5  # 周一~周五

ADMIN_LOGIN = "trial_admin"
ADMIN_PASSWORD = "trial123"
TEACHER_PASSWORD = "123456"

# 每个班排这几门课（通用学科名，非真实信息）。
# 🔴 刻意避开周三第 1–2 节：让「示例活动」只撞故意造的冲突演示课，不撞常规课，
#    保证冲突数少而可控（教师能看懂、一键消解跑得动），叙事与演示组织 demo 一致。
SUBJECTS = [("语文", 1, 1), ("数学", 1, 2), ("英语", 2, 1), ("物理", 3, 3), ("化学", 4, 1)]

# 自检「泄漏词表」：本项目真实上下文的敏感词。任一命中 = 脱敏失败。
# （「年级」也列入：trial 数据用「示例N班」，不该出现任何「年级」字样）
LEAK_WORDS = [
    "嘉祥", "锦江", "八年级", "七年级", "九年级",
    "初一", "初二", "初三", "高一", "高二", "高三", "年级",
]


# ---------------------------------------------------------------------------
# 名称体系：全部明显虚构 + 中性
# ---------------------------------------------------------------------------
def cls_name(i: int) -> str:
    return f"示例{i}班"


def teacher_name(k: int) -> str:
    return f"示例教师{k:02d}"


def student_name(i: int, j: int) -> str:
    return f"示例学生{i:02d}-{j:02d}"


def room_name(r: int) -> str:
    return f"示例教室{r:02d}"


def teacher_login(k: int) -> str:
    return f"trial_t{k:02d}"


# ---------------------------------------------------------------------------
# 脱敏盐（将来真实数据脱敏用）
# ---------------------------------------------------------------------------
def ensure_salt() -> str:
    """读或生成脱敏盐（hex）。盐不入库、不入 git。"""
    if os.path.exists(SALT_PATH):
        salt = open(SALT_PATH, "r", encoding="utf-8").read().strip()
        if re.fullmatch(r"[0-9a-f]{64}", salt):
            return salt
    salt = secrets.token_hex(32)
    os.makedirs(os.path.dirname(SALT_PATH), exist_ok=True)
    with open(SALT_PATH, "w", encoding="utf-8") as f:
        f.write(salt + "\n")
    return salt


def scrub_name(raw: str, salt_hex: str) -> str:
    """HMAC(salt, 真名) → 稳定假名。同一真名永远得到同一假名，盐不泄露则不可反推。"""
    digest = hmac.new(bytes.fromhex(salt_hex), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"示例{digest[:6]}"


# ---------------------------------------------------------------------------
# 生成试用组织
# ---------------------------------------------------------------------------
def build() -> dict:
    """重建 data/trial.db，返回规模与冲突统计。"""
    # 删旧库（含 -wal/-shm 残留）
    for suffix in ("", "-wal", "-shm"):
        p = TRIAL_DB + suffix
        if os.path.exists(p):
            os.remove(p)

    conn = connect(TRIAL_DB)
    init_db(conn)

    org_id, root_id = create_organization(conn, ORG_NAME)
    setup_default_slots(conn, org_id, periods_per_day=PERIODS_PER_DAY, days=DAYS)

    start = TERM_START
    end = (date.fromisoformat(start) + timedelta(days=20 * 7 - 1)).isoformat()
    create_term(conn, org_id, TERM_NAME, start, end)

    # 管理员
    admin_obj = create_object(conn, org_id, "user", "示例管理员")
    add_edge(conn, root_id, admin_obj)
    create_user(conn, admin_obj, ADMIN_LOGIN, ADMIN_PASSWORD, role="admin")

    # 班级 + 学生
    class_ids: list[str] = []
    student_ids: list[str] = []
    for i in range(1, CLASSES + 1):
        gid = create_object(conn, org_id, "group", cls_name(i))
        add_edge(conn, root_id, gid)
        class_ids.append(gid)
        for j in range(1, STUDENTS_PER_CLASS + 1):
            sid = create_object(conn, org_id, "user", student_name(i, j))
            add_edge(conn, gid, sid)
            student_ids.append(sid)

    # 教师
    teacher_ids: list[str] = []
    for k in range(1, TEACHERS + 1):
        tid = create_object(conn, org_id, "user", teacher_name(k))
        add_edge(conn, root_id, tid)
        create_user(conn, tid, teacher_login(k), TEACHER_PASSWORD, role="teacher")
        teacher_ids.append(tid)

    # 教室
    room_ids: list[str] = []
    for r in range(1, ROOMS + 1):
        rid = create_object(conn, org_id, "resource", room_name(r), capacity=50)
        add_edge(conn, root_id, rid)
        room_ids.append(rid)

    # 🔴 2026-10-10 修复第 12 处静默失效：教师账号登录后「授权组为空、默认组 None」。
    # 根因：脚本只create_user 建账号，从未调grant_role 写 group_roles 授权行。
    # 表现：管理员能登录（role='admin' 全组织短路，看不出问题），
    #      但 12 个教师 trial_t01..t12 全部拿到空授权列表 → 试用老师看到空系统、
    #      组切换器空白、一键消解按钮拿不到 group → **试用当天才会发现**。
    # 授权口径（docs/06）：教师对自己实际任教的班级有 'view'；
    # 排课相关给 'own'（可提议改课）。这里按「任教关系」精确授权，不给全组织。
    #   tid -> 他教的 cid 集合（与下面 create_periodic 的分配口径完全一致）
    teacher_teaching: dict[str, list[str]] = {tid: [] for tid in teacher_ids}
    for ci, cid in enumerate(class_ids):
        for si, (subj, dow, idx) in enumerate(SUBJECTS):
            tid = teacher_ids[(ci * len(SUBJECTS) + si) % TEACHERS]
            if cid not in teacher_teaching[tid]:
                teacher_teaching[tid].append(cid)
    # 冲突演示课涉及的两位教师也授权到对应班级，否则他们看不到要消解的冲突
    teacher_teaching[teacher_ids[3]].append(class_ids[0])
    if len(class_ids) > 1:
        teacher_teaching[teacher_ids[7]].append(class_ids[1])

    for tid in teacher_ids:
        u = conn.execute("SELECT id FROM users WHERE object_id=?", (tid,)).fetchone()
        uid = u["id"] if u else None
        if not uid:
            raise RuntimeError(f"教师对象 {tid} 没有对应的 users 行，无法授权")
        for cid in teacher_teaching[tid]:
            grant_role(conn, org_id, cid, uid, "own")

    # 课表：每班 5 门课，教师轮换分配（同一节次用不同教师，避免假冲突）
    for ci, cid in enumerate(class_ids):
        for si, (subj, dow, idx) in enumerate(SUBJECTS):
            tid = teacher_ids[(ci * len(SUBJECTS) + si) % TEACHERS]
            create_periodic(
                conn,
                NewSchedule(
                    title=f"{cls_name(ci + 1)} {subj}",
                    org_id=org_id,
                    created_by=admin_obj,
                    participants={"target": [cid], "teacher": [tid]},
                    resources=[room_ids[ci % ROOMS]],
                ),
                slot_bitmap=bit(dow, idx),
                week_parity=0,
                valid_from=start,
                valid_to=end,
            )

    # 故意制造两类真实冲突，供试用教师练「一键消解」：
    # ① 教师撞课：同一位教师周三第 1 节被排到两个班
    a = create_periodic(
        conn,
        NewSchedule(
            title=f"{cls_name(1)} 物理（冲突演示）",
            org_id=org_id,
            created_by=admin_obj,
            participants={"target": [class_ids[0]], "teacher": [teacher_ids[3]]},
        ),
        slot_bitmap=bit(3, 1),
        valid_from=start,
        valid_to=end,
    )
    b = create_periodic(
        conn,
        NewSchedule(
            title=f"{cls_name(2)} 物理（冲突演示）",
            org_id=org_id,
            created_by=admin_obj,
            participants={"target": [class_ids[1]], "teacher": [teacher_ids[3]]},
        ),
        slot_bitmap=bit(3, 1),
        valid_from=start,
        valid_to=end,
    )
    # ② 活动撞课：示例活动占周三第 1–2 节，只撞示例1班的冲突演示物理课（单班活动）
    d3 = date.fromisoformat(start) + timedelta(days=2)
    create_once(
        conn,
        NewSchedule(
            title="示例活动",
            org_id=org_id,
            created_by=admin_obj,
            participants={"participant": [class_ids[0]]},
            resources=[room_ids[0]],
        ),
        start_at=f"{d3.isoformat()}T08:00",
        end_at=f"{d3.isoformat()}T09:40",
    )

    conn.commit()

    cfg = load_slot_config(conn, org_id)
    d0 = date.fromisoformat(start)
    d6 = d0 + timedelta(days=6)
    n_objects = conn.execute("SELECT COUNT(*) AS c FROM objects").fetchone()["c"]
    n_conflicts = scan(conn, org_id, d0, d6, cfg=cfg).count

    conn.close()
    return {
        "org_id": org_id,
        "classes": CLASSES,
        "students": len(student_ids),
        "teachers": TEACHERS,
        "rooms": ROOMS,
        "objects": n_objects,
        "conflicts": n_conflicts,
        "term_start": start,
        "term_end": end,
        "admin_login": ADMIN_LOGIN,
    }


# ---------------------------------------------------------------------------
# 自检：把「文本字段」全部导出，供正则 / grep 验证
# ---------------------------------------------------------------------------
def _dump_lines(conn: sqlite3.Connection) -> list[str]:
    """导出所有可能携带真实信息的文本字段，一行一个「表|字段|值」。"""
    lines: list[str] = []
    queries = [
        ("organizations", "name", "SELECT name FROM organizations"),
        ("objects", "name", "SELECT name FROM objects"),
        ("users", "login", "SELECT login FROM users"),
        ("schedules", "title", "SELECT title FROM schedules"),
        ("terms", "name", "SELECT name FROM terms"),
        ("period_slots", "name", "SELECT name FROM period_slots WHERE name IS NOT NULL"),
        ("calendar_exceptions", "note", "SELECT note FROM calendar_exceptions WHERE note IS NOT NULL"),
    ]
    for table, field, sql in queries:
        for row in conn.execute(sql):
            lines.append(f"{table}|{field}|{row[0]}")
    return lines


def check(db_path: str = TRIAL_DB) -> tuple[bool, list[str]]:
    """对库跑自检。返回 (是否通过, 报告行)。"""
    if not os.path.exists(db_path):
        return False, [f"FAIL 库不存在：{db_path}"]

    conn = connect(db_path)
    lines = _dump_lines(conn)
    conn.close()

    report: list[str] = []
    ok = True

    # ① 泄漏检查：真实敏感词 0 命中
    blob = "\n".join(lines)
    hits = [(w, blob.count(w)) for w in LEAK_WORDS if w in blob]
    if hits:
        ok = False
        for w, n in hits:
            report.append(f"FAIL 泄漏词命中：{w} ×{n}")
    else:
        report.append("PASS 泄漏词表 0 命中（嘉祥/锦江/各年级段 均未出现）")

    # ② 正向虚构确认：各类名称符合「示例」规范
    def _names(table: str) -> list[str]:
        return [ln.split("|")[2] for ln in lines if ln.startswith(f"{table}|")]

    obj_names = _names("objects")
    # 按前缀粗分（示例教师/学生/教室/班/管理员）
    class_names = [n for n in obj_names if re.fullmatch(r"示例\d+班", n)]
    teacher_names = [n for n in obj_names if re.fullmatch(r"示例教师\d+", n)]
    student_names = [n for n in obj_names if re.fullmatch(r"示例学生\d+-\d+", n)]
    room_names = [n for n in obj_names if re.fullmatch(r"示例教室\d+", n)]
    admin_names = [n for n in obj_names if n == "示例管理员"]
    # 白名单：示例N班 / 示例教师XX / 示例学生XX-XX / 示例教室XX / 示例管理员
    #         + 「试用学校」（create_organization 自动建的根组名 = 组织名，中性合规）
    _ok_name = re.compile(r"示例(\d+班|教师\d+|学生\d+-\d+|教室\d+|管理员)|试用学校")
    other_names = [n for n in obj_names if not _ok_name.fullmatch(n)]

    org_names = _names("organizations")
    logins = _names("users")

    if org_names != [ORG_NAME]:
        ok = False
        report.append(f"FAIL 组织名应为「{ORG_NAME}」，实际 {org_names}")
    else:
        report.append(f"PASS 组织名 =「{ORG_NAME}」")

    if len(class_names) != CLASSES:
        ok = False
        report.append(f"FAIL 班级数 {len(class_names)} ≠ {CLASSES}")
    else:
        report.append(f"PASS 班级 {CLASSES} 个，全部匹配「示例N班」")

    if not teacher_names or len(teacher_names) != TEACHERS:
        ok = False
        report.append(f"FAIL 教师名不规范，共 {len(teacher_names)}/{TEACHERS}")
    else:
        report.append(f"PASS 教师 {len(teacher_names)} 位，全部匹配「示例教师XX」")

    if not student_names or len(student_names) != CLASSES * STUDENTS_PER_CLASS:
        ok = False
        report.append(f"FAIL 学生名不规范，共 {len(student_names)}")
    else:
        report.append(f"PASS 学生 {len(student_names)} 名，全部匹配「示例学生XX-XX」")

    if not room_names or len(room_names) != ROOMS:
        ok = False
        report.append(f"FAIL 教室名不规范，共 {len(room_names)}/{ROOMS}")
    else:
        report.append(f"PASS 教室 {len(room_names)} 间，全部匹配「示例教室XX」")

    if not admin_names:
        ok = False
        report.append("FAIL 缺「示例管理员」")
    else:
        report.append("PASS 管理员 =「示例管理员」")

    if other_names:
        ok = False
        report.append(f"FAIL 存在不符合「示例」规范的对象名：{sorted(set(other_names))}")
    else:
        report.append("PASS 无游离对象名（全部对象名均符合「示例」规范）")

    bad_logins = [ln for ln in logins if not re.fullmatch(r"trial_[a-z0-9_]+", ln)]
    if bad_logins:
        ok = False
        report.append(f"FAIL 登录名不规范：{bad_logins}")
    else:
        report.append(f"PASS 登录名 {len(logins)} 个，全部匹配「trial_*」")

    return ok, report


def _print_check(ok: bool, report: list[str]) -> int:
    print("=" * 56)
    print("试用组织脱敏自检" + ("　✅ 通过" if ok else "　❌ 未通过"))
    print("=" * 56)
    for line in report:
        print("  " + line)
    print("=" * 56)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="L5 试用组织脱敏数据")
    ap.add_argument("--check", action="store_true", help="只对现有库跑自检")
    ap.add_argument("--dump", action="store_true", help="导出全部文本字段（供 grep）")
    ap.add_argument("--scrub", metavar="NAME", help="用脱敏盐稳定脱敏一个名字")
    args = ap.parse_args()

    if args.scrub:
        salt = ensure_salt()
        print(f"真名「{args.scrub}」→ 脱敏假名「{scrub_name(args.scrub, salt)}」")
        print(f"（盐已保存至 {SALT_PATH}，同一真名永远得到同一假名）")
        return 0

    if args.dump:
        if not os.path.exists(TRIAL_DB):
            print(f"库不存在：{TRIAL_DB}", file=sys.stderr)
            return 1
        conn = connect(TRIAL_DB)
        for ln in _dump_lines(conn):
            print(ln)
        conn.close()
        return 0

    if args.check:
        ok, report = check()
        return _print_check(ok, report)

    # 默认：重建 + 自检
    ensure_salt()
    info = build()
    print("=" * 56)
    print("试用组织（trial）数据已生成")
    print("=" * 56)
    print(f"库文件：{TRIAL_DB}")
    print(f"组织：{ORG_NAME}（org_id={info['org_id']}）")
    print(
        f"规模：{info['classes']} 个班 · {info['teachers']} 位教师 · "
        f"{info['students']} 名学生 · {info['rooms']} 间教室"
    )
    print(f"对象总数：{info['objects']}")
    print(f"学期：{info['term_start']} ~ {info['term_end']}")
    print(f"管理员登录：{info['admin_login']}")
    print(f"扫描第 1 周冲突：{info['conflicts']} 处（与演示组织的 23 无关，独立数据）")
    print()

    ok, report = check()
    return _print_check(ok, report)


if __name__ == "__main__":
    raise SystemExit(main())
