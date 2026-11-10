"""B4 消解闭环内核：应用前快照 / 可撤销 / 可固化（卡 rvdL9a，例外 ⑧）。

存在理由（与 b3/b9_kernels 同一条纪律）
---------------------------------------------------------------------
`api_resolve.py` 只做「取参数 → 调这里 → 拼响应」。快照的读/写/回写规则
只能有一份实现，否则「撤销到底还原了什么」会随接口各写一遍而漂移。

🔴 本模块**不改 m2.py 一行**
---------------------------------------------------------------------
`m2.apply_plan()` 是原地 UPDATE：
  · 周期日程 → `schedule_periodic.slot_bitmap`
  · 一次性活动 → `schedule_once.start_at` / `end_at`
（注意：`schedules` 主表**没有** start_at/end_at 列，只有 id/org_id/kind/
 title/created_by/created_at/updated_at；卡面与 AGENTS 例外⑧ 里写的
 `schedules.start_at` 是笔误，起止时刻在 `schedule_once`。）
这是「点一次就不可逆」的根因。修复办法不是改 m2（它被 386 项测试锁定），
而是在调用前存一份全量快照，撤销时按快照逐字段回写。

🔴 为什么存全量而非增量
---------------------------------------------------------------------
消解是迭代的（`max_rounds=24`），每轮改哪几条取决于当时的占用状态，
**事前算不出改动集合**；组织级日程只有几十条，全量快照几 KB。
「简单且不会漏」优先于「省几 KB」——漏一条就是撤销不干净，
而「撤销了但其实没回到原位」正是本项目最不能犯的静默失效。

status 三态（CHECK 锁死，不允许第四种或 NULL）
---------------------------------------------------------------------
  applied   已应用，可 revert
  reverted  已撤销，回写完成；再 revert 幂等（200，不再改数据）
  committed 已固化；再 revert → 409（不许静默忽略）

只用 Python 标准库。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .http_api import ApiError

from scheduler import authz
from scheduler.db import init_db, new_id

#: 合法状态（与 schema.sql 的 CHECK 约束逐项对应）
STATUS_APPLIED = "applied"
STATUS_REVERTED = "reverted"
STATUS_COMMITTED = "committed"
VALID_STATUSES = (STATUS_APPLIED, STATUS_REVERTED, STATUS_COMMITTED)

#: 快照列表默认条数（契约：最近 20 条）
LIST_LIMIT = 20


# ---------------------------------------------------------------------------
# 0. 旧库兜底（第 10 处静默失效防线）
# ---------------------------------------------------------------------------


def ensure_snapshot_table(conn: sqlite3.Connection) -> None:
    """确保 resolve_snapshots 表存在（幂等）。

    与 `occupancy.ensure_period_set_tables` / `b8_kernels.ensure_change_tables`
    同范式：`ensure_database` 只在库文件缺失时 seed，**已存在的 data/*.db
    不会自动补表**，故每次读前调用。schema.sql 是唯一 DDL 来源，杜绝
    「表结构在 schema.sql 与代码里各写一遍」的漂移。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'resolve_snapshots'"
    ).fetchone()
    if row is None:
        init_db(conn)


# ---------------------------------------------------------------------------
# 1. 抓快照 / 写快照
# ---------------------------------------------------------------------------


def capture(conn: sqlite3.Connection, org_id: str) -> dict:
    """抓全量快照：本组织全部周期日程的 slot_bitmap + 全部一次性活动的起止时刻。

    返回可直接 json.dumps 的 dict（payload 结构见 schema.sql 注释）。
    只读，不改任何业务表。
    """
    schedules: list[dict] = []
    for r in conn.execute(
        "SELECT sp.schedule_id AS id, sp.slot_bitmap AS bitmap "
        "FROM schedule_periodic sp "
        "JOIN schedules s ON s.id = sp.schedule_id "
        "WHERE s.org_id = ? ORDER BY sp.schedule_id",
        (org_id,),
    ).fetchall():
        schedules.append(
            {
                "id": r["id"],
                "kind": "periodic",
                "bitmap": r["bitmap"],
                "start_at": None,
                "end_at": None,
            }
        )
    for r in conn.execute(
        "SELECT so.schedule_id AS id, so.start_at AS start_at, so.end_at AS end_at "
        "FROM schedule_once so "
        "JOIN schedules s ON s.id = so.schedule_id "
        "WHERE s.org_id = ? ORDER BY so.schedule_id",
        (org_id,),
    ).fetchall():
        schedules.append(
            {
                "id": r["id"],
                "kind": "once",
                "bitmap": None,
                "start_at": r["start_at"],
                "end_at": r["end_at"],
            }
        )
    return {
        "schedules": schedules,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_snapshot(
    conn: sqlite3.Connection,
    org_id: str,
    user_id: str,
    payload: dict,
    applied_count: int,
    remaining_before: int,
    remaining_after: int,
) -> str:
    """写入一条 status='applied' 的快照行，返回 snapshot_id。"""
    ensure_snapshot_table(conn)
    sid = new_id("snap")
    conn.execute(
        "INSERT INTO resolve_snapshots "
        "(id, org_id, created_by, status, payload, applied_count, "
        " remaining_before, remaining_after) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            org_id,
            user_id,
            STATUS_APPLIED,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            applied_count,
            remaining_before,
            remaining_after,
        ),
    )
    conn.commit()
    return sid


# ---------------------------------------------------------------------------
# 2. 读取
# ---------------------------------------------------------------------------


def get_row(conn: sqlite3.Connection, org_id: str, snapshot_id: str) -> sqlite3.Row:
    """取本组织的快照行；不存在或属于别的组织 → 404（不区分，避免探测）。

    🔴 跨组织一律按「不存在」处理：区分 403/404 会把「这个 id 存在」
    这条信息泄漏给别组织用户。
    """
    ensure_snapshot_table(conn)
    row = conn.execute(
        "SELECT * FROM resolve_snapshots WHERE id = ? AND org_id = ?",
        (snapshot_id, org_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, f"快照不存在：{snapshot_id}（或不属于当前组织）")
    return row


def list_recent(conn: sqlite3.Connection, org_id: str, limit: int = LIST_LIMIT) -> list[dict]:
    """最近 N 条快照（created_at 倒序，同秒按插入顺序倒序保证确定性）。

    🔴 created_at 是 `datetime('now')`（秒级精度），同一秒内连点两次会并列。
    原先用 `id DESC` 破并列 —— 而 id 是 uuid 前缀，**顺序随机**，
    表现为「刚做的那条不在最前」。改用 rowid（插入顺序）作次级键。
    """
    ensure_snapshot_table(conn)
    rows = conn.execute(
        "SELECT rs.id, rs.created_at, rs.status, rs.applied_count, "
        "       rs.remaining_before, rs.remaining_after, rs.created_by, "
        "       u.login AS created_by_login "
        "FROM resolve_snapshots rs "
        "LEFT JOIN users u ON u.id = rs.created_by "
        "WHERE rs.org_id = ? "
        "ORDER BY rs.created_at DESC, rs.rowid DESC LIMIT ?",
        (org_id, limit),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "created_by_name": r["created_by_login"] or r["created_by"],
            "created_at": r["created_at"],
            "status": r["status"],
            "applied_count": r["applied_count"],
            "remaining_before": r["remaining_before"],
            "remaining_after": r["remaining_after"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 3. 回写（撤销）
# ---------------------------------------------------------------------------


def _restore(conn: sqlite3.Connection, payload: dict) -> tuple[int, int]:
    """按 payload 逐字段回写。返回 (实际回写行数, 目标行已不存在的条数)。

    行已不存在（快照之后被删掉的日程）不报错也不静默忽略：计入 missing，
    由调用方放进响应体，让「撤销没还原干净」这件事可见。
    """
    restored = 0
    missing = 0
    for item in payload.get("schedules") or []:
        sid = item.get("id")
        if not sid:
            continue
        if item.get("kind") == "periodic":
            cur = conn.execute(
                "UPDATE schedule_periodic SET slot_bitmap = ? WHERE schedule_id = ?",
                (item.get("bitmap"), sid),
            )
        else:
            cur = conn.execute(
                "UPDATE schedule_once SET start_at = ?, end_at = ? WHERE schedule_id = ?",
                (item.get("start_at"), item.get("end_at"), sid),
            )
        if cur.rowcount:
            restored += 1
        else:
            missing += 1
    conn.commit()
    return restored, missing


def revert(conn: sqlite3.Connection, org_id: str, snapshot_id: str) -> dict:
    """撤销一条快照。

    三态语义：
      · committed → 409（已固化，需管理员先解锁；不许静默忽略）
      · reverted  → 幂等 200，**不写任何数据**（第二次撤销不能把数据改回去）
      · applied   → 按 payload 回写，置 reverted

    返回 {"already_reverted": bool, "restored": int, "missing": int}
    """
    row = get_row(conn, org_id, snapshot_id)
    status = row["status"]
    if status == STATUS_COMMITTED:
        raise ApiError(
            409,
            f"快照 {snapshot_id} 已固化（committed），不能再撤销。"
            "如需回退请由管理员重新执行消解，或新建快照后再撤销。",
        )
    if status == STATUS_REVERTED:
        # 幂等：不重复回写。重复回写会把「之后别人做的合法修改」又打回快照，
        # 是真正危险的操作——所以这里必须只读不写。
        return {"already_reverted": True, "restored": 0, "missing": 0}

    payload = json.loads(row["payload"])
    restored, missing = _restore(conn, payload)
    conn.execute(
        "UPDATE resolve_snapshots SET status = ? WHERE id = ?", (STATUS_REVERTED, snapshot_id)
    )
    conn.commit()
    return {"already_reverted": False, "restored": restored, "missing": missing}


# ---------------------------------------------------------------------------
# 4. 固化（commit）
# ---------------------------------------------------------------------------


def require_own_level(conn: sqlite3.Connection, user_id: str, group_id: str) -> None:
    """固化权限：该组 effective_level 必须是 own，否则 403。

    `authz.effective_level` 已实现「admin 本组织短路 → own」
    （`authz.py:206-211`），所以这里**不要**再单独判一次全局角色——
    判两遍等于把同一条权限口径写两份，将来必漂移。
    teacher 的天花板是 propose（`authz.ROLE_CEILING`），授 own 也封顶到
    propose → 教师一律 403，符合例外⑧「教师只能 apply/revert」。
    """
    eff = authz.effective_level(conn, user_id, group_id)
    if eff != "own":
        raise ApiError(
            403,
            f"固化需要该组的 own 档权限（当前 {eff or '无授权'}）。"
            "教师可应用与撤销，固化请由管理员执行。",
        )


def commit(conn: sqlite3.Connection, org_id: str, snapshot_id: str) -> None:
    """把 applied 快照置为 committed。非 applied → 409。"""
    row = get_row(conn, org_id, snapshot_id)
    if row["status"] != STATUS_APPLIED:
        raise ApiError(
            409,
            f"快照 {snapshot_id} 当前状态为 {row['status']}，只有 applied 状态可以固化。",
        )
    conn.execute(
        "UPDATE resolve_snapshots SET status = ? WHERE id = ?",
        (STATUS_COMMITTED, snapshot_id),
    )
    conn.commit()
