"""B4（S9）：/api/resolve 冲突消解 API。

GET  /api/resolve?group=&mode=  → top-3 消解方案预览（docs/07 §6.1）
POST /api/resolve                → 执行一键消解（迭代）
POST /api/resolve/revert?id=     → 撤销一次消解（按应用前快照逐字段回写）
POST /api/resolve/commit?id=     → 固化（仅 admin / own 档）
GET  /api/resolve/snapshots?group= → 最近 20 条快照
POST /api/resolve/explain?snapshot_id= → 把这次消解翻成一句人话（v1.1，只读）
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
  4. 限频：进程内滑动窗口，按 token 计数（B4 跟进 rjsdbj 已接入 @require_auth，
     未登录 401；限频以 Authorization 头取 token，缺省归入匿名桶）。
  5. 鉴权（B4 跟进 rjsdbj，照 P0 卡 a21588b 的 B3 范式）：
     @require_auth 拦 401；`_scope` 走 `K.resolve_authorized_org` 拦跨组织 403
     （GET 预览与 POST 执行同口径，杜绝「读泄漏 + 写越权」），
     movable 一律按 effective_group 闭包（越权不越界）。
  6. 闭环（卡 rvdL9a，例外 ⑧）：`m2.apply_plan()` 是**原地 UPDATE**，
     点一次不可逆。本文件在 `resolve_and_apply_filtered()` 调用**前后**加一层
     快照（`b4_kernels.capture` / `write_snapshot`），支撑「撤销本次消解」
     与「固化此版本」。🔴 m2.py 一行未改 —— 它是 386 项测试锁定的核心。
     权限：apply / revert = 登录 + 同组织；commit = 该组 own 档（admin 短路）。

签名约定（写死，不得改）：
  handle(method, path, query, body, headers, context=None)

只用 Python 标准库。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import timedelta

from . import auth
from . import b3_kernels as K
from . import b4_kernels as S
from . import b_explain_kernels as X
from . import config
from .http_api import ApiError, json_body

from scheduler.occupancy import scan
from scheduler.slots import decode_mask, hex_to_mask, load_slot_config
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


def _scope(conn, user_id: str, group: str | None) -> tuple[str, set[str]]:
    """鉴权 + 解析 group 参数 → (org_id, movable_ids)。

    🔴 跨组织越权出口（B4 跟进 rjsdbj，照 P0 卡 a21588b 的 B3 范式修复）：
      - 原实现 `SELECT id FROM organizations ORDER BY id LIMIT 1` 完全无鉴权，
        且 org id 是 uuid4，多组织下非确定性返回**别家**数据——
        GET 泄漏别组织可动集合，POST 直接**改写**别组织课表（IDOR 写越权）。
      - 现一律走 `K.resolve_authorized_org`：group 不在 authorized_groups → 403；
        group=None 时用 authz.default_group_id 定位用户自己的默认组（同口径）。

    movable 语义（B2 鉴权接入后收紧，与文件头「越权不越界」预留意图对齐）：
      可动集合一律按 effective_group 闭包计算（含默认组）。
      admin 默认组 = 根组 → 闭包覆盖全组织，行为与旧「None=全体可动」一致；
      低权限用户默认组下只能动自己组闭包内的日程。
    """
    effective_group, org_id = K.resolve_authorized_org(conn, user_id, group)
    return org_id, m2.movable_schedule_ids(conn, effective_group)


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


@auth.require_auth
def resolve_get(method, path, query, body, headers, context=None):
    if _rate_limited(_client_token(headers)):
        raise ApiError(429, "请求过于频繁，请稍后再试")

    mode = _parse_mode(query)
    group = _parse_group(query)
    cfg = config.get_current_config()
    date_from, date_to = _resolve_window(cfg)

    conn = config.open_db()
    try:
        org_id, movable_ids = _scope(conn, context["user_id"], group)
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


@auth.require_auth
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
        org_id, movable_ids = _scope(conn, context["user_id"], group)
        slot_cfg = load_slot_config(conn, org_id)
        remaining_before = scan(conn, org_id, date_from, date_to, cfg=slot_cfg).count

        # 🔴 快照必须在写入**之前**抓（例外 ⑧）：apply_plan 是原地 UPDATE，
        #    抓晚一步拿到的就是消解后的状态，撤销等于没撤。
        before_payload = S.capture(conn, org_id)

        applied, remaining_after, filtered_out = m2.resolve_and_apply_filtered(
            conn, org_id, date_from, date_to, movable_ids,
            cfg=slot_cfg,
            window_days=MODE_WINDOW_DAYS[mode],
        )

        snapshot_id = S.write_snapshot(
            conn,
            org_id,
            context["user_id"],
            before_payload,
            applied,
            remaining_before,
            remaining_after,
        )

        if remaining_after == 0:
            termination = "ok"
        elif applied == 0:
            termination = "no_solution"
        else:
            termination = "best_effort"

        # 🔴 原有六个字段一个都不能少（前端 renderResolveResult 与契约测试在吃）；
        #    snapshot_id 是新增字段，非破坏性。
        return {
            "applied": applied,
            "remaining_before": remaining_before,
            "remaining_after": remaining_after,
            "rounds": applied,
            "filtered_out_count": filtered_out,
            "termination": termination,
            "snapshot_id": snapshot_id,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 闭环：撤销 / 固化 / 快照列表（卡 rvdL9a）
# ---------------------------------------------------------------------------


def _snapshot_id(query: dict) -> str:
    """取 ?id=snap_x。缺失 → 400（路由是精确匹配，id 只能走 query）。"""
    sid = (query.get("id") or [""])[0].strip()
    if not sid:
        raise ApiError(400, "缺少 id 参数（要撤销/固化哪一条快照）")
    return sid


@auth.require_auth
def resolve_revert(method, path, query, body, headers, context=None):
    """撤销一次消解：按应用前快照逐字段回写。

    权限 = 登录 + 同组织（教师可用）。固化的快照 → 409（不许静默忽略）。
    已撤销再撤 → 200 幂等，**不写任何数据**。
    """
    sid = _snapshot_id(query)
    group = _parse_group(query)
    conn = config.open_db()
    try:
        org_id, _movable = _scope(conn, context["user_id"], group)
        result = S.revert(conn, org_id, sid)
        # 回写后用同一口径重算冲突数，证明「还原了」不是靠自述
        cfg = config.get_current_config()
        date_from, date_to = _resolve_window(cfg)
        slot_cfg = load_slot_config(conn, org_id)
        conflicts_after = scan(conn, org_id, date_from, date_to, cfg=slot_cfg).count
        return {
            "reverted": True,
            "snapshot_id": sid,
            "conflicts_after": conflicts_after,
            "already_reverted": result["already_reverted"],
            "restored": result["restored"],
            "missing": result["missing"],
        }
    finally:
        conn.close()


@auth.require_auth
def resolve_commit(method, path, query, body, headers, context=None):
    """固化某个版本：仅 admin 全局角色 或 该组 own 档，否则 403。"""
    sid = _snapshot_id(query)
    group = _parse_group(query)
    conn = config.open_db()
    try:
        effective_group, org_id = K.resolve_authorized_org(conn, context["user_id"], group)
        # 先判组织归属（不存在/别组织的 id → 404），再判档位（→ 403）
        S.get_row(conn, org_id, sid)
        S.require_own_level(conn, context["user_id"], effective_group)
        S.commit(conn, org_id, sid)
        return {"committed": True, "snapshot_id": sid}
    finally:
        conn.close()


@auth.require_auth
def resolve_snapshots_get(method, path, query, body, headers, context=None):
    """最近 20 条快照（org 级，按登录用户可访问的组定位组织）。"""
    group = _parse_group(query)
    conn = config.open_db()
    try:
        org_id, _movable = _scope(conn, context["user_id"], group)
        return {"snapshots": S.list_recent(conn, org_id)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v1.1（卡 rHrY9D）出向 D1：消解结果 → 人话解释（只读，绝不写库）
# ---------------------------------------------------------------------------

_DOW = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 事实里最多列几条改动（再多就成流水账，老师只要知道「动了哪几条」）。
_EXPLAIN_MAX_CHANGES = 5


def _periods_of(bitmap: str) -> list[tuple[int, int]]:
    """周掩码文本 → [(周几, 第几节)]（1-based 节次，给人看的口径）。

    🔴 这里是**周掩码**（0-based 位序，位号 =(周几-1)*16+(节次-1)），
    不是 API 出参的单日掩码 —— 两者混用会让整页错位且不报错。
    黄金对照：周三第 1 节 = 周掩码 `0x100000000` = 单日掩码 `0x2`，
    下面的 `_periods_of` 只吃前者。
    """
    try:
        mask = hex_to_mask(str(bitmap or "0"))
    except (TypeError, ValueError):
        return []
    return [(dow, idx + 1) for dow, idx in decode_mask(mask)]


def _fmt_periods(items) -> str:
    if not items:
        return "不再占用"
    parts = []
    for dow, period in sorted(items):
        label = _DOW[dow - 1] if 1 <= dow <= 7 else f"周{dow}"
        parts.append(f"{label}第{period}节")
    return "、".join(parts)


def _has_consecutive(items) -> bool:
    """同一周几是否出现了相邻两节（连堂）—— 纯位运算，不交给模型判断。"""
    by_dow: dict[int, set[int]] = {}
    for dow, period in items:
        by_dow.setdefault(dow, set()).add(period)
    for periods in by_dow.values():
        for p in periods:
            if (p + 1) in periods:
                return True
    return False


def _schedule_meta(conn, schedule_id: str) -> tuple[str, str]:
    """(课名, 目标对象名)。查不到就给中性占位，⛔ 不猜。"""
    row = conn.execute(
        "SELECT s.title AS title, o.name AS oname"
        " FROM schedules s"
        " LEFT JOIN schedule_participants sp"
        "   ON sp.schedule_id = s.id AND sp.role = 'target'"
        " LEFT JOIN objects o ON o.id = sp.object_id"
        " WHERE s.id = ? LIMIT 1",
        (schedule_id,),
    ).fetchone()
    if row is None:
        return "（已不存在的日程）", "—"
    return (row["title"] or "未命名"), (row["oname"] or "—")


def _diff_facts(conn, org_id: str, row) -> dict:
    """快照（消解前）vs 当前库（消解后）→ 结构化事实。

    🔴 为什么能这么比：`m2.apply_plan()` 是原地 UPDATE，`b4_kernels.capture`
    在写入**前**抓了全量 bitmap/起止时刻，所以「改了什么」可以逐字段 diff 出来，
    不需要改领域层（m2 一行都不许改）。
    """
    try:
        before = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        before = {}
    after = S.capture(conn, org_id)

    before_map = {s["id"]: s for s in before.get("schedules") or [] if s.get("id")}
    changes: list[dict] = []
    extra: list[str] = []

    for item in after.get("schedules") or []:
        sid = item.get("id")
        old = before_map.get(sid)
        if not old:
            continue  # 快照之后新建的日程不归这次消解管
        title, obj = _schedule_meta(conn, sid)
        if item.get("kind") == "periodic":
            if str(old.get("bitmap")) == str(item.get("bitmap")):
                continue
            old_p = set(_periods_of(old.get("bitmap")))
            new_p = set(_periods_of(item.get("bitmap")))
            moved = old_p - new_p
            landed = new_p - old_p
            if not moved and not landed:
                continue
            changes.append({
                "object": obj,
                "title": title,
                "from": _fmt_periods(moved),
                "to": _fmt_periods(landed),
            })
            if _has_consecutive(new_p) and not _has_consecutive(old_p):
                extra.append("调整后这一天出现连堂（连续 2 节）")
        else:
            if (old.get("start_at"), old.get("end_at")) == (
                item.get("start_at"), item.get("end_at")
            ):
                continue
            changes.append({
                "object": obj,
                "title": title,
                "from": str(old.get("start_at") or "未设置"),
                "to": str(item.get("start_at") or "未设置"),
            })

    return {
        "applied": int(row["applied_count"] or 0),
        "before": int(row["remaining_before"] or 0),
        "after": int(row["remaining_after"] or 0),
        "changes": changes[:_EXPLAIN_MAX_CHANGES],
        "extra": extra,
    }


@auth.require_auth
def resolve_explain(method, path, query, body, headers, context=None):
    """POST /api/resolve/explain?snapshot_id=snap_x —— 把消解结果翻成一句人话。

    🔴 **只读**：只读快照行 + 读当前库比对，⛔ 不写任何表、不调用写接口。
    🔴 **不阻塞主流程**：本接口与 `POST /api/resolve` 是两条独立请求 ——
    前端先渲染结果卡片（数字、条目全部就位），再异步来取这一句解释；
    3 秒拿不到就继续显示模板句，不重排、不闪。
    🔴 任何失败（模型未配置 / 超时 / 数字不接地）都返回**模板句**且 HTTP 200，
    ⛔ 不返 5xx —— 解释不出来不是故障，是降级。
    """
    sid = (query.get("snapshot_id") or [""])[0].strip()
    if not sid:
        raise ApiError(400, "缺少 snapshot_id 参数（要解释哪一次消解）")

    group = _parse_group(query)
    conn = config.open_db()
    try:
        org_id, _movable = _scope(conn, context["user_id"], group)
        row = S.get_row(conn, org_id, sid)  # 跨组织 → 404（不区分，避免探测）

        names: list[str] = []
        try:
            got_names = X.object_names(conn, context["user_id"])
            names = list(got_names or {})
        except Exception:  # noqa: BLE001 —— 拿不到名单只影响令牌还原，不阻断
            names = []

        try:
            facts = _diff_facts(conn, org_id, row)
            explained = X.explain_resolve(facts, names=names)
        except Exception:  # noqa: BLE001 —— 解释失败也必须 200 + 模板句
            explained = {
                "explanation": "这次调整已完成，具体改动见上方条目。",
                "source": "template",
                "degraded": True,
            }

        return {
            "snapshot_id": sid,
            "explanation": explained["explanation"],
            "source": explained["source"],
            "degraded": explained["degraded"],
        }
    finally:
        conn.close()


ROUTES = {
    ("GET", "/api/resolve"): resolve_get,
    ("POST", "/api/resolve"): resolve_post,
    ("POST", "/api/resolve/revert"): resolve_revert,
    ("POST", "/api/resolve/commit"): resolve_commit,
    ("GET", "/api/resolve/snapshots"): resolve_snapshots_get,
    ("POST", "/api/resolve/explain"): resolve_explain,
}
