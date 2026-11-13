"""组织公告领域内核（卡 rTVvb8）。

用途与设计意图（答辩口径）：
  1. 公告是**组织级**内容：一条公告属于一个 `org_id`，跨组织不可见。
     隔离靠「SQL 一律带 `WHERE org_id = ?`」，不是靠接口层自觉过滤。
  2. 每组织**恒一条**（`UNIQUE(org_id)`）：发布＝upsert，停用＝软删
     （`active=0`，不删行），停用后重新发布复用同一 id。
  3. 正文 `body_md` 是 **Markdown 原文**：本文件不转义、不 strip、不转 HTML。
     防注入的责任在前端——它必须用 textContent / mdToDom 构建 DOM
     （⛔ 禁 innerHTML），后端只保证「原样存、原样还」。
  4. 为什么单独一个内核文件：`api_*.py` 只能是接口层（AGENTS §3.1 硬约束），
     读写规则只允许这一份实现，接口层只做「取参数 → 调这里 → 拼响应」。

签名约定：本文件不注册路由，因此文件名**不能**是 `api_*.py`
（routes.py 会 glob 扫到它并要求导出非空 ROUTES）。
只用 Python 标准库。
"""

from __future__ import annotations

import sqlite3

from scheduler.db import init_db, new_id

#: `deactivate()` 的三种结果。⛔ 不要用 True/False 三态混淆「已停用」与「不存在」。
DEACTIVATED = "deactivated"
ALREADY_INACTIVE = "already_inactive"


def ensure_announcements(conn: sqlite3.Connection) -> None:
    """确保 announcements 表存在（幂等）。

    🔴 为什么每次读前都要调：`ensure_database` 只在**库文件不存在**时 seed，
    已存在的 data/demo.db、data/trial.db 与线上部署库**不会自动补进
    schema.sql 新增的表**。缺表时 GET /api/announcements 会直接 500
    （sqlite3.OperationalError: no such table），而 /api/health 仍报 200
    —— 典型的假绿灯。

    schema.sql 是这张表的**唯一 DDL 来源**，杜绝表结构在代码与 schema 里
    各写一遍的漂移（与 b4_kernels.ensure_snapshot_table 同范式）。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'announcements'"
    ).fetchone()
    if row is None:
        init_db(conn)


def get_active(conn: sqlite3.Connection, org_id: str) -> dict | None:
    """本组织当前生效的公告；没有或已停用 → None。

    `updated_by_name` 取值顺序：objects.name（人可读姓名）→ users.login
    （没有对象名时的兜底）→ 原始 id（账号已被删除时的最后兜底）。
    ⛔ 不 join 不到就返回空串：那会让前端显示「更新于 X · 」这种残句。
    """
    ensure_announcements(conn)
    row = conn.execute(
        "SELECT a.id AS id, a.org_id AS org_id, a.title AS title,"
        "       a.body_md AS body_md, a.updated_by AS updated_by,"
        "       a.updated_at AS updated_at,"
        "       COALESCE(o.name, u.login, a.updated_by) AS updated_by_name"
        "  FROM announcements a"
        "  LEFT JOIN users u ON u.id = a.updated_by"
        "  LEFT JOIN objects o ON o.id = u.object_id"
        " WHERE a.org_id = ? AND a.active = 1",
        (org_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def upsert(
    conn: sqlite3.Connection,
    org_id: str,
    user_id: str,
    title: str,
    body_md: str,
) -> dict:
    """发布公告（有则覆盖、无则新建），返回 `{"created": bool, "announcement": dict}`。

    🔴 `DO UPDATE` 里**必须**写 `active = 1`：否则「先停用、再发布」之后
       GET 仍然返回 null —— 一条没有任何报错的静默失效（本站最贵的 bug 类型）。
    🔴 更新分支不动 `id` / `created_at`：id 稳定，前端与审计都靠它。

    `created` 只是回执字段，并发首插时可能不准（两请求都看到「无行」）；
    数据本身由 `UNIQUE(org_id)` + `ON CONFLICT` 保证永远只有一行。

    `title` 由接口层 strip 后传入；`body_md` 原样入库（XSS 往返保真靠这个）。
    """
    ensure_announcements(conn)

    existed = conn.execute(
        "SELECT id FROM announcements WHERE org_id = ?", (org_id,)
    ).fetchone()
    ann_id = existed["id"] if existed is not None else new_id("ann")

    conn.execute(
        "INSERT INTO announcements"
        " (id, org_id, title, body_md, active, updated_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 1, ?, datetime('now'), datetime('now'))"
        " ON CONFLICT(org_id) DO UPDATE SET"
        "   title      = excluded.title,"
        "   body_md    = excluded.body_md,"
        "   active     = 1,"
        "   updated_by = excluded.updated_by,"
        "   updated_at = datetime('now')",
        (ann_id, org_id, title, body_md, user_id),
    )
    conn.commit()
    return {"created": existed is None, "announcement": get_active(conn, org_id)}


def deactivate(
    conn: sqlite3.Connection,
    org_id: str,
    announcement_id: str,
    user_id: str,
) -> str | None:
    """按 id 停用（软删）。返回 `DEACTIVATED` / `ALREADY_INACTIVE` / `None`。

    `org_id` 由接口层从 token + group 解析得出，**绝不采用客户端传的组织**；
    SQL 里带 `org_id` 就是把组织隔离写进写路径——拿别组织的 id 来删，
    影响行数必然是 0。

    `rowcount == 0` 不能直接当 404：可能是「已经停用过了」。所以再按
    `(id, org_id)` 查一次——存在则幂等（双击不报错），不存在才让接口转 404
    （404 同时覆盖「不存在」与「属于别的组织」，不泄漏 id 是否存在）。
    """
    ensure_announcements(conn)
    cur = conn.execute(
        "UPDATE announcements"
        "   SET active = 0, updated_by = ?, updated_at = datetime('now')"
        " WHERE id = ? AND org_id = ? AND active = 1",
        (user_id, announcement_id, org_id),
    )
    if cur.rowcount:
        conn.commit()
        return DEACTIVATED

    row = conn.execute(
        "SELECT 1 FROM announcements WHERE id = ? AND org_id = ?",
        (announcement_id, org_id),
    ).fetchone()
    return None if row is None else ALREADY_INACTIVE
