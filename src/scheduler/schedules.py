"""日程的读写（周期日程 / 一次性活动）。

设计意图：
    周期日程（课表）与一次性活动（事件）在**存储层**分成两张扩展表，
    但在**读写层**刻意共用同一套参数：title / 参与者 / 资源。
    这么做是为了避免写出两套逻辑——两套逻辑正是"排课看不见活动"的根源。
    上层（occupancy）把两者都展开成「(日期, 当天掩码)」，之后一切统一。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .db import new_id


@dataclass
class NewSchedule:
    """创建日程的统一入参（周期与一次性共用）。"""

    title: str
    org_id: str
    created_by: str | None = None
    # role -> [object_id]，role ∈ target / teacher / participant
    participants: dict[str, list[str]] | None = None
    resources: list[str] | None = None


def create_periodic(
    conn: sqlite3.Connection,
    spec: NewSchedule,
    *,
    slot_bitmap: int,
    week_parity: int = 0,
    valid_from: str,
    valid_to: str,
) -> str:
    """创建周期日程（如某班某教师某科目的每周课表）。

    slot_bitmap: 一周掩码（整数），由 slots.bit() 组合而来
    week_parity: 0=每周 / 1=仅单周 / 2=仅双周
    """
    if week_parity not in (0, 1, 2):
        raise ValueError("week_parity 必须是 0/1/2")
    sid = new_id("sch")
    conn.execute(
        "INSERT INTO schedules(id, org_id, kind, title, created_by)"
        " VALUES (?,?, 'periodic', ?,?)",
        (sid, spec.org_id, spec.title, spec.created_by),
    )
    conn.execute(
        "INSERT INTO schedule_periodic(schedule_id, week_parity, slot_bitmap, valid_from, valid_to)"
        " VALUES (?,?,?,?,?)",
        (sid, week_parity, format(slot_bitmap, "x"), valid_from, valid_to),
    )
    _write_links(conn, sid, spec)
    conn.commit()
    return sid


def create_once(
    conn: sqlite3.Connection,
    spec: NewSchedule,
    *,
    start_at: str,
    end_at: str,
    group_id: str | None = None,
) -> str:
    """创建一次性活动。

    跨天活动：存**两条** start/end 不同的记录、共享同一个 group_id，
    由前端按 group_id 聚合展示（这样每一天的掩码仍然是干净的）。
    """
    sid = new_id("sch")
    conn.execute(
        "INSERT INTO schedules(id, org_id, kind, title, created_by)"
        " VALUES (?,?, 'once', ?,?)",
        (sid, spec.org_id, spec.title, spec.created_by),
    )
    conn.execute(
        "INSERT INTO schedule_once(schedule_id, start_at, end_at, group_id)"
        " VALUES (?,?,?,?)",
        (sid, start_at, end_at, group_id),
    )
    _write_links(conn, sid, spec)
    conn.commit()
    return sid


def _write_links(conn: sqlite3.Connection, schedule_id: str, spec: NewSchedule) -> None:
    for role, ids in (spec.participants or {}).items():
        if role not in ("target", "teacher", "participant"):
            raise ValueError(f"非法参与者角色: {role}")
        for oid in ids:
            conn.execute(
                "INSERT OR IGNORE INTO schedule_participants(schedule_id, object_id, role)"
                " VALUES (?,?,?)",
                (schedule_id, oid, role),
            )
    for rid in spec.resources or []:
        conn.execute(
            "INSERT OR IGNORE INTO schedule_resources(schedule_id, resource_id)"
            " VALUES (?,?)",
            (schedule_id, rid),
        )


def get_schedule(conn: sqlite3.Connection, schedule_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM v_schedule_full WHERE id = ?", (schedule_id,)
    ).fetchone()


def list_schedules(
    conn: sqlite3.Connection,
    org_id: str,
    *,
    kind: str | None = None,
    object_id: str | None = None,
) -> list[sqlite3.Row]:
    """列出日程。可按类型过滤，或按"与某对象相关"过滤（含其所属组的日程）。"""
    sql = (
        "SELECT DISTINCT s.* FROM v_schedule_full s"
        " LEFT JOIN schedule_participants p ON p.schedule_id = s.id"
        " LEFT JOIN schedule_resources r ON r.schedule_id = s.id"
        " WHERE s.org_id = ?"
    )
    params: list = [org_id]
    if kind:
        sql += " AND s.kind = ?"
        params.append(kind)
    if object_id:
        # 对象自身 + 其所属的所有组：一条边就能覆盖"班级课表也算学生的课表"
        groups = [
            r["ancestor_id"]
            for r in conn.execute(
                "SELECT ancestor_id FROM object_edges WHERE descendant_id = ?",
                (object_id,),
            ).fetchall()
        ]
        ids = [object_id] + groups
        sql += f" AND (p.object_id IN ({','.join('?' * len(ids))}) OR r.resource_id IN ({','.join('?' * len(ids))}))"
        params.extend(ids)
        params.extend(ids)
    return conn.execute(sql, params).fetchall()


def delete_schedule(conn: sqlite3.Connection, schedule_id: str) -> None:
    conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    conn.commit()


def touch(conn: sqlite3.Connection, schedule_id: str) -> None:
    conn.execute(
        "UPDATE schedules SET updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(timespec="seconds"), schedule_id),
    )
