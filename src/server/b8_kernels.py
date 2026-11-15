"""B8 共用内核：审批流（提议→批准→落库）的**唯一口径源**。

🔴🔴 本文件存在的唯一理由（与 b3/b5/b9_kernels 同一条纪律）：让审批流的
「状态机 / 批准校验 / 落库」集中在一处，`api_approvals.py` 只做「取参数 →
调这里 → 拼响应」，**禁止自己再实现一遍**。

用途与设计意图（答辩口径）：
  1. 永久类改动（改周期日程的 valid_from）不直接生效，走「提出请求 → 批准
     → 落库」五态 + 审计日志（docs/06 §3/§4 冻结）。
  2. 🔴 draft 与 pending 一律**绝不碰任何业务数据**；只有 approved 才落库，
     落库成功才转 applied；落库失败回滚到 pending + 记 fail 事件
     （rCMOFk：回 approved 会让请求僵死——approve/reject/withdraw 只认 pending）。
  3. 🔴 只批下级（docs/06 §1）：批准/拒绝前批准人在该组织的档位必须
     **严格高于**提出人（require_higher_level），同级或更低 → 403。
  4. 🔴 批准时必须重跑硬约束（scheduler.solver.constraints.hard_violations）：
     提议时合法 ≠ 批准时合法（期间别人可能改了课表），「批准后才发现撞车」
     是审批流最典型的翻车点。校验通过才 UPDATE，不通过 409 零写入。
  5. 数据合规（AGENTS §7）：纯本地 SQLite 读写，不外发任何内容。

落库范围（本卡最窄的一条，docs/06 §2 永久改的最小代表）：
  target_type='schedule' + op='update' + 改 schedule_periodic.valid_from 一列。
  其他组合（group/member、create/delete）一律 raise ValueError，不静默跳过。

只用 Python 标准库。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from scheduler import authz
from scheduler.db import init_db, new_id
from scheduler.occupancy import (
    apply_schedule_exceptions,
    build_occupancy,
    expand_periodic,
    load_exceptions,
    load_terms,
)
from scheduler.slots import hex_to_mask, load_slot_config
from scheduler.solver.constraints import hard_violations
from scheduler.solver.m2 import build_context, related_objects

from .http_api import ApiError

# ---------------------------------------------------------------------------
# 契约常量（docs/06 §4）
# ---------------------------------------------------------------------------

#: 本卡支持的落库组合（唯一一条写路径）。其余组合由 apply_change 抛 ValueError。
_SUPPORTED_OP = "update"
_SUPPORTED_TARGET_TYPE = "schedule"

#: 请求终态（不可再变更）。
_TERMINAL_STATUSES = ("rejected", "withdrawn", "applied")

#: ensure 兜底要检查的两张表（缺任一都要 init_db，防止只查一张误判已就绪）。
_TABLES = ("change_requests", "change_events")


# ---------------------------------------------------------------------------
# 建表（幂等，第 8 处静默失效防线）
# ---------------------------------------------------------------------------


def ensure_change_tables(conn: sqlite3.Connection) -> None:
    """确保 change_requests 与 change_events 两张表都存在（幂等）。

    🔴 与 telemetry.ensure_event_log / b5_kernels.ensure_demands 同款问题：
    ensure_database 只在**库文件不存在**时 seed，已存在的 data/*.db
    （trial.db / demo.db）不会补进 schema.sql 新增的表。缺表时调审批接口会
    直接 500（no such table），而 /api/health 仍 200 → 假绿灯。

    **两张表都查**（S18 demand_levels 的教训：只查一张会误判「已就绪」，
    写另一张时 no such table）。schema.sql 是这两张表的唯一 DDL 来源。
    """
    missing = [
        name
        for name in _TABLES
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is None
    ]
    if missing:
        init_db(conn)


# ---------------------------------------------------------------------------
# 读辅助
# ---------------------------------------------------------------------------


def load_request(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row:
    """按 id 取请求；不存在 → 404。"""
    if not isinstance(request_id, str) or not request_id.strip():
        raise ApiError(400, "缺少参数 id（要操作哪条请求）")
    row = conn.execute(
        "SELECT * FROM change_requests WHERE id = ?", (request_id.strip(),)
    ).fetchone()
    if row is None:
        raise ApiError(404, "请求不存在")
    return row


def request_payload(req: sqlite3.Row) -> dict:
    """payload_json → dict（{before, after}）。"""
    return json.loads(req["payload_json"])


def new_request_id() -> str:
    """请求 id（`cr_` 前缀，契约示例 cr_x 同族）。"""
    return new_id("cr")


def new_event_id() -> str:
    return new_id("cev")


# ---------------------------------------------------------------------------
# 权限判定（docs/06 §1 四档）
# ---------------------------------------------------------------------------


def _has_level_in_org(
    conn: sqlite3.Connection, user_id: str, org_id: str, required_level: str
) -> bool:
    """用户在 org_id 内是否至少一个组达到 required_level。

    🔴 为什么不直接传 group：批准/查看按「请求所在的 org」判权，
    而非「调用者的默认组」——admin 的 default_group 可能落在别的组织，
    若按默认组判权会把跨组织的 admin 误杀。遍历该 org 的全部组，
    任一 `authz.can` 命中即可（admin 短路 own，teacher 天花板 propose）。
    """
    rows = conn.execute(
        "SELECT id FROM objects WHERE org_id = ? AND kind = 'group'", (org_id,)
    ).fetchall()
    return any(authz.can(conn, user_id, g["id"], required_level) for g in rows)


def require_approve_in_org(
    conn: sqlite3.Connection, user_id: str, org_id: str
) -> None:
    """批准/拒绝前：用户在 org_id 内至少一组达到 approve 档，否则 403。

    🔴 这条是审批流的灵魂之一：teacher 天花板 propose < approve ⇒
    老师永远批不了（即便被授了 own 也被 cap 到 propose）。
    """
    if not _has_level_in_org(conn, user_id, org_id, "approve"):
        raise ApiError(403, "无权批准：需要 approve 档及以上")


def require_org_access(
    conn: sqlite3.Connection, user_id: str, org_id: str
) -> None:
    """查看审计轨迹前：用户在 org_id 内有 view 级授权，否则 403（组织隔离）。"""
    if not _has_level_in_org(conn, user_id, org_id, "view"):
        raise ApiError(403, "无权访问该组织的请求")


# ---------------------------------------------------------------------------
# 只批下级（rCMOFk 缺陷2：批准人档位必须严格高于提出人）
# ---------------------------------------------------------------------------


def max_level_in_org(conn: sqlite3.Connection, user_id: str, org_id: str) -> str | None:
    """用户在 org_id 全部组上的最高有效档（None = 该组织内无任何授权）。

    口径与 _has_level_in_org 同源：按「请求所在组织」判权，不按默认组。
    沿 authz.effective_level：admin 本组织短路 own，teacher 天花板 propose。
    """
    rows = conn.execute(
        "SELECT id FROM objects WHERE org_id = ? AND kind = 'group'", (org_id,)
    ).fetchall()
    best: str | None = None
    for g in rows:
        eff = authz.effective_level(conn, user_id, g["id"])
        if eff is None:
            continue
        if best is None or authz.LEVEL_ORDER[eff] > authz.LEVEL_ORDER[best]:
            best = eff
    return best


def require_higher_level(
    conn: sqlite3.Connection,
    approver_id: str,
    requester_id: str,
    org_id: str,
) -> None:
    """批准/拒绝前：批准人在 org 内的档位必须**严格高于**提出人，否则 403。

    docs/06 §1：approve = 批准/拒绝**自己下级**提出的请求。同级或更低 → 403
    （两个 admin 互批也拦：本组织短路算 own，own == own 不满足严格高于；
    teacher 天花板 propose，propose <= propose 同样拦）。
    比较用 authz.LEVEL_ORDER 整数映射（勘误2：禁字符串字典序）。
    提出人无档（提交时已过 resolve_authorized_org，理论不可达）→ 视为
    低于 view，任何达到 approve 档的批准人都满足严格高于。
    自己批自己由 handler 更早的 requester == approver 判定拦，本函数不重复。
    """
    approver = max_level_in_org(conn, approver_id, org_id)
    requester = max_level_in_org(conn, requester_id, org_id)
    a_rank = authz.LEVEL_ORDER[approver] if approver is not None else -1
    r_rank = authz.LEVEL_ORDER[requester] if requester is not None else -1
    if a_rank <= r_rank:
        raise ApiError(
            403, "只能批准/拒绝自己下级的请求（批准人档位须严格高于提出人）"
        )


def can_view_request(
    conn: sqlite3.Connection, viewer_id: str, req_row: sqlite3.Row
) -> bool:
    """读取侧可见性（rRK0Q5）：本人 或 档位严格高于提出人 → 可见。

    口径与 require_higher_level 同源（只批下级的读取侧对偶）：提交/批准侧
    鉴权早就有，读取侧原来只有 org 过滤 → 本组织内教师B 能看到教师A 的
    改课申请（requester_id / reason / payload 前后快照全泄露），跨用户隐私
    越权。判据：
      - requester_id == 调用者 → 可见（自己的申请当然能看）；
      - 否则 max_level_in_org(调用者) **严格高于** max_level_in_org(提出人)
        （authz.LEVEL_ORDER 整数比较，🔴 禁字符串字典序）→ 可见；
      - 其余（同级或更低，如教师B 看教师A：propose == propose）→ 不可见。

    ⚠️ 已知且可接受的副作用（与「只能批自己下级」语义一致，不是 bug）：
    两个 admin 在本组织的有效档都短路成 own，own > own 不成立 →
    admin 之间互相看不到对方的申请。演示「提议→批准」请用
    「教师提议 + 管理员批准」组合（自批本来就 403）。
    """
    if req_row["requester_id"] == viewer_id:
        return True
    viewer = max_level_in_org(conn, viewer_id, req_row["org_id"])
    requester = max_level_in_org(conn, req_row["requester_id"], req_row["org_id"])
    v_rank = authz.LEVEL_ORDER[viewer] if viewer is not None else -1
    r_rank = authz.LEVEL_ORDER[requester] if requester is not None else -1
    return v_rank > r_rank


# ---------------------------------------------------------------------------
# 快照（提交时抓 before；落库后抓 after）
# ---------------------------------------------------------------------------


def capture_before(conn: sqlite3.Connection, target_row: sqlite3.Row) -> dict:
    """提交时抓「变更前」快照（最小字段，够审批页 diff）。"""
    return {
        "valid_from": target_row["valid_from"],
        "valid_to": target_row["valid_to"],
    }


# ---------------------------------------------------------------------------
# 批准时重跑硬约束（本卡最核心的一条）
# ---------------------------------------------------------------------------


def validate_apply(
    conn: sqlite3.Connection,
    org_id: str,
    target_row: sqlite3.Row,
    new_valid_from: str,
    cfg,
) -> list[str]:
    """把目标日程按 after.valid_from 重新展开，与「其余日程的当前占用」跑硬约束。

    返回违规说明列表（空 = 无硬冲突）。纯读 + 内存算，不碰任何 DB 写。

    调用链（与 m2.schedule_masks / b5.detect_demand_conflicts 同源口径）：
      expand_periodic（新展开，叠加 B9 例外）→ build_occupancy（剔除目标本身）
      → m2.build_context → hard_violations。
    """
    d_from = date.fromisoformat(new_valid_from)
    d_to = date.fromisoformat(target_row["valid_to"])
    terms = load_terms(conn, org_id)
    exceptions = load_exceptions(conn, org_id)

    # 目标日程按 after.valid_from 的新展开（week_parity/slot_bitmap 不变）。
    new_days = expand_periodic(
        target_row["week_parity"],
        hex_to_mask(target_row["slot_bitmap"]),
        new_valid_from,
        target_row["valid_to"],
        cfg,
        terms,
        d_from,
        d_to,
        exceptions,
    )
    # 叠加 B9 例外（cancel/move），与 m2.schedule_masks 同口径，避免误报。
    new_days = apply_schedule_exceptions(new_days, target_row["id"], exceptions, cfg=cfg)

    # 其余日程的当前占用 = 全组织 build_occupancy 剔除目标本身。
    entries = [
        e
        for e in build_occupancy(conn, org_id, d_from, d_to, cfg=cfg)
        if e.schedule_id != target_row["id"]
    ]
    ctx = build_context(conn, org_id, entries, cfg=cfg)

    people, resources = related_objects(conn, target_row["id"])
    involved = list(dict.fromkeys(people + resources))

    problems: list[str] = []
    for d, mask in sorted(new_days.items()):
        problems.extend(hard_violations(ctx, involved, d, mask))
    return problems


# ---------------------------------------------------------------------------
# 落库（本卡唯一的写路径）
# ---------------------------------------------------------------------------


def apply_change(conn: sqlite3.Connection, req: sqlite3.Row) -> dict:
    """落库：仅当 target_type='schedule' + op='update' 时改 valid_from 一列。

    其他组合一律 raise ValueError（不静默跳过）。返回落库后 after 快照
    （供 change_events.detail_json 审计）。**不 commit**——由 approve handler
    在同一事务里 commit，保证「校验→落库→转 applied→记事件」原子。
    """
    if req["target_type"] != _SUPPORTED_TARGET_TYPE or req["op"] != _SUPPORTED_OP:
        raise ValueError(
            f"apply_change 只支持 target_type='schedule'+op='update'，"
            f"收到 target_type={req['target_type']!r} op={req['op']!r}"
        )
    payload = request_payload(req)
    new_valid_from = payload["after"]["valid_from"]
    target_id = req["target_id"]

    # 🔴 校验日程属于 req.org_id（跨组织/已删除的日程绝不 UPDATE）。
    row = conn.execute(
        "SELECT id FROM schedules WHERE id = ? AND org_id = ?",
        (target_id, req["org_id"]),
    ).fetchone()
    if row is None:
        raise ValueError(f"日程 {target_id!r} 不存在或不属于组织 {req['org_id']!r}")

    conn.execute(
        "UPDATE schedule_periodic SET valid_from = ? WHERE schedule_id = ?",
        (new_valid_from, target_id),
    )
    # 落库后 SELECT after（供审计与答辩演示）。
    row = conn.execute(
        "SELECT valid_from FROM schedule_periodic WHERE schedule_id = ?", (target_id,)
    ).fetchone()
    return {"valid_from": row["valid_from"] if row else None}


def record_event(
    conn: sqlite3.Connection,
    request_id: str,
    actor_id: str,
    action: str,
    detail_json: str | None = None,
) -> None:
    """追加一条审计事件（不 commit）。action ∈ submit/approve/reject/withdraw/apply/fail。"""
    conn.execute(
        "INSERT INTO change_events(id, request_id, actor_id, action, detail_json)"
        " VALUES (?,?,?,?,?)",
        (new_event_id(), request_id, actor_id, action, detail_json),
    )
