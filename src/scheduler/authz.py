"""B2 权限领域层（authz.py）。

用途与设计意图（答辩口径）：
  1. 「权限按组」而非全局角色一把梭——领导身份体现在管的哪个组，
     不在全局角色（AGENTS §9）。全局角色（users.role）只当天花板。
  2. 四档语义（审批流下重定义，docs/06 §1）：
       view 看 / propose 提请求 / approve 批下级请求 / own 改组结构+批永久改
     「任何人都不直接改数据」由 B8 审批流落地；本模块只判能不能看/进组。
  3. 四条机制（docs/06 §1，不要动）：
       ① 沿对象图祖先链继承（授权向下流动）
       ② 多条路径取最高档 max（不就近优先）
       ③ 全局角色当天花板（admin=own / teacher=propose / terminal=view）
       ④ 不做负权限 deny（DAG 多父下语义不自洽；收回 = 删行）
  4. 三处勘误（队长 2026-10-03）已落实：
       - 勘误2：level 比较用 LEVEL_ORDER 整数映射，不靠字符串字典序
         （'own' 字典序 < 'view'，会得反结果）
       - 勘误3：default_group_id 按层级最高（depth-to-root 最小），
         并列才比 level / group_id

纯函数 + SQLite，零第三方依赖。领域层红线：本文件为队长批准的新增例外
（AGENTS §3 ①），不碰 src/scheduler/ 下任何现有文件。

闭包表依赖：effective_level / authorized_groups 依赖 object_edges 祖先链。
P0-1（rebuild_closure 清空闭包表）已由队长修复合入 main，祖先链可用。
"""

from __future__ import annotations

import sqlite3

from .db import new_id

# ---------------------------------------------------------------------------
# 权限档有序映射（勘误2：禁止用字符串字典序比较 level）
# ---------------------------------------------------------------------------
#: level → rank。语义上 view < propose < approve < own。
#: 字符串字典序里 'own' < 'view'（'o'<'v'），与权限语义相反，故必须用本映射。
LEVEL_ORDER: dict[str, int] = {"view": 0, "propose": 1, "approve": 2, "own": 3}

#: rank → level（反向查表）。
LEVEL_BY_RANK: dict[int, str] = {v: k for k, v in LEVEL_ORDER.items()}

#: 全局角色 → 天花板档（AGENTS §9）。
#: admin 本组织短路 + 组织外显式授权（CP-H-S2 甲-2，见 effective_level）；
#: 其余角色天花板为纯 cap。
#: student 无 users 行不登录，此处保留映射以备完整性校验。
ROLE_CEILING: dict[str, str] = {
    "admin": "own",
    "teacher": "propose",
    "terminal": "view",
    "student": "view",
}

#: 合法 level 集合（参数校验用）。
VALID_LEVELS = frozenset(LEVEL_ORDER)


def ceiling_for_role(role: str) -> str | None:
    """全局角色对应的天花板档；未知角色返回 None（视为无授权）。"""
    return ROLE_CEILING.get(role)


def _rank(level: str) -> int:
    """level → rank；非法 level 抛 ValueError（早暴露数据错误）。"""
    r = LEVEL_ORDER.get(level)
    if r is None:
        raise ValueError(f"非法 level：{level!r}（合法值 {sorted(VALID_LEVELS)}）")
    return r


# ---------------------------------------------------------------------------
# 写入：授权 / 撤销
# ---------------------------------------------------------------------------

def grant_role(
    conn: sqlite3.Connection,
    org_id: str,
    group_id: str,
    user_id: str,
    level: str,
) -> str:
    """授予（或覆盖）某用户在某组的权限档。

    UNIQUE(org_id, group_id, user_id) 保证一人一组一行：
    再授不同档 = 覆盖（upsert），不并存——多路径取 max 来自对象图 DAG，
    不来自本表多行（docs/06 §1 机制②）。

    返回 group_roles.id。
    """
    if level not in VALID_LEVELS:
        raise ValueError(f"非法 level：{level!r}（合法值 {sorted(VALID_LEVELS)}）")
    rid = new_id("grl")
    conn.execute(
        "INSERT INTO group_roles (id, org_id, group_id, user_id, level) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (org_id, group_id, user_id) DO UPDATE SET level=excluded.level",
        (rid, org_id, group_id, user_id, level),
    )
    conn.commit()
    return rid


def revoke_role(
    conn: sqlite3.Connection,
    org_id: str,
    group_id: str,
    user_id: str,
) -> int:
    """撤销某用户在某组的授权（删行）。返回删除行数。

    不做负权限 deny（docs/06 §1 机制④）：收回权限 = 删掉那一行，
    不存在「授 view 抵消 own」这类负权限——DAG 多父下语义不自洽。
    """
    cur = conn.execute(
        "DELETE FROM group_roles WHERE org_id=? AND group_id=? AND user_id=?",
        (org_id, group_id, user_id),
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# 读取：有效授权判定
# ---------------------------------------------------------------------------

def _user_role(conn: sqlite3.Connection, user_id: str) -> str | None:
    """取 users.role（全局角色）。用户不存在返回 None。"""
    row = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    return row["role"] if row else None


def _user_org_id(conn: sqlite3.Connection, user_id: str) -> str | None:
    """用户所属组织 id（users.object_id → objects.org_id）。

    CP-H-S2（方案甲-2）：admin 的隐式 own 权限以此为锚。
    用户行缺失 / object_id 悬挂 → None（fail-closed：无所属组织
    即无隐式范围，只剩显式 group_roles 授权）。
    """
    row = conn.execute(
        "SELECT o.org_id AS org_id FROM users u "
        "JOIN objects o ON o.id = u.object_id WHERE u.id = ?",
        (user_id,),
    ).fetchone()
    return row["org_id"] if row else None


def _group_in_org(conn: sqlite3.Connection, group_id: str, org_id: str) -> bool:
    """组是否存在且属于指定组织。"""
    row = conn.execute(
        "SELECT 1 FROM objects WHERE id = ? AND org_id = ?", (group_id, org_id)
    ).fetchone()
    return row is not None


def _granted_groups(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """沿祖先链（含自身）取用户有**显式 group_roles 授权**的全部组 id。

    admin 与非 admin 共用（CP-H-S2 甲-2：admin 在自己组织之外的组
    也走这条显式授权路径）。
    """
    # 后代（含自身）= WHERE ancestor_id IN (用户有直接授权的组)
    # 🔴 2026-10-10 修复：必须过滤 o.kind='group'。
    # 原实现只取 descendant_id，而闭包表里含 kind='user' 的学生对象
    #（班级 → 学生 也是一条边）→ 教师登录后组列表混进 28 个学生对象，
    # 组切换器出现「示例学生03-15」这种根本不是组的条目。
    # 该缺陷在试用库里因 group_roles 之前为空而被掩盖（教师根本进不去），
    # 一旦修好授权立刻暴露 —— 属于「修A 牵出 B」的连锁。
    rows = conn.execute(
        "SELECT DISTINCT e.descendant_id AS gid "
        "FROM object_edges e "
        "JOIN group_roles gr ON gr.group_id = e.ancestor_id "
        "JOIN objects o ON o.id = e.descendant_id "
        "WHERE gr.user_id = ? AND o.kind = 'group' "
        "ORDER BY e.descendant_id",
        (user_id,),
    ).fetchall()
    return [r["gid"] for r in rows]


def effective_level(
    conn: sqlite3.Connection,
    user_id: str,
    group_id: str,
) -> str | None:
    """用户在指定组上的有效授权档。

    算法（docs/06 §1 + AGENTS §9 + CP-H-S2 甲-2）：
      1. admin → **本组织短路**：group 在自己所属组织内直接返回 'own'
         （无需任何 group_roles 行）；组织外的组不短路，落入下方
         显式授权路径（cp-H-S2 修复前是全库无条件 'own'，跨组织泄露）。
      2. 其余角色（含 admin 的组织外组）：取 group_id 的所有祖先
         （含自身，object_edges depth>=0），在这些祖先上查该用户的
         group_roles，取最高档 max。无任何授权 → 返回 None。
      3. 全局角色天花板 cap：effective = min(max_grant, ceiling)。
         teacher 授 own 也被 cap 到 propose（审批流下普通教师不能批永久改）。

    返回 level 字符串或 None（无授权）。
    """
    role = _user_role(conn, user_id)
    if role is None:
        return None
    ceiling = ROLE_CEILING.get(role)
    if ceiling is None:
        return None  # 未知全局角色，视为无授权

    # admin 本组织短路（CP-H-S2 甲-2：仅限自己所属组织）
    if role == "admin":
        org = _user_org_id(conn, user_id)
        if org is not None and _group_in_org(conn, group_id, org):
            return "own"
        # 组织外的组：不 return，落入下方显式授权路径（ceiling=own cap）

    # 沿祖先链（含自身）取该用户的最高授权档
    # object_edges: (ancestor_id, descendant_id, depth)，含自反边 depth=0
    # group_id 的所有祖先 = WHERE descendant_id = group_id
    rows = conn.execute(
        "SELECT gr.level AS level "
        "FROM group_roles gr "
        "JOIN object_edges e ON e.ancestor_id = gr.group_id "
        "WHERE gr.user_id = ? AND e.descendant_id = ?",
        (user_id, group_id),
    ).fetchall()
    if not rows:
        return None  # 无任何授权（非 admin）→ 无访问权

    max_rank = max(_rank(r["level"]) for r in rows)
    eff_rank = min(max_rank, _rank(ceiling))  # 天花板 cap
    return LEVEL_BY_RANK[eff_rank]


def can(
    conn: sqlite3.Connection,
    user_id: str,
    group_id: str,
    required_level: str,
) -> bool:
    """用户在指定组上是否达到 required_level（含继承与天花板）。"""
    eff = effective_level(conn, user_id, group_id)
    if eff is None:
        return False
    return _rank(eff) >= _rank(required_level)


# ---------------------------------------------------------------------------
# 读取：组列表（/api/groups 底座）
# ---------------------------------------------------------------------------

def authorized_groups(
    conn: sqlite3.Connection,
    user_id: str,
) -> list[str]:
    """用户有授权（含继承）的全部组 id 列表。

    /api/groups 契约（docs/07 §2）：只返回我有授权的组，未授权的根本不列出
    （不是灰掉）。

    算法（CP-H-S2 甲-2）：
      - admin → 自己所属组织的全部组（隐式 own，无需 group_roles 行）
        ∪ 组织外**显式授权**的组及后代（与普通角色同一条路径）。
        修复前是 SELECT 全库所有组（跨组织泄露，CP-H-S2）。
      - 其余角色：用户在某组有直接授权 → 该组及其所有后代（含自身）都可见
        （授权沿祖先链向下继承），见 _granted_groups。
      - 所有角色天花板 >= view，故凡有授权的组 effective 都 >= view，
        无需再逐组算 effective 过滤。

    返回按 id 升序排序（确定性；单组织 admin 输出与修复前逐字节一致）。
    """
    role = _user_role(conn, user_id)
    if role is None:
        return []

    if role == "admin":
        org = _user_org_id(conn, user_id)
        gids: set[str] = set(_granted_groups(conn, user_id))
        if org is not None:
            rows = conn.execute(
                "SELECT id FROM objects WHERE kind='group' AND org_id=?",
                (org,),
            ).fetchall()
            gids.update(r["id"] for r in rows)
        return sorted(gids)

    return _granted_groups(conn, user_id)


def _depth_to_root(conn: sqlite3.Connection, group_id: str) -> int:
    """该组到最近根组（is_root=1）的深度。根组自身 = 0。

    勘误3：default_group_id 取「沿祖先链层级最高」= depth-to-root 最小。
    无根组祖先（孤儿组）返回大数，排到最后（不应出现在正常数据中）。
    """
    row = conn.execute(
        "SELECT MIN(e.depth) AS d "
        "FROM object_edges e "
        "JOIN objects o ON o.id = e.ancestor_id "
        "WHERE e.descendant_id = ? AND o.is_root = 1",
        (group_id,),
    ).fetchone()
    d = row["d"]
    return d if d is not None else 10_000


def default_group_id(
    conn: sqlite3.Connection,
    user_id: str,
) -> str | None:
    """登录后缺省组（docs/07 §1 + 勘误3）。

    精确定义：
      1. 取该用户有授权（effective >= view）的全部组；
      2. 在其中取沿对象图祖先链层级最高者（depth-to-root 最小，最靠近根）；
      3. 并列（同 depth）依次比较：① level 高的优先 ② group_id 字典序小的优先
         → 保证结果确定，避免每次登录显示的组不一样。

    管理员 → 根组「全校」；年级组长 → 该年级；任课教师 → 其所属最高层级组。
    无任何授权（非 admin）→ None。
    """
    groups = authorized_groups(conn, user_id)
    if not groups:
        return None

    def sort_key(gid: str) -> tuple[int, int, str]:
        depth = _depth_to_root(conn, gid)
        eff = effective_level(conn, user_id, gid)
        # level 高的优先 → rank 取负（越大越前）；无 effective 视为 view(0)
        neg_level = -_rank(eff) if eff is not None else 0
        return (depth, neg_level, gid)

    return min(groups, key=sort_key)
