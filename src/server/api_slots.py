"""B 批·局部补排后端：GET /api/slots（只读）——把 m2.find_slots() 结果透出给前端。

契约（卡面 B 批·局部补排后端）：
    GET /api/slots?target=<对象id>&from=YYYY-MM-DD&to=YYYY-MM-DD&duration=2&limit=8

用途与设计意图（答辩口径）：
  1. m2.find_slots() 已在 main（M3 局部补排），但路由表没有 /api/slots，
     现场网页看不见「该补排在哪」。本文件只做一件事：注册**只读**路由，
     把 find_slots 的候选透出。**不写任何表、不新增 POST、不做「直接落位」**。
  2. 🔴 「哪几节可排」由 find_slots 内部 effective_slot_config(exc, cfg, iso)
     （occupancy.py:524）唯一决定。本文件**绝不自算节次**——重复判 = 冬季作息
     那天给出第 8 节、排不进去且不报错（静默失效，本项目最痛形态）。
  3. 🔴 cfg 传 None（同 scripts/demo_fill.py:109 口径）——让 find_slots 自己
     load_slot_config，确保与演示脚本逐项一致。
  4. 🔴 跨组织越权出口：target 可以是 group（班级）或 user（教师），而
     K.resolve_authorized_org 吃的是 group、且教师 id 不在 authorized_groups
     （只含 group id）——直接复用会误 403。故先查 target 的 org_id（查不到→400
     「对象不存在」），再校验用户对该 org 有权限（admin=自己 org ∪ 授权组 org；
     非 admin=authorized_groups 各组所属 org 并集）。语义=越权访问别组织数据→
     403，绝不是 200 空结果。

签名约定（写死）：handle(method, path, query, body, headers, context=None)。
只用 Python 标准库。
"""

from __future__ import annotations

from datetime import date

from . import auth, config
from .http_api import ApiError
from scheduler import authz
from scheduler.solver import m2

#: 单次请求允许的最大日期跨度（天，含首尾）。
MAX_SPAN_DAYS = 31

#: duration 上界 = slots.MAX_PERIODS_PER_DAY（与 find_slots 内部上界一致）。
MAX_DURATION = 16

#: limit 上界（卡面写死）。
MAX_LIMIT = 50


def _param(query: dict, name: str) -> str | None:
    """从 query 取单值参数（parse_qs 给出 list；缺省/空 → None）。"""
    vals = query.get(name)
    if not vals:
        return None
    return vals[0]


def _required(query: dict, name: str) -> str:
    """必填参数；缺省/空串 → 400。"""
    v = _param(query, name)
    if v is None or v == "":
        raise ApiError(400, f"缺少参数 {name}")
    return v


def _parse_date(value: str, name: str) -> date:
    """YYYY-MM-DD → date；非法 → 400。"""
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ApiError(400, f"参数 {name} 必须是 YYYY-MM-DD，收到 {value!r}")


def _parse_int(query: dict, name: str, default: int, lo: int, hi: int) -> int:
    """可选整数参数；缺省 → default；非法/越界 → 400。"""
    v = _param(query, name)
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except ValueError:
        raise ApiError(400, f"参数 {name} 必须是整数，收到 {v!r}")
    if not (lo <= n <= hi):
        raise ApiError(400, f"参数 {name} 必须在 {lo}..{hi} 之间，收到 {n}")
    return n


def _target_org(conn, target: str) -> str:
    """target → 所属 org_id；查不到 → 400「对象不存在」（⛔ 不许静默空列表）。"""
    row = conn.execute("SELECT org_id FROM objects WHERE id = ?", (target,)).fetchone()
    if row is None:
        raise ApiError(400, f"对象不存在：{target}")
    return row["org_id"]


def _org_authorized(conn, user_id: str, target_org: str) -> bool:
    """校验用户对 target 所属组织有权限（跨组织越权 → False → 403）。

    语义（卡面核实点1）：
      - 任何角色：authorized_groups 各组所属 org 的并集（含 admin 跨组织显式授权、
        及 admin 本组织所有组——admin 的 authorized_groups 已隐式含本组织全组）；
      - admin 兜底：本组织即使没有任何 group 行（或授权集为空），仍隐式 own 本组织，
        故额外比对 authz._user_org_id。
    绝不允许「越权访问别组织数据 → 200 空结果」，必须是 403。
    """
    for gid in authz.authorized_groups(conn, user_id):
        row = conn.execute("SELECT org_id FROM objects WHERE id = ?", (gid,)).fetchone()
        if row is not None and row["org_id"] == target_org:
            return True
    if authz._user_role(conn, user_id) == "admin":
        own = authz._user_org_id(conn, user_id)
        if own is not None and own == target_org:
            return True
    return False


def _format_candidate(c: dict) -> dict:
    """候选 → API 出参：删掉周级 mask（前端不做位运算），保留 mask_hex / periods 等。"""
    return {k: v for k, v in c.items() if k != "mask"}


@auth.require_auth
def list_slots(method, path, query, body, headers, context=None):
    """GET /api/slots?target=&from=&to=&duration=&limit=（只读）。

    成功 200：{"ok":true,"target":...,"duration":...,"count":N,"candidates":[...]}
    全满：200 + count=0（空 candidates 是正常业务态，不是错误）。
    401 未登录（@require_auth，嵌套格式）；403 跨组织；400 参数错误/对象不存在。
    """
    user_id = context["user_id"]

    target = _required(query, "target")
    d0 = _parse_date(_required(query, "from"), "from")
    d1 = _parse_date(_required(query, "to"), "to")
    duration = _parse_int(query, "duration", default=1, lo=1, hi=MAX_DURATION)
    limit = _parse_int(query, "limit", default=8, lo=1, hi=MAX_LIMIT)

    if d0 > d1:
        raise ApiError(
            400, f"起始日期 from={d0.isoformat()} 晚于结束日期 to={d1.isoformat()}"
        )
    if (d1 - d0).days + 1 > MAX_SPAN_DAYS:
        raise ApiError(400, f"日期跨度不能超过 {MAX_SPAN_DAYS} 天")

    conn = config.open_db()
    try:
        org_id = _target_org(conn, target)
        if not _org_authorized(conn, user_id, org_id):
            raise ApiError(403, "无权访问该组织的数据")

        # cfg=None：与 demo_fill 同口径，让 find_slots 自己 load_slot_config +
        # 内部 effective_slot_config（occupancy.py:524）决定「哪几节可排」。
        candidates = m2.find_slots(
            conn,
            org_id,
            target=target,
            date_range=(d0, d1),
            duration=duration,
            limit=limit,
            cfg=None,
        )
        items = [_format_candidate(c) for c in candidates]
        return {
            "ok": True,
            "target": target,
            "duration": duration,
            "count": len(items),
            "candidates": items,
        }
    finally:
        conn.close()


ROUTES = {
    ("GET", "/api/slots"): list_slots,
}
