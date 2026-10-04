"""B2 组列表接口：GET /api/groups（分支B）。

用途与设计意图（答辩口径）：
  1. 只返回当前用户有授权（含继承）的组——未授权的根本不列出（不是灰掉）
     （docs/07 §2）。底座是 authz.authorized_groups()。
  2. 每项含 group_id / name / level / member_count / parent_name：
     - level = 该用户在该组的有效授权档（含继承与天花板，authz.effective_level）
     - member_count = 该组后代中 kind='user' 的人数（"组列表显示人数"）
     - parent_name = 最近父组（depth=1）的名字；根组为 None（勘误3 判据）
  3. 多父 DAG 下 parent_name 取字典序最小者（保证结果确定；契约未规定多父行为）。
  4. @require_auth 装饰器统一拦 401（未登录/坏令牌）。

签名约定（写死）：handle(method, path, query, body, headers, context=None)。
只用 Python 标准库。
"""

from __future__ import annotations

from . import auth, config


@auth.require_auth
def list_groups(method: str, path: str, query: dict, body: bytes, headers: dict, context=None):
    """GET /api/groups。

    成功 200：[{"group_id","name","level","member_count","parent_name"}]
    未登录 401（由 require_auth 装饰器统一处理）。
    """
    from scheduler import authz  # 延迟导入，避免循环（authz 不依赖 server）

    user_id = context["user_id"]
    conn = config.open_db()
    try:
        gids = authz.authorized_groups(conn, user_id)
        result = []
        for gid in gids:
            obj = conn.execute("SELECT name FROM objects WHERE id=?", (gid,)).fetchone()
            name = obj["name"] if obj else None
            level = authz.effective_level(conn, user_id, gid) or "view"
            # member_count：该组后代中 kind='user' 的人数
            mc = conn.execute(
                "SELECT COUNT(*) AS c FROM object_edges e "
                "JOIN objects o ON o.id = e.descendant_id "
                "WHERE e.ancestor_id = ? AND o.kind = 'user'",
                (gid,),
            ).fetchone()["c"]
            # parent_name：最近父组（depth=1 且为 group）；多父取字典序最小
            parent = conn.execute(
                "SELECT o.name AS name FROM object_edges e "
                "JOIN objects o ON o.id = e.ancestor_id "
                "WHERE e.descendant_id = ? AND e.depth = 1 AND o.kind = 'group' "
                "ORDER BY o.name LIMIT 1",
                (gid,),
            ).fetchone()
            parent_name = parent["name"] if parent else None
            result.append({
                "group_id": gid,
                "name": name,
                "level": level,
                "member_count": mc,
                "parent_name": parent_name,
            })
        return result
    finally:
        conn.close()


ROUTES = {
    ("GET", "/api/groups"): list_groups,
}
