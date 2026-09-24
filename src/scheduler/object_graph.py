"""对象图（DAG）+ 闭包表，以及身份层（登录账号）。

设计意图：

1. **为什么是 DAG 而不是树？**
   一个学生可以同时属于「高一(3)班」和「机器人社团」。单亲树表达不了这种关系，
   强行表达就要给学生复制两行或给社团打补丁，后面权限判定和冲突检测全要写特判。
   DAG 允许多个父节点，语义直接对应现实。

2. **为什么配闭包表？**
   "某年级下所有学生""某学生所属的所有组"这类查询非常频繁，
   每次递归遍历会拖慢冲突检测。闭包表以空间换时间：
   把 (祖先, 后代, 距离) 全量存下来，查询变成一次索引扫描。
   规模估算：1500 人 + 32 班 + ~10 社团，闭包行数约 1–2 万，SQLite 完全无压力。

3. **对象 ≠ 账号**。
   objects 里可以有 1500 个学生行（用于调度与冲突检测），
   users 里只放真正需要登录的人。两者由 object_id 关联、互不强制。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3

from .db import new_id

_PBKDF2_ROUNDS = 100_000


class CycleError(ValueError):
    """试图建立一条会形成环的边。"""


# ---------------------------------------------------------------------------
# 口令存储（不引第三方依赖，保证离线可跑）
# ---------------------------------------------------------------------------


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """pbkdf2-hmac-sha256，返回 'pbkdf2$<salt_hex>$<hash_hex>'。"""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), _PBKDF2_ROUNDS
    )
    return hmac.compare_digest(dk.hex(), dk_hex)


# ---------------------------------------------------------------------------
# 组织
# ---------------------------------------------------------------------------


def create_organization(
    conn: sqlite3.Connection, name: str, *, create_root_group: bool = True
) -> tuple[str, str | None]:
    """创建组织，并自动创建名为「全校」的根组。

    根组的作用：全校性活动只要把根组选为参与者对象即可，
    不必因为"组织不是对象"而写任何特判。
    """
    org_id = new_id("org")
    conn.execute("INSERT INTO organizations(id, name) VALUES (?, ?)", (org_id, name))
    root_id = None
    if create_root_group:
        root_id = create_object(conn, org_id, "group", name, is_root=True)
    conn.commit()
    return org_id, root_id


def get_root_group(conn: sqlite3.Connection, org_id: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM objects WHERE org_id = ? AND kind = 'group' AND is_root = 1",
        (org_id,),
    ).fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------------------
# 对象
# ---------------------------------------------------------------------------


def create_object(
    conn: sqlite3.Connection,
    org_id: str,
    kind: str,
    name: str,
    *,
    capacity: int | None = None,
    is_root: bool = False,
) -> str:
    """创建对象，并写入自反边（自己到自己的闭包行，depth=0）。"""
    if kind not in ("group", "user", "resource"):
        raise ValueError(f"非法 kind: {kind}")
    oid = new_id("obj")
    conn.execute(
        "INSERT INTO objects(id, org_id, kind, name, capacity, is_root)"
        " VALUES (?,?,?,?,?,?)",
        (oid, org_id, kind, name, capacity, 1 if is_root else 0),
    )
    conn.execute(
        "INSERT OR IGNORE INTO object_edges(ancestor_id, descendant_id, depth)"
        " VALUES (?,?,0)",
        (oid, oid),
    )
    return oid


def delete_object(conn: sqlite3.Connection, object_id: str) -> None:
    """删除对象。闭包行与参与者行由外键 ON DELETE CASCADE 自动清理。"""
    conn.execute("DELETE FROM objects WHERE id = ?", (object_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# 边与闭包
# ---------------------------------------------------------------------------


def is_ancestor(conn: sqlite3.Connection, ancestor_id: str, descendant_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM object_edges WHERE ancestor_id = ? AND descendant_id = ?",
        (ancestor_id, descendant_id),
    ).fetchone()
    return row is not None


def add_edge(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    """建立 parent → child 的父子关系，并增量维护闭包表。

    防环判据：如果 child 已经是 parent 的祖先，再加这条边就会成环。
    """
    if parent_id == child_id:
        raise CycleError("不能把自己作为自己的父节点")
    if is_ancestor(conn, child_id, parent_id):
        raise CycleError(
            f"会形成环：{child_id} 已经是 {parent_id} 的祖先"
        )
    # 闭包增量：parent 的所有祖先（含自身） × child 的所有后代（含自身）
    conn.execute(
        "INSERT OR IGNORE INTO object_edges(ancestor_id, descendant_id, depth)"
        " SELECT a.ancestor_id, d.descendant_id, a.depth + 1 + d.depth"
        " FROM object_edges a, object_edges d"
        " WHERE a.descendant_id = ? AND d.ancestor_id = ?",
        (parent_id, child_id),
    )
    conn.commit()


def remove_edge(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    """删除一条父子关系，然后全量重算闭包。

    为什么用全量重算而不是增量删除：增量删除闭包要判断"这条路径是否还有其他走法"，
    容易写错。删边是低频操作（调整组织架构时才会发生），
    1–2 万行的闭包全量重算在毫秒级，正确性远比速度重要。
    """
    # 只删直接边（depth = 1）；间接路径由 rebuild_closure 重新推导
    conn.execute(
        "DELETE FROM object_edges"
        " WHERE ancestor_id = ? AND descendant_id = ? AND depth = 1",
        (parent_id, child_id),
    )
    rebuild_closure(conn)
    conn.commit()


def rebuild_closure(conn: sqlite3.Connection) -> None:
    """按直接边（depth = 1 的边）全量重算闭包表。"""
    conn.execute("DELETE FROM object_edges WHERE depth > 0")
    conn.execute(
        "INSERT OR IGNORE INTO object_edges(ancestor_id, descendant_id, depth)"
        " SELECT id, id, 0 FROM objects"
    )
    direct = conn.execute(
        "SELECT ancestor_id, descendant_id FROM object_edges WHERE depth = 1"
    ).fetchall()
    adj: dict[str, list[str]] = {}
    for r in direct:
        adj.setdefault(r["ancestor_id"], []).append(r["descendant_id"])
    # BFS：从每个节点出发按层扩散
    for start in [r["id"] for r in conn.execute("SELECT id FROM objects").fetchall()]:
        seen: dict[str, int] = {}
        frontier = [(start, 1)]
        while frontier:
            nxt = []
            for node, depth in frontier:
                for child in adj.get(node, []):
                    if child in seen or child == start:
                        continue
                    seen[child] = depth
                    nxt.append((child, depth + 1))
            for node, depth in frontier:
                conn.execute(
                    "INSERT OR IGNORE INTO object_edges(ancestor_id, descendant_id, depth)"
                    " VALUES (?,?,?)",
                    (start, node, depth),
                )
            frontier = nxt
    conn.commit()


def descendants(
    conn: sqlite3.Connection,
    object_id: str,
    *,
    kind: str | None = None,
    include_self: bool = False,
) -> list[str]:
    """查询后代（含直接子节点与间接）。"""
    sql = (
        "SELECT e.descendant_id AS id, o.kind AS kind FROM object_edges e"
        " JOIN objects o ON o.id = e.descendant_id"
        " WHERE e.ancestor_id = ?"
    )
    params: list = [object_id]
    if kind:
        sql += " AND o.kind = ?"
        params.append(kind)
    if not include_self:
        sql += " AND e.depth > 0"
    rows = conn.execute(sql, params).fetchall()
    return [r["id"] for r in rows]


def ancestors(
    conn: sqlite3.Connection, object_id: str, *, include_self: bool = False
) -> list[str]:
    sql = "SELECT ancestor_id FROM object_edges WHERE descendant_id = ?"
    params: list = [object_id]
    if not include_self:
        sql += " AND depth > 0"
    return [r["ancestor_id"] for r in conn.execute(sql, params).fetchall()]


def groups_of(conn: sqlite3.Connection, object_id: str) -> list[str]:
    """某对象直属/间接所属的组。"""
    return [
        a
        for a in ancestors(conn, object_id)
        if conn.execute("SELECT kind FROM objects WHERE id = ?", (a,)).fetchone()["kind"]
        == "group"
    ]


# ---------------------------------------------------------------------------
# 身份层
# ---------------------------------------------------------------------------


def create_user(
    conn: sqlite3.Connection,
    object_id: str,
    login: str,
    password: str,
    role: str = "teacher",
) -> str:
    """为某个对象开通登录账号。

    注意：objects 里的行（可被调度）与 users 里的行（可登录）是两回事。
    学生可以只有 objects 行而没有 users 行。
    """
    if role not in ("admin", "teacher", "terminal", "student"):
        raise ValueError(f"非法 role: {role}")
    uid = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, object_id, login, password_hash, role)"
        " VALUES (?,?,?,?,?)",
        (uid, object_id, login, hash_password(password), role),
    )
    conn.commit()
    return uid


def authenticate(conn: sqlite3.Connection, login: str, password: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    if row is None or row["status"] != "active":
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return dict(row)
