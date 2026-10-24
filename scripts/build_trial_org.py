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
    python scripts/build_trial_org.py                    # 重建 data/trial.db（共享组织，现场演示用）
    python scripts/build_trial_org.py --per-teacher       # 重建 data/trial.db（一师一组织，12 位老师试用用）
    python scripts/build_trial_org.py --check             # 只对现有库跑自检
    python scripts/build_trial_org.py --dump              # 导出全部文本字段（供 grep）
    python scripts/build_trial_org.py --scrub 王小明  # 用脱敏盐稳定脱敏一个名字

🔴🔴 为什么试用必须用 `--per-teacher`（2026-10-11 队长提问触发的第 13 处设计缺陷）
---------------------------------------------------------------------------
问题：「一个老师把冲突全消了，剩下的老师怎么办？」
共享单组织时答案是**其他老师做不了任务**——消解是真改库，
第一位老师点完一键消解，全组织 32 处冲突归零，后面 11 位老师进来看到的是 0 冲突，
既采不到完成率（分母有start、分子没success），老师也会以为系统坏了。

候选方案与淘汰理由：
  A. 每人一个库文件（trial_t01.db…trial_t12.db）
     ❌ 启动命令要带参数、12 份库各自脱敏检查、报告要写合并逻辑、
        老师拿错文件就登不进自己那份。复杂度换不来收益。
  B. 每位老师试用前跑一次 build_trial_org.py 重置
     ❌ 现场手抖就忘；老师之间无法同时用；等于把数据管理甩给主持人。
  C. 加大冲突数量（32 → 384）让后面的人也有活干
     ❌ 只是把问题推后N 位，���人消完还是归零；且冲突太多老师看不完。
  D. **一师一组织（采用）**
     ✅ 系统本来就按 org_id 硬隔离：scan()、/api/conflicts、/api/resolve
        都经 `resolve_authorized_org()` 定位组织，只认「当前用户有授权的组」。
        给每位老师一个独立组织，消解天然互不影响，**零新增代码**。
     ✅ 仍然只有一个 data/trial.db，启动命令不变、脱敏自检只跑一遍、
        telemetry_report.py 只读一个库（trial_code 天然 T01…T12 不串号）。
     ✅ 顺带演示了「组织级数据隔离」这个真实架构能力，答辩可讲。

残留局限（知情接受）：
  - 每位老师只看到自己 2 个班 60 名学生的数据，规模比真实小。
    这是特性不是缺陷——老师能一眼看完所有冲突，才愿意点消解。
  - event_log 里12 个组织的事件混在一张表，但 trial_code 区分得开，
    且 aggregate() 刻意不按 org 分组（人数/时长/完成率/满意度都跨组织汇总），
    正好就是章程第八条(六)要的「试用人数/时长/完成率/满意度」。
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
#: 一师一组织模式下的组织名模板（试用隔离用，见文件头「为什么试用必须用 --per-teacher」）。
ORG_NAME_PER_TEACHER = "试用学校T{code}"
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
#: 教师初始共用口令：仅在建库时写入，部署后必须立即用
#: deploy/reset_trial_passwords.py 重置为各自随机串（2026-10-23 已执行）。
TEACHER_PASSWORD = "123456"
#: trial_admin 口令 2026-10-24 起不再是明文常量（TRIAL-AUTH-KIT）：
#: 从口令表 data/trial-passwords.txt 读（与 reset_trial_passwords.py 同一来源），
#: 表里没有就报错要求先跑 generate。理由：口令表是唯一事实来源，
#: build 与 reset 共享，避免「库里的口令与发给老师的登录卡不一致」。
_PW_TABLE = os.path.join(ROOT, "data", "trial-passwords.txt")


def admin_password() -> str:
    """从 data/trial-passwords.txt 读 trial_admin 口令；缺失则报错退出。"""
    if os.path.exists(_PW_TABLE):
        for ln in open(_PW_TABLE, encoding="utf-8"):
            parts = ln.split(None, 1)
            if len(parts) == 2 and parts[0] == ADMIN_LOGIN:
                return parts[1].strip()
    raise SystemExit(
        f"FAIL 未找到 {ADMIN_LOGIN} 的口令（{_PW_TABLE}）。\n"
        "请先运行：python deploy/reset_trial_passwords.py generate\n"
        "（口令表 data/ 已 gitignore，明文不入库、不出本机）"
    )

# 每个班排这几门课（通用学科名，非真实信息）。
# 🔴 刻意避开周三第 1–2 节：让「示例活动」只撞故意造的冲突演示课，不撞常规课，
#    保证冲突数少而可控（教师能看懂、一键消解跑得动），叙事与演示组织 demo 一致。
SUBJECTS = [("语文", 1, 1), ("数学", 1, 2), ("英语", 2, 1), ("物理", 3, 3), ("化学", 4, 1)]

#: 一师一组织模式下，每个组织的班级数 / 学生数 / 教室数 / 教师数。
#: 刻意做小：老师要能一眼看完所有冲突才愿意点消解；数据大了反而没人用。
PER_ORG_CLASSES = 2
PER_ORG_STUDENTS = 12
PER_ORG_ROOMS = 2

#: 自检「泄漏词表」：本项目真实上下文的敏感词。任一命中 = 脱敏失败。
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
def _fresh_db(db_path: str) -> sqlite3.Connection:
    """删旧库（含 -wal/-shm 残留）并建空库。"""
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)
    conn = connect(db_path)
    init_db(conn)
    return conn


def build_per_teacher(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """一师一组织：给 12 位老师各建一个独立组织（试用隔离用）。

    🔴 为什么不是「一个组织 12 个教师」（那正是共享单组织的病根）：
      消解是真改库。第一位老师点完一键消解，全组织冲突归零，
      后面 11 位老师进来看到 0 冲突 → 完成率的分母有 start、分子没 success，
      老师还会以为系统坏了。分组织后 scan/resolve 都按 org_id 隔离，互不影响。

    每个组织的内容刻意做小（2 班 × 12 人 / 2 教室 / 1 教师）：
      老师一眼看完 → 愿意点消解 → 才采得到真实体验数据。
    """
    end_d = date.fromisoformat(start) + timedelta(days=6)
    orgs: list[dict] = []

    for k in range(1, TEACHERS + 1):
        code = f"{k:02d}"
        org_name = ORG_NAME_PER_TEACHER.format(code=code)
        org_id, root_id = create_organization(conn, org_name)
        setup_default_slots(conn, org_id, periods_per_day=PERIODS_PER_DAY, days=DAYS)
        create_term(conn, org_id, TERM_NAME, start, end)

        # 该组织专属的 1 位教师（登录名 trial_tNN → 后端推短码 TNN）
        tid = create_object(conn, org_id, "user", teacher_name(k))
        add_edge(conn, root_id, tid)
        create_user(conn, tid, teacher_login(k), TEACHER_PASSWORD, role="teacher")

        class_ids: list[str] = []
        for i in range(1, PER_ORG_CLASSES + 1):
            gid = create_object(conn, org_id, "group", cls_name(i))
            add_edge(conn, root_id, gid)
            class_ids.append(gid)
            for j in range(1, PER_ORG_STUDENTS + 1):
                sid = create_object(conn, org_id, "user", student_name(i, j))
                add_edge(conn, gid, sid)

        room_ids = []
        for r in range(1, PER_ORG_ROOMS + 1):
            rid = create_object(conn, org_id, "resource", room_name(r), capacity=50)
            add_edge(conn, root_id, rid)
            room_ids.append(rid)

        # 🔴 必须授权，否则教师登录后 authorized_groups 为空 → 空系统（第 12 处静默失效）
        u = conn.execute("SELECT id FROM users WHERE object_id=?", (tid,)).fetchone()
        for cid in class_ids:
            grant_role(conn, org_id, cid, u["id"], "own")

        # 常规课：两班各 2 门，错开节次（不制造假冲突）
        for ci, cid in enumerate(class_ids):
            for si, (subj, dow, idx) in enumerate(SUBJECTS[:2]):
                create_periodic(
                    conn,
                    NewSchedule(
                        title=f"{cls_name(ci + 1)} {subj}",
                        org_id=org_id,
                        created_by=tid,
                        participants={"target": [cid], "teacher": [tid]},
                        resources=[room_ids[ci % PER_ORG_ROOMS]],
                    ),
                    slot_bitmap=bit(dow, idx),
                    week_parity=0,
                    valid_from=start,
                    valid_to=end,
                )

        # 故意冲突①：同一位教师同一节被排到两个班（教师撞课）
        #  与上面的常规课错开（周三第 3/4 节），保证冲突数少而可控。
        for ci, cid in enumerate(class_ids):
            create_periodic(
                conn,
                NewSchedule(
                    title=f"{cls_name(ci + 1)} 物理（冲突演示）",
                    org_id=org_id,
                    created_by=tid,
                    participants={"target": [cid], "teacher": [tid]},
                    resources=[room_ids[ci % PER_ORG_ROOMS]],
                ),
                slot_bitmap=bit(3, 3),
                valid_from=start,
                valid_to=end,
            )

        # 故意冲突②：示例活动占周三第 3–4 节，撞本组织两个班的冲突演示课
        #  （一次撞两个班 → 老师点一次消解能看到多组变化，比只撞一个班更有说服力）
        d3 = date.fromisoformat(start) + timedelta(days=2)
        create_once(
            conn,
            NewSchedule(
                title="示例活动",
                org_id=org_id,
                created_by=tid,
                participants={"participant": list(class_ids)},
                resources=[room_ids[0]],
            ),
            start_at=f"{d3.isoformat()}T10:00",
            end_at=f"{d3.isoformat()}T11:40",
        )

        conn.commit()
        cfg = load_slot_config(conn, org_id)
        n = scan(conn, org_id, date.fromisoformat(start), end_d, cfg=cfg).count
        orgs.append({
            "org_id": org_id,
            "root_id": root_id,  # CP-H-S2 甲-2：trial_admin 显式授权用
            "org_name": org_name,
            "teacher_login": teacher_login(k),
            "trial_code": f"T{code}",
            "classes": len(class_ids),
            "students": PER_ORG_CLASSES * PER_ORG_STUDENTS,
            "conflicts": n,
        })

    # 主持人用总管理员：放在**独立**的第 13 个组织，不进任何教师组织
    #（避免 admin 隐式范围混入教师组织——CP-H-S2 修复后 admin 默认只看
    #  自己所属组织，跨组织必须显式授权）
    admin_org, admin_root = create_organization(conn, "试用学校管理员")
    admin_obj = create_object(conn, admin_org, "user", "示例管理员")
    add_edge(conn, admin_root, admin_obj)
    admin_uid = create_user(conn, admin_obj, ADMIN_LOGIN, admin_password(), role="admin")

    # 🔴 CP-H-S2 甲-2 配套（2026-10-19）：修复前 admin 走全库短路，
    # trial_admin 天生能看/管全部 12 个组织；收敛后此能力必须由**显式
    # group_roles 授权**承载——给每个试用组织的根组授 own（沿祖先链向下
    # 继承到该组织全部组）。trial_admin 的监控/审批能力与修复前等价，
    # 但「默认不可见、显式才可见」的安全性质成立。
    # 🔴 副作用提示：无这些授权行的旧 trial.db，trial_admin 只剩自己
    #  的第 13 组织（1 个空组）——重建本库即恢复。
    for o in orgs:
        grant_role(conn, o["org_id"], o["root_id"], admin_uid, "own")

    return {"per_teacher": True, "orgs": orgs}


def build(per_teacher: bool = False) -> dict:
    """重建 data/trial.db，返回规模与冲突统计。

    per_teacher=False（默认）：共享单组织，32 处冲突，供**现场演示 / 多人围观**。
    per_teacher=True：一师一组织（12 个），每位老师的消解互不影响，供**教师试用**。
    """
    conn = _fresh_db(TRIAL_DB)

    if per_teacher:
        term_end = (date.fromisoformat(TERM_START)
                    + timedelta(days=20 * 7 - 1)).isoformat()
        info = build_per_teacher(conn, TERM_START, term_end)
        info["admin_login"] = ADMIN_LOGIN
        conn.close()
        return info

    org_id, root_id = create_organization(conn, ORG_NAME)
    setup_default_slots(conn, org_id, periods_per_day=PERIODS_PER_DAY, days=DAYS)

    start = TERM_START
    end = (date.fromisoformat(start) + timedelta(days=20 * 7 - 1)).isoformat()
    create_term(conn, org_id, TERM_NAME, start, end)

    # 管理员
    admin_obj = create_object(conn, org_id, "user", "示例管理员")
    add_edge(conn, root_id, admin_obj)
    create_user(conn, admin_obj, ADMIN_LOGIN, admin_password(), role="admin")

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
    org_names = _names("organizations")
    logins = _names("users")

    # 🔴 模式自动识别（2026-10-11）：一师一组织模式下有13 个组织（12 教师 + 1 管理员），
    # 各组织规模也不同。原来的固定断言 `org_names != [ORG_NAME]` 会直接误报 FAIL。
    per_teacher = any(re.fullmatch(r"试用学校T\d{2}", n) for n in org_names)
    n_torgs = sum(1 for n in org_names if re.fullmatch(r"试用学校T\d{2}", n))
    if per_teacher:
        exp_classes = n_torgs * PER_ORG_CLASSES
        exp_students = n_torgs * PER_ORG_CLASSES * PER_ORG_STUDENTS
        exp_rooms = n_torgs * PER_ORG_ROOMS
        exp_teachers = TEACHERS
        _ok_name = re.compile(
            r"示例(\d+班|教师\d+|学生\d+-\d+|教室\d+|管理员)|试用学校(T\d{2}|管理员)?"
        )
    else:
        exp_classes, exp_students = CLASSES, CLASSES * STUDENTS_PER_CLASS
        exp_rooms, exp_teachers = ROOMS, TEACHERS
        _ok_name = re.compile(
            r"示例(\d+班|教师\d+|学生\d+-\d+|教室\d+|管理员)|试用学校"
        )
    other_names = [n for n in obj_names if not _ok_name.fullmatch(n)]

    # 按前缀粗分（示例教师/学生/教室/班/管理员）
    class_names = [n for n in obj_names if re.fullmatch(r"示例\d+班", n)]
    teacher_names = [n for n in obj_names if re.fullmatch(r"示例教师\d+", n)]
    student_names = [n for n in obj_names if re.fullmatch(r"示例学生\d+-\d+", n)]
    room_names = [n for n in obj_names if re.fullmatch(r"示例教室\d+", n)]
    admin_names = [n for n in obj_names if n == "示例管理员"]

    if per_teacher:
        report.append(f"PASS 模式识别：一师一组织（{n_torgs} 个教师组织 + 管理员组织）")
        if n_torgs != TEACHERS:
            ok = False
            report.append(f"FAIL 教师组织数 {n_torgs} ≠ {TEACHERS}")
        bad_orgs = [n for n in org_names
                    if not re.fullmatch(r"试用学校T\d{2}|试用学校管理员", n)]
        if bad_orgs:
            ok = False
            report.append(f"FAIL 组织名不规范：{bad_orgs}")
        else:
            report.append("PASS 全部组织名匹配「试用学校TNN」/「试用学校管理员」")
    else:
        if org_names != [ORG_NAME]:
            ok = False
            report.append(f"FAIL 组织名应为「{ORG_NAME}」，实际 {org_names}")
        else:
            report.append(f"PASS 组织名 =「{ORG_NAME}」")

    if len(class_names) != exp_classes:
        ok = False
        report.append(f"FAIL 班级数 {len(class_names)} ≠ {exp_classes}")
    else:
        report.append(f"PASS 班级 {len(class_names)} 个，全部匹配「示例N班」")

    if not teacher_names or len(teacher_names) != exp_teachers:
        ok = False
        report.append(f"FAIL 教师名不规范，共 {len(teacher_names)}/{exp_teachers}")
    else:
        report.append(f"PASS 教师 {len(teacher_names)} 位，全部匹配「示例教师XX」")

    if not student_names or len(student_names) != exp_students:
        ok = False
        report.append(f"FAIL 学生名不规范，共 {len(student_names)}")
    else:
        report.append(f"PASS 学生 {len(student_names)} 名，全部匹配「示例学生XX-XX」")

    if not room_names or len(room_names) != exp_rooms:
        ok = False
        report.append(f"FAIL 教室名不规范，共 {len(room_names)}/{exp_rooms}")
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

    #③ 一师一组织模式的隔离断言：每组织恰好 1 位教师 + 授权非空（否则老师登录看到空系统）
    if per_teacher:
        conn2 = connect(db_path)
        try:
            bad_auth: list[str] = []
            # 只查**教师组织**（名字形如 试用学校TNN）；管理员组织本就没有教师与授权，
            # 2026-10-11 首跑时正是这里误报「试用学校管理员(教师0/授权0)」。
            for o in conn2.execute(
                "SELECT o.org_id, o.name FROM objects o "
                "WHERE o.kind='group' AND o.is_root=1 "
                "AND o.org_id IN (SELECT id FROM organizations WHERE name LIKE '试用学校T%') "
                "ORDER BY o.name"
            ).fetchall():
                n_teacher = conn2.execute(
                    "SELECT COUNT(*) c FROM objects x JOIN users u ON u.object_id=x.id "
                    "WHERE x.org_id=? AND u.role='teacher'", (o["org_id"],)
                ).fetchone()["c"]
                n_grant = conn2.execute(
                    "SELECT COUNT(*) c FROM group_roles WHERE org_id=?", (o["org_id"],)
                ).fetchone()["c"]
                if n_teacher != 1 or n_grant == 0:
                    bad_auth.append(f"{o['name']}(教师{n_teacher}/授权{n_grant})")
            if bad_auth:
                ok = False
                report.append(f"FAIL 组织隔离异常：{bad_auth}")
            else:
                report.append(f"PASS {n_torgs} 个教师组织：各 1 位教师且授权非空")
        finally:
            conn2.close()

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
    ap.add_argument("--per-teacher", action="store_true",
                    help="一师一组织：12 位老师各一个独立组织，消解互不影响（教师试用用）")
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
    info = build(per_teacher=args.per_teacher)
    print("=" * 56)
    print("试用组织（trial）数据已生成"
          + ("　【一师一组织模式】" if info.get("per_teacher") else "　【共享单组织模式】"))
    print("=" * 56)
    print(f"库文件：{TRIAL_DB}")

    if info.get("per_teacher"):
        print(f"组织数：{len(info['orgs'])} 个教师组织 + 1 个管理员组织")
        term_end = (date.fromisoformat(TERM_START)
                    + timedelta(days=20 * 7 - 1)).isoformat()
        print(f"学期：{TERM_START} ~ {term_end}")
        print(f"教师密码：{TEACHER_PASSWORD}（登录名见下表，一人一组织，互不影响）")
        print()
        print(f"{'短码':<6}{'登录名':<14}{'组织':<14}{'班':<4}{'学生':<6}{'第1周冲突'}")
        for o in info["orgs"]:
            print(f"{o['trial_code']:<6}{o['teacher_login']:<14}{o['org_name']:<14}"
                  f"{o['classes']:<4}{o['students']:<6}{o['conflicts']}")
        print()
        print(f"管理员登录：{ADMIN_LOGIN} / <口令见 data/trial-passwords.txt>（独立组织，与教师数据不通）")
        print()
    else:
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
        print("⚠️共享单组织：仅用于现场演示/多人围观。教师试用请用 --per-teacher，")
        print("    否则第一位老师消解后，其余老师看到的冲突会归零。")
        print()

    ok, report = check()
    return _print_check(ok, report)


if __name__ == "__main__":
    raise SystemExit(main())
