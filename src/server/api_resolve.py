"""B4（S9）：/api/resolve 冲突消解 API。

GET  /api/resolve?group=&mode=  → top-3 消解方案预览（docs/07 §6.1）
POST /api/resolve                → 执行一键消解（迭代）
限频：每 token 每分钟 10 次，超限 429（docs/07 §6）

用途与设计意图（答辩口径）：
  1. GET 预览：把 resolve() 给出的「每处冲突若干方案」扁平化、去重、
     按代价升序取前 3，并附 diff_text / impact / moves / warnings。
     三个硬字段（diff_text / impact / 去重）缺一前端就没法比较（§6.1）。
  2. POST 应用：迭代「找冲突 → 过滤不可动方案 → 应用最优 → 重扫」，
     直到无方案或冲突数不再下降；响应给 applied / remaining_before /
     remaining_after / rounds / filtered_out_count（+ termination 三态）。
  3. 可动集合（AGENTS §3 例外 / docs/07）：group 决定哪些日程「可动」，
     动了不可动日程的方案一律过滤，保证将来 B2 鉴权接入后越权不越界。
  4. 限频：进程内滑动窗口，按 token 计数；context=None（B4 未登录）时
     以 Authorization 头取 token，缺省归入匿名桶。

签名约定（写死，不得改）：
  handle(method, path, query, body, headers, context=None)

只用 Python 标准库。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import timedelta

from . import config
from .http_api import ApiError, json_body

from scheduler.occupancy import scan
from scheduler.slots import load_slot_config
from scheduler.solver import m2

# ---------------------------------------------------------------------------
# 限频（每 token 每分钟 10 次）
# ---------------------------------------------------------------------------

RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60

_rate_lock = threading.Lock()
_rate_buckets: dict[str, deque[float]] = {}


def _client_token(headers: dict) -> str:
    """从 Authorization 头取 token；缺省归入匿名桶（B4 未登录）。"""
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return "anonymous"


def _rate_limited(token: str) -> bool:
    """滑动窗口限频：最近 60 秒内已满 RATE_LIMIT 次 → True（应返回 429）。"""
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets.setdefault(token, deque())
        while bucket and now - bucket[0] >= RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT:
            return True
        bucket.append(now)
        return False


# ---------------------------------------------------------------------------
# 模式 → 搜索窗口（docs/07 §6：minimal 默认，行为与改动前一致）
# ---------------------------------------------------------------------------

MODE_WINDOW_DAYS = {"minimal": 5, "balanced": 7, "optimal": 14}

#: 每处冲突最多生成几个候选（需足够覆盖「移动冲突的任一侧」，供过滤后仍有方案）
MAX_PLANS_PER_CONFLICT = 8


def _slots_hex(slots) -> str:
    """把 (dow, period) 列表转成 API 的「单日掩码」十六进制字符串。

    约定（docs/07 §5 / §6.1）：period N → 位 N（1-based），
    0x2=第1节、0x8=第3节、0x6=第1+2节。与 slots.bit 的 0-based 周掩码不同。
    """
    return "0x" + format(m2.day_slots_mask(slots), "x")


def _parse_mode(query: dict) -> str:
    mode = (query.get("mode") or [""])[0] or "minimal"
    if mode not in MODE_WINDOW_DAYS:
        raise ApiError(400, f"mode 必须是 minimal/balanced/optimal，收到 {mode!r}")
    return mode


def _parse_group(query: dict) -> str | None:
    group = (query.get("group") or [""])[0]
    return group or None


def _scope(conn, group: str | None) -> tuple[str, set[str] | None]:
    """解析 group 参数 → (org_id, movable_ids)。movable_ids=None 表示全体可动。"""
    if group:
        row = conn.execute("SELECT org_id FROM objects WHERE id = ?", (group,)).fetchone()
        if row is None:
            raise ApiError(404, "组不存在")
        return row["org_id"], m2.movable_schedule_ids(conn, group)
    row = conn.execute("SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise ApiError(404, "无组织数据")
    return row["id"], None  # 无 group → 不限制可动集合


def _resolve_window(cfg):
    """以 cfg.today 所在周（周一到周日）为求解窗口（docs/07 §4：本周）。"""
    start = config.monday_of(cfg.today)
    return start, start + timedelta(days=6)


def _format_move(mv) -> dict:
    return {
        "schedule_id": mv.schedule_id,
        "title": mv.title,
        "from": {"date": mv.old_date.isoformat(), "slots": _slots_hex(mv.old_slots)},
        "to": {"date": mv.new_date.isoformat(), "slots": _slots_hex(mv.new_slots)},
    }


def _format_plan(conn, plan, index: int) -> dict:
    return {
        "index": index,
        "cost": plan.cost,
        "diff_text": m2.plan_diff_text(conn, plan),
        "impact": m2.plan_impact(conn, plan),
        "moves": [_format_move(mv) for mv in plan.moves],
        "warnings": m2.plan_warnings(conn, plan),
    }


def resolve_get(method, path, query, body, headers, context=None):
    if _rate_limited(_client_token(headers)):
        raise ApiError(429, "请求过于频繁，请稍后再试")

    mode = _parse_mode(query)
    group = _parse_group(query)
    cfg = config.get_current_config()
    date_from, date_to = _resolve_window(cfg)

    conn = config.open_db()
    try:
        org_id, movable_ids = _scope(conn, group)
        slot_cfg = load_slot_config(conn, org_id)
        results = m2.resolve(
            conn, org_id, date_from, date_to,
            cfg=slot_cfg,
            window_days=MODE_WINDOW_DAYS[mode],
            max_plans=MAX_PLANS_PER_CONFLICT,
        )
        plans, _ = m2.filter_movable(results, movable_ids)
        top = m2.top_plans(plans, n=3)
        formatted = [_format_plan(conn, p, i) for i, p in enumerate(top)]

        if formatted:
            termination = "ok"
        elif results:
            termination = "no_solution"  # 有冲突但可动集合过滤后无可行方案
        else:
            termination = "ok"  # 本就无冲突
        return {"plans": formatted, "termination": termination}
    finally:
        conn.close()


def resolve_post(method, path, query, body, headers, context=None):
    if _rate_limited(_client_token(headers)):
        raise ApiError(429, "请求过于频繁，请稍后再试")

    data = json_body(body)
    plan_index = data.get("plan_index")
    if plan_index is not None and (
        isinstance(plan_index, bool) or not isinstance(plan_index, int) or plan_index < 0
    ):
        raise ApiError(400, "plan_index 必须是非负整数")

    mode = _parse_mode(query)
    group = _parse_group(query)
    cfg = config.get_current_config()
    date_from, date_to = _resolve_window(cfg)

    conn = config.open_db()
    try:
        org_id, movable_ids = _scope(conn, group)
        slot_cfg = load_slot_config(conn, org_id)
        remaining_before = scan(conn, org_id, date_from, date_to, cfg=slot_cfg).count

        applied, remaining_after, filtered_out = m2.resolve_and_apply_filtered(
            conn, org_id, date_from, date_to, movable_ids,
            cfg=slot_cfg,
            window_days=MODE_WINDOW_DAYS[mode],
        )

        if remaining_after == 0:
            termination = "ok"
        elif applied == 0:
            termination = "no_solution"
        else:
            termination = "best_effort"

        return {
            "applied": applied,
            "remaining_before": remaining_before,
            "remaining_after": remaining_after,
            "rounds": applied,
            "filtered_out_count": filtered_out,
            "termination": termination,
        }
    finally:
        conn.close()


ROUTES = {
    ("GET", "/api/resolve"): resolve_get,
    ("POST", "/api/resolve"): resolve_post,
}
