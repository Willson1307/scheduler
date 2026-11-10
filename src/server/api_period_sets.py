"""B-1：作息方案（period_sets / set_slots）最小接口。

用途与设计意图（答辩口径）：
  夏冬作息、考试周这类「同一天换一套节次时间」的需求，走多套**方案**表达：
    · 冬令时整体推迟 → 同一套方案改 start_time
    · 冬季取消第 8 节 → 该方案不写 idx=8 那一行（或写 enabled=0）
    · 考试周只上上午 4 节 → 该方案只写 idx 1..4
  位图不用改：`slots.py:24-26` 的 MAX_PERIODS_PER_DAY=16 是定宽上界，
  第 k 节永远是第 k 位，所以「几套节次数量不同的作息」可以共存于同一个 112 位掩码。

路由（精确匹配、无 path 参数，id 一律走 query —— http_api.py:110）：
  POST /api/period-sets              创建方案（含节次行）
  GET  /api/period-sets              列表
  GET  /api/period-sets/detail?id=   单方案详情（含全部节次行）

🔴 鉴权（不重复 B3 那次「硬编码 context=None」的事故）：
  · READ  = 登录 + 组织隔离（B3.resolve_authorized_org）
  · WRITE = `own` 档（docs/06：改「结构与规则」是永久类改动）。
    authz.ROLE_CEILING 把 teacher 封顶到 propose → 教师建方案一律 403。
  · 跨组织按 id 访问 → 404（「按 id 访问具体资源」语义，与 docs/07 §9 一致）。

🔴 绑定「哪天用哪套」不在本文件：用现成的
  schedule_exceptions(scope='day', mode='special', date=..., set_id=...)
  卡面明确 ⛔ 不新建「日期→方案」表。

只用 Python 标准库。
"""

from __future__ import annotations

import re

from . import auth, config
from . import b3_kernels as B3
from .http_api import json_body
from scheduler import authz
from scheduler.db import new_id
from scheduler.occupancy import ensure_period_set_tables
from scheduler.slots import MAX_PERIODS_PER_DAY

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _err(status: int, code: str, message: str):
    """契约 §7 的统一嵌套错误体（不用 ApiError 的扁平体，前端只需一套解析）。"""
    return status, {"error": {"code": code, "message": message}}


def _require_own(conn, user_id: str, group_id: str):
    """写入口的档位闸：不满足 own 一律 403（教师天花板 = propose）。"""
    if not authz.can(conn, user_id, group_id, "own"):
        raise PermissionError("需要 own 档权限（修改组织结构与规则）才能创建作息方案")


def _parse_slot(raw, lineno: int) -> dict:
    """校验并规范化一条 set_slots 行；任何不合法都 400，不静默丢弃。"""
    if not isinstance(raw, dict):
        return {"__error__": f"slots[{lineno}] 必须是对象"}
    dow = raw.get("day_of_week")
    idx = raw.get("idx")
    # bool 是 int 的子类，显式排掉，否则 True 会被当成 1
    if isinstance(dow, bool) or not isinstance(dow, int) or not 1 <= dow <= 7:
        return {"__error__": f"slots[{lineno}].day_of_week 必须是 1..7 的整数"}
    if isinstance(idx, bool) or not isinstance(idx, int) or not 1 <= idx <= MAX_PERIODS_PER_DAY:
        return {
            "__error__": (
                f"slots[{lineno}].idx 必须是 1..{MAX_PERIODS_PER_DAY} 的整数"
                f"（上界取 slots.MAX_PERIODS_PER_DAY，不许按当前节数写死）"
            )
        }
    start, end = raw.get("start_time"), raw.get("end_time")
    if not isinstance(start, str) or not _HHMM.match(start):
        return {"__error__": f"slots[{lineno}].start_time 必须是 'HH:MM'"}
    if not isinstance(end, str) or not _HHMM.match(end):
        return {"__error__": f"slots[{lineno}].end_time 必须是 'HH:MM'"}
    if start >= end:
        return {"__error__": f"slots[{lineno}] 的 end_time 必须晚于 start_time"}
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        return {"__error__": f"slots[{lineno}].enabled 必须是布尔值"}
    return {
        "day_of_week": dow,
        "idx": idx,
        "start_time": start,
        "end_time": end,
        "enabled": enabled,
    }


@auth.require_auth
def create_period_set(method, path, query, body, headers, context=None):
    """POST /api/period-sets —— 建方案 + 批量插节次行（同事务）。"""
    data = json_body(body)
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return _err(400, "bad_request", "缺少 name（方案名，如「冬季作息」）")
    name = name.strip()
    note = data.get("note")
    if note is not None and not isinstance(note, str):
        return _err(400, "bad_request", "note 必须是字符串")

    raw_slots = data.get("slots")
    if not isinstance(raw_slots, list) or not raw_slots:
        return _err(400, "bad_request", "slots 必须是非空数组（方案至少要有节次行）")

    rows: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for i, raw in enumerate(raw_slots):
        parsed = _parse_slot(raw, i)
        if "__error__" in parsed:
            return _err(400, "bad_request", parsed["__error__"])
        key = (parsed["day_of_week"], parsed["idx"])
        if key in seen:
            # UNIQUE(set_id, day_of_week, idx) 会在 INSERT 时炸成 500，这里先明确 400
            return _err(
                400,
                "bad_request",
                f"slots 里 (day_of_week={key[0]}, idx={key[1]}) 重复——同一节只能写一行，"
                f"要「没有这一节」请用 enabled:false 或不写这行",
            )
        seen.add(key)
        rows.append(parsed)

    conn = config.open_db()
    try:
        # 🔴 旧库兜底：已存在的 data/*.db 不会自动补新表（ensure_database 只在
        # 库文件缺失时 seed）→ 缺表时本接口会 500。与 b5/b8 的 ensure_* 同范式。
        ensure_period_set_tables(conn)
        effective_group, org_id = B3.resolve_authorized_org(
            conn, context["user_id"], data.get("group")
        )
        try:
            _require_own(conn, context["user_id"], effective_group)
        except PermissionError as exc:
            return _err(403, "forbidden", str(exc))

        set_id = new_id("set")
        conn.execute(
            "INSERT INTO period_sets (id, org_id, name, note) VALUES (?, ?, ?, ?)",
            (set_id, org_id, name, note),
        )
        for r in rows:
            conn.execute(
                "INSERT INTO set_slots"
                "(id, set_id, day_of_week, idx, start_time, end_time, enabled)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("ss"),
                    set_id,
                    r["day_of_week"],
                    r["idx"],
                    r["start_time"],
                    r["end_time"],
                    1 if r["enabled"] else 0,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "set_id": set_id,
        "name": name,
        "org_id": org_id,
        "slot_count": len(rows),
        "enabled_slot_count": sum(1 for r in rows if r["enabled"]),
    }


@auth.require_auth
def list_period_sets(method, path, query, body, headers, context=None):
    """GET /api/period-sets —— 本组织方案列表（只读，任意成员可看）。"""
    group = (query.get("group") or [""])[0] or None
    conn = config.open_db()
    try:
        ensure_period_set_tables(conn)
        _effective_group, org_id = B3.resolve_authorized_org(
            conn, context["user_id"], group
        )
        rows = conn.execute(
            "SELECT p.id, p.name, p.note, p.created_at,"
            "       COUNT(s.id) AS slot_count,"
            "       COALESCE(SUM(CASE WHEN s.enabled = 1 THEN 1 ELSE 0 END), 0)"
            "           AS enabled_slot_count"
            " FROM period_sets p LEFT JOIN set_slots s ON s.set_id = p.id"
            " WHERE p.org_id = ? GROUP BY p.id ORDER BY p.created_at, p.id",
            (org_id,),
        ).fetchall()
        items = [
            {
                "id": r["id"],
                "name": r["name"],
                "note": r["note"],
                "created_at": r["created_at"],
                "slot_count": r["slot_count"],
                "enabled_slot_count": r["enabled_slot_count"],
            }
            for r in rows
        ]
    finally:
        conn.close()
    return {"ok": True, "count": len(items), "items": items}


@auth.require_auth
def period_set_detail(method, path, query, body, headers, context=None):
    """GET /api/period-sets/detail?id=set_x —— 单方案详情（含全部节次行）。

    🔴 id 走 query：分发层是 table.get((method, path))，没有 path 参数。
    """
    set_id = (query.get("id") or [""])[0] or None
    if not set_id:
        return _err(400, "bad_request", "缺少 id（GET /api/period-sets/detail?id=set_x）")
    group = (query.get("group") or [""])[0] or None
    conn = config.open_db()
    try:
        ensure_period_set_tables(conn)
        _effective_group, org_id = B3.resolve_authorized_org(
            conn, context["user_id"], group
        )
        row = conn.execute(
            "SELECT id, org_id, name, note, created_at FROM period_sets"
            " WHERE id = ? AND org_id = ?",
            (set_id, org_id),
        ).fetchone()
        if row is None:
            # 跨组织 / 不存在都 404：不泄漏「别处存在这个 id」
            return _err(404, "not_found", f"作息方案不存在：{set_id}")
        slots = [
            {
                "day_of_week": s["day_of_week"],
                "idx": s["idx"],
                "start_time": s["start_time"],
                "end_time": s["end_time"],
                "enabled": bool(s["enabled"]),
            }
            for s in conn.execute(
                "SELECT day_of_week, idx, start_time, end_time, enabled FROM set_slots"
                " WHERE set_id = ? ORDER BY day_of_week, idx",
                (set_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "ok": True,
        "set": {
            "id": row["id"],
            "name": row["name"],
            "note": row["note"],
            "created_at": row["created_at"],
        },
        "slots": slots,
        "slot_count": len(slots),
    }


ROUTES = {
    ("POST", "/api/period-sets"): create_period_set,
    ("GET", "/api/period-sets"): list_period_sets,
    ("GET", "/api/period-sets/detail"): period_set_detail,
}
