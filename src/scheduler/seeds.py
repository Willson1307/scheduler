"""演示 / 试用组织种子数据。

用途有三个：
    1. 队员本地开发时一条命令就能拿到可跑的数据，不用手工录；
    2. 现场演示：断网环境下也有完整场景可展示；
    3. **轻量化试用**：给教师建一个"试用组织"，让他们在这个沙箱里随便试，
       不会污染真实数据（对应队长提出的 G3 试用方案）。

with_conflict=True 时会**故意制造一处教师撞课**，
这样打开冲突检测页就能立刻看到效果，便于演示与验收。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .db import init_db, new_id
from .object_graph import add_edge, create_object, create_organization, create_user
from .occupancy import create_term
from .schedules import NewSchedule, create_once, create_periodic
from .slots import bit, setup_default_slots


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def seed_demo(
    conn: sqlite3.Connection,
    *,
    org_name: str = "试用学校",
    classes: int = 3,
    students_per_class: int = 8,
    teachers: int = 9,  # 默认保证「每班每科一位专属教师」，避免演示数据里出现一堆假冲突
    rooms: int = 4,
    admin_login: str = "admin",
    admin_password: str = "admin123",
    term_start: str | None = None,
    with_conflict: bool = True,
) -> dict:
    """写入一套完整的演示数据，返回各类 id 便于后续操作。"""
    init_db(conn)

    org_id, root_id = create_organization(conn, org_name)
    cfg = setup_default_slots(conn, org_id, periods_per_day=10, days=5)

    start = term_start or _monday_of(date.today()).isoformat()
    end = (date.fromisoformat(start) + timedelta(days=20 * 7 - 1)).isoformat()
    create_term(conn, org_id, "2026-2027 学年第一学期", start, end)

    # --- 管理员（挂在根组下，便于登录） ---
    admin_obj = create_object(conn, org_id, "user", "系统管理员")
    add_edge(conn, root_id, admin_obj)
    create_user(conn, admin_obj, admin_login, admin_password, role="admin")

    # --- 班级 + 学生 + 教师 ---
    class_ids: list[str] = []
    teacher_ids: list[str] = []
    student_ids: list[str] = []

    for i in range(classes):
        gid = create_object(conn, org_id, "group", f"高一({i + 1})班")
        add_edge(conn, root_id, gid)
        class_ids.append(gid)
        for j in range(students_per_class):
            sid = create_object(conn, org_id, "user", f"学生{i + 1}-{j + 1:02d}")
            add_edge(conn, gid, sid)
            student_ids.append(sid)

    for k in range(teachers):
        tid = create_object(conn, org_id, "user", f"教师{k + 1:02d}")
        add_edge(conn, root_id, tid)
        create_user(conn, tid, f"t{k + 1:02d}", "123456", role="teacher")
        teacher_ids.append(tid)

    room_ids: list[str] = []
    for r in range(rooms):
        rid = create_object(conn, org_id, "resource", f"教室{r + 1:02d}", capacity=50)
        add_edge(conn, root_id, rid)
        room_ids.append(rid)

    # --- 课表：每个班排语文/数学/英语，各由固定教师授课 ---
    # 教师按下标轮换分配，保证同一位教师不会同时被排在两个班（否则全是假冲突）
    subjects = [("语文", 1, 1), ("数学", 1, 2), ("英语", 2, 1)]
    schedule_ids: list[str] = []
    for ci, cid in enumerate(class_ids):
        for si, (name, dow, idx) in enumerate(subjects):
            tid = teacher_ids[(ci * len(subjects) + si) % len(teacher_ids)]
            sid = create_periodic(
                conn,
                NewSchedule(
                    title=f"高一({ci + 1})班 {name}",
                    org_id=org_id,
                    created_by=admin_obj,
                    participants={"target": [cid], "teacher": [tid]},
                    resources=[room_ids[ci % len(room_ids)]],
                ),
                slot_bitmap=bit(dow, idx),
                week_parity=0,
                valid_from=start,
                valid_to=end,
            )
            schedule_ids.append(sid)

    # --- 故意制造冲突：同一位教师在周一第 1 节被安排在两个班 ---
    conflict_pair = None
    if with_conflict and len(class_ids) >= 2 and teachers >= 2:
        a = create_periodic(
            conn,
            NewSchedule(
                title="（冲突演示）高一(1)班 物理",
                org_id=org_id,
                created_by=admin_obj,
                participants={
                    "target": [class_ids[0]],
                    "teacher": [teacher_ids[3 % teachers]],
                },
            ),
            slot_bitmap=bit(3, 1),  # 周三第 1 节
            valid_from=start,
            valid_to=end,
        )
        b = create_periodic(
            conn,
            NewSchedule(
                title="（冲突演示）高一(2)班 物理",
                org_id=org_id,
                created_by=admin_obj,
                participants={
                    "target": [class_ids[1]],
                    "teacher": [teacher_ids[3 % teachers]],
                },
            ),
            slot_bitmap=bit(3, 1),  # 同一时间
            valid_from=start,
            valid_to=end,
        )
        conflict_pair = (a, b)

    # --- 一次性活动：全年级大会，占用周三第 1-2 节（会撞上上面的物理课） ---
    event_id = None
    if with_conflict:
        d3 = date.fromisoformat(start) + timedelta(days=2)  # 学期第 1 周周三
        event_id = create_once(
            conn,
            NewSchedule(
                title="全年级大会",
                org_id=org_id,
                created_by=admin_obj,
                participants={"participant": class_ids},
                resources=[room_ids[0]],
            ),
            start_at=f"{d3.isoformat()}T08:00",
            end_at=f"{d3.isoformat()}T09:40",
            group_id=new_id("evt"),
        )

    conn.commit()
    return {
        "org_id": org_id,
        "root_group_id": root_id,
        "class_ids": class_ids,
        "teacher_ids": teacher_ids,
        "student_ids": student_ids,
        "room_ids": room_ids,
        "schedule_ids": schedule_ids,
        "conflict_pair": conflict_pair,
        "event_id": event_id,
        "term_start": start,
        "term_end": end,
        "admin": {"login": admin_login, "password": admin_password},
        "slot_config": cfg,
    }
