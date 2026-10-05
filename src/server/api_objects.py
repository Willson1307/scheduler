"""B3 对象列表接口：GET /api/objects?group=<id>（docs/07 §3）。

响应 200：**[{"id","kind","name","parent_ids":[...]}]** —— 顶层就是数组，
🔴 不是包在对象里（契约 §3 冻结；前端直接遍历，包一层会渲染成空列表）。

---------------------------------------------------------------------------
用途与设计意图（答辩口径）
---------------------------------------------------------------------------
  1. **闭包一次查询解决两件事**：对象集合来自 `object_edges` 的传递闭包
     （某组的全部后代，含组自身），父集合来自同一张表的 `depth = 1` 行。
     闭包表本来就是为「查某组下所有人」设计的（schema.sql:54-56），
     用它比"递归 CTE 自己走图"快一个量级，且与领域层口径一致。
  2. **🔴🔴 必须过滤 depth = 0**：闭包表含**自反边**（节点指向自己，depth=0）。
     契约的 `parent_ids` 语义是「**全部直接父**」。若不过滤，
     每个对象都会把自己算成自己的父，前端树控件会出现自环。
     本文件里depth=0 只用于「把组自身纳入结果」（它确实该在结果里），
     绝不用于「算父」——这两件事必须分开，这是本文件最容易写错的地方。
  3. **排序必须全序**：`objects.id` 是 uuid4（随机），直接 SELECT 的行序
     不保证稳定，而契约要求「同一请求两次调用逐字一致」，否则前端每次刷新
     列表顺序都会变（肉眼可见的闪烁/跳动）。因此显式
     `ORDER BY kind, name, id` 定对象序，`ORDER BY 父名, 父id` 定父序——
     两条都带唯一键兜底，构成全序，不存在并列。
  4. **组织隔离**：闭包边表没有 org_id 列（靠 objects 冗余的 org_id 过滤），
     查询里显式加 `o.org_id = ?`。这不是多余条件——B2 已抓到「可跨组织操作
     他组织课表」的 P0，本接口将来接鉴权时，这里是同一类漏洞的出口。
  5. **kind 严格三值**（group / user / resource）：由 schema.sql:45 的 CHECK
     约束保证写入侧只有三值（契约 §3 勘误已明确 organization **不进** objects，
     它在独立的 organizations 表）。本接口**原样透传**而不加白名单过滤：
     契约 §3 同时写明「将来新增取值属非破坏性变更，前端按 §12 降级渲染」，
     服务端主动过滤掉未知 kind 会让新值**静默消失**，比透传更难排查。

---------------------------------------------------------------------------
契约未写清之处（本文档的假设，与队长确认后可能调整）
---------------------------------------------------------------------------
  A. `group` 是否可缺省：契约 §3 表格标「**可选**」，同文 §1 说「缺省 = 所在
     最高组」。本实现取**表格的口径（可选）**，理由是**有测试实证**：
     `test_contract.py:211` 就是不带 group 的 `GET /api/objects`，且断言
     200 + 结果非空。若改用 `b3_kernels.require_group`（缺失即 400），
     会把契约自己的测试打成 400。
     🔴 但「缺省 = 所在最高组」不能实现成「全体对象」——那会让用户看到
     别的班级。B2 补鉴权后，缺省走 `K.resolve_authorized_org` →
     `authz.default_group_id(conn, user_id)` 定位**登录用户自己的默认组**
     （docs/07 §1「缺省=所在最高组」），不再取「该组织根组」，也绝不取
     「第一个组织」（多组织下会跨组织泄漏）。
     与 `/api/conflicts` 的宽松口径（`K.parse_group`）保持一致。
  B. 父的排序：契约未规定。本实现按 (父名, 父id) 升序，只为**确定性**。
  C. 额外字段（depth / is_root / parent_names）依据契约 §12「允许加字段，
     前端忽略不认识的字段」。之所以加：前端要画组树就得知道层级深度与
     父组名，否则得再发一次请求去查——多一次往返，且多一次口径漂移的机会。
     🔴 顶层**不能**加 kind 计数（顶层必须是数组），前端按 kind 自行 count 即可。

签名约定（写死，不得改）：handle(method, path, query, body, headers, context=None)。
只用 Python 标准库。
"""

from __future__ import annotations

import sqlite3

from . import auth
from . import b3_kernels as K
from . import config
from .http_api import ApiError

# ---------------------------------------------------------------------------
# 一次查询取回「闭包内每个对象 × 它的每个直接父」
# ---------------------------------------------------------------------------
#
# 为什么用 LEFT JOIN 而不是两次查询：
#   父集合只关心 depth=1，用 LEFT JOIN 才能让「没有父的根组」也出现在结果里
#   （根组的 parent_ids 应为 []，写成 INNER JOIN 根组会被整条丢掉）。
#
# 🔴 ON 子句里的 `pe.depth = 1` 是**本接口的灵魂**：它让 depth=0 的自反边
#    永远不可能进入 parent_id / parent_name 两列。
#
# ORDER BY 覆盖全部输出列且每列都带唯一键兜底（o.id / pe.ancestor_id），
# ⇒ 任何两行都不并列 ⇒ 结果与SQLite 的行存储顺序无关 ⇒ 两次调用逐字一致。

_SQL_OBJECTS_IN_GROUP = """
SELECT o.id           AS id,
       o.kind         AS kind,
       o.name         AS name,
       o.is_root      AS is_root,
       e.depth        AS depth,
       pe.ancestor_id AS parent_id,
       p.name         AS parent_name
FROM object_edges e
JOIN objects o
  ON o.id = e.descendant_id
LEFT JOIN object_edges pe
  ON pe.descendant_id = o.id AND pe.depth = 1
LEFT JOIN objects p
  ON p.id = pe.ancestor_id
WHERE e.ancestor_id = ?
  AND o.org_id = ?
ORDER BY o.kind, o.name, o.id, p.name, pe.ancestor_id
"""


def _load_rows(conn: sqlite3.Connection, group_id: str, org_id: str) -> list:
    """取该组闭包内的全部对象及其直接父（未整形的扁平行）。"""
    return conn.execute(_SQL_OBJECTS_IN_GROUP, (group_id, org_id)).fetchall()


def _build_items(rows: list) -> list[dict]:
    """扁平行 → 契约 §3 的对象数组。

    行序已由 SQL 保证（同对象的行连续、同对象的父按父名/父id 升序），
    所以这里只需「按 id 归并」，无需二次排序——**不在Python 侧重排是刻意的**：
    排序口径只有SQL 里那一条，避免两处排序逻辑漂移。
    """
    items: list[dict] = []
    index: dict[str, dict] = {}   # id → 已建好的 item（归并用）
    for r in rows:
        oid = r["id"]
        item = index.get(oid)
        if item is None:
            # 首次见到该对象 → 新建一项。is_root 转成 bool（SQLite 存 0/1，
            # 前端不必再猜 0 是不是 false）。
            item = {
                "id": oid,
                "kind": r["kind"],
                "name": r["name"],
                "parent_ids": [],
                # —— 以下为契约 §12 允许的额外字段（前端忽略不认识的字段）——
                "parent_names": [],
                "depth": r["depth"],   # 0 = 该组自身，1 = 直接成员，2 = 更深层
                "is_root": bool(r["is_root"]),
            }
            index[oid] = item
            items.append(item)
        # LEFT JOIN 未命中时 parent_id 为 NULL ⇒ 该对象无父（根组），
        # 自然跳过，不产生任何 parent_ids 元素 ⇒ 正是契约要的 []。
        if r["parent_id"] is not None:
            item["parent_ids"].append(r["parent_id"])
            item["parent_names"].append(r["parent_name"])
    return items


@auth.require_auth
def list_objects(method: str, path: str, query: dict, body: bytes, headers: dict, context=None):
    """GET /api/objects?group=<id> → 200（契约 docs/07 §3）。

    成功 200： [{"id","kind","name","parent_ids":[...], ...}]
    未登录 → 401（@require_auth）
    越权（group 不在用户 authorized_groups）→ 403（K.resolve_authorized_org）
    group 不存在 → 404；group 指向的不是一个组 → 400（本文件校验，见下）

    🔴 group 缺省**不报错**（契约 §3 表格把 group 标为「可选」，
    且 test_contract.py:211 就是不带 group 的 `GET /api/objects`，
    断言 200 且结果非空）。缺省时走 `K.resolve_authorized_org` →
    `authz.default_group_id` 定位用户自己的默认组（docs/07 §1「缺省=所在最高组」），
    **不再取第一个组织**（多组织下会返回别家数据）。
    """
    # 🔴 用宽松口径（parse_group）而非 K.require_group：契约 §3 表格标
    #    「可选」，且 test_contract.py:211 是不带 group 的调用。
    #    缺省时必须落到「用户自己的一个具体组」（authz.default_group_id）
    #    而不是全体对象/第一个组织——否则用户会看到别的班级/组织的数据。
    user_id = context["user_id"]
    group = K.parse_group(query)

    conn = config.open_db()
    try:
        # 越权校验 + 组 → org_id（越权 403；组不存在 404）。
        # 取 org_id 有两个用途：① 闭包查询按 org_id 做组织隔离；
        # ② 组不存在时立刻 404，而不是返回空数组（空数组会被前端当成
        #    「这个组没有对象」，比报错更难解释）。
        group, org_id = K.resolve_authorized_org(conn, user_id, group)

        # group 参数（显式传入时）必须真的是「组」。resolve_org 只校验
        # 「对象存在」，传一个 student 的 id 也能过；那时返回的是
        # 「该学生自己」的闭包（只有它自己），是个「看起来正常但答错了」
        # 的答案，必须挡掉。
        kind_row = conn.execute("SELECT kind FROM objects WHERE id = ?", (group,)).fetchone()
        if kind_row["kind"] != "group":
            raise ApiError(400, f"group 必须是 kind='group' 的对象，收到 kind={kind_row['kind']!r}")

        return _build_items(_load_rows(conn, group, org_id))
    finally:
        conn.close()


ROUTES = {
    ("GET", "/api/objects"): list_objects,
}