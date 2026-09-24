"""M2：冲突消解（最小改动搜索）。

设计意图：

    新学期全局排课（M1）一年只用一次，真正高频的是**微调**：
    某位老师请假、某个活动插进来、某节课要挪。
    这时候用户不想要"重新排一遍"，只想"改动最小地解决这一处冲突"。

    所以 M2 不做全局搜索，而是：
        对每一处冲突，枚举"把其中一条日程挪到哪"，
        按 **改动幅度 + 软约束代价** 排序，给出前 3 个方案让人选。

    搜索空间极小（一天十几个槽位 × 前后几天），暴力枚举 + 排序就够了，
    这也是把 M2 排在 M1 前面做的原因：最有用、最容易做对、最容易演示。

    ⚠️ 两类日程挪动的语义不同，这点必须讲清楚：
      · 一次性活动：只改那一天；
      · 周期日程：改的是每周的节次（等于调课，整个生效区间都跟着变），
        所以检查必须在**扫描区间内所有同周几的日期**上都通过。
        方案描述里会明确标注"每周生效"。

    ⚠️ 求解过程不调用任何大模型。大模型只在入口处负责"听懂人话、选模式"。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..occupancy import (
    OccupancyEntry,
    build_occupancy,
    find_conflicts,
    load_exceptions,
    load_terms,
    occupancy_map,
    expand_once,
    expand_periodic,
)
from ..slots import SlotConfig, bit, decode_mask, hex_to_mask, load_slot_config, popcount
from .constraints import SolverContext, hard_violations, load_objects, soft_cost


@dataclass
class Move:
    """一次挪动。periodic=True 时表示"每周生效"（调课），否则只改那一天。"""

    schedule_id: str
    title: str
    old_date: date
    old_slots: list[tuple[int, int]]
    new_date: date
    new_slots: list[tuple[int, int]]
    periodic: bool = False

    def describe(self) -> str:
        def fmt(d: date, ss: list[tuple[int, int]]) -> str:
            return f"{d.isoformat()} " + "、".join(
                f"周{'一二三四五六日'[w - 1]}第{i}节" for w, i in ss
            )

        tail = "（每周生效）" if self.periodic else ""
        return (
            f"{'调课' if self.periodic else '改期'}『{self.title}』: "
            f"{fmt(self.old_date, self.old_slots)} → {fmt(self.new_date, self.new_slots)}{tail}"
        )


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    cost: float = 0.0
    note: str = ""

    def describe(self) -> str:
        head = f"代价 {self.cost:.2f}" + (f"｜{self.note}" if self.note else "")
        return head + "\n" + "\n".join("  · " + m.describe() for m in self.moves)


@dataclass
class ResolveResult:
    conflict: object
    plans: list[Plan] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def related_objects(conn: sqlite3.Connection, schedule_id: str) -> tuple[list[str], list[str]]:
    """一条日程挪动时，谁的时间都要跟着挪。返回 (人员对象, 资源对象)。"""
    people: list[str] = []
    resources: list[str] = []
    for r in conn.execute(
        "SELECT object_id, role FROM schedule_participants WHERE schedule_id = ?",
        (schedule_id,),
    ).fetchall():
        oid = r["object_id"]
        row = conn.execute("SELECT kind FROM objects WHERE id = ?", (oid,)).fetchone()
        if row and row["kind"] == "group":
            for m in conn.execute(
                "SELECT descendant_id FROM object_edges WHERE ancestor_id = ? AND depth > 0",
                (oid,),
            ).fetchall():
                people.append(m["descendant_id"])
        people.append(oid)
    for r in conn.execute(
        "SELECT resource_id FROM schedule_resources WHERE schedule_id = ?", (schedule_id,)
    ).fetchall():
        resources.append(r["resource_id"])
    return people, resources


def schedule_masks(
    conn: sqlite3.Connection, schedule_id: str, cfg: SlotConfig, days: list[date]
) -> dict[date, int]:
    """某条日程在给定每一天占用了哪些节次。"""
    row = conn.execute("SELECT * FROM v_schedule_full WHERE id = ?", (schedule_id,)).fetchone()
    if row is None or not days:
        return {}
    lo, hi = min(days), max(days)
    if row["kind"] == "periodic":
        raw = expand_periodic(
            row["week_parity"],
            hex_to_mask(row["slot_bitmap"]),
            row["valid_from"],
            row["valid_to"],
            cfg,
            load_terms(conn, row["org_id"]),
            lo,
            hi,
            load_exceptions(conn, row["org_id"]),
        )
    else:
        raw = expand_once(row["start_at"], row["end_at"], cfg)
    return {d: m for d, m in raw.items() if d in set(days)}


def build_context(
    conn: sqlite3.Connection,
    org_id: str,
    entries: list[OccupancyEntry],
    *,
    cfg: SlotConfig | None = None,
) -> SolverContext:
    return SolverContext(
        cfg=cfg or load_slot_config(conn, org_id),
        objects=load_objects(conn, org_id),
        occupancy=occupancy_map(entries),
    )


def _candidate_masks(free: int, need: int, prefer_idx: int, limit: int = 8) -> list[int]:
    """从空闲掩码里挑 need 个槽位，优先挑节次序号接近原位置的。

    不穷举全部组合（会爆炸），按"与原节次的距离"贪心取前几个。
    这是有意的简化：M2 求的是"改动最小"，离原位置最近的几个候选已经足够好。
    """
    slots = decode_mask(free)
    if len(slots) < need:
        return []
    scored = sorted(slots, key=lambda s: (abs(s[1] - prefer_idx), s[0], s[1]))
    out: list[int] = []
    for start in range(0, len(scored) - need + 1):
        m = 0
        for w, i in scored[start : start + need]:
            m |= bit(w, i)
        if m and m not in out:
            out.append(m)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def resolve(
    conn: sqlite3.Connection,
    org_id: str,
    date_from: date,
    date_to: date,
    *,
    cfg: SlotConfig | None = None,
    window_days: int = 5,
    max_plans: int = 3,
    move_once_first: bool = True,
) -> list[ResolveResult]:
    """扫描冲突，并为每一处冲突给出消解方案。

    window_days: 一次性活动允许前后挪动的天数
    move_once_first: 优先挪动一次性活动（课表通常比活动更难动）——来自真实场景的启发式
    """
    cfg = cfg or load_slot_config(conn, org_id)
    all_days = _days(date_from, date_to)
    entries = build_occupancy(conn, org_id, date_from, date_to, cfg=cfg)
    conflicts = find_conflicts(entries)
    ctx = build_context(conn, org_id, entries, cfg=cfg)

    kinds = {
        r["id"]: r["kind"]
        for r in conn.execute("SELECT id, kind FROM schedules WHERE org_id = ?", (org_id,))
    }

    seen: set[tuple] = set()
    results: list[ResolveResult] = []
    for c in conflicts:
        key = (c.schedule_a, c.schedule_b, c.date, c.mask)
        if key in seen:
            continue
        seen.add(key)
        order = [c.schedule_a, c.schedule_b]
        if move_once_first:
            order.sort(key=lambda s: 0 if kinds.get(s) == "once" else 1)
        plans: list[Plan] = []
        for sid in order:
            plans.extend(
                _plans_moving(conn, sid, c, ctx, cfg, all_days, window_days)
            )
        results.append(ResolveResult(c, _dedup(plans)[:max_plans]))
    return results


def _days(d0: date, d1: date) -> list[date]:
    out, d = [], d0
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


def _dedup(plans: list[Plan]) -> list[Plan]:
    plans = sorted(plans, key=lambda p: p.cost)
    out: list[Plan] = []
    seen: set[str] = set()
    for p in plans:
        k = ";".join(m.describe() for m in p.moves)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _plans_moving(
    conn: sqlite3.Connection,
    sid: str,
    c,
    ctx: SolverContext,
    cfg: SlotConfig,
    all_days: list[date],
    window_days: int,
) -> list[Plan]:
    row = conn.execute("SELECT kind, title FROM schedules WHERE id = ?", (sid,)).fetchone()
    kind, title = row["kind"], row["title"]
    people, resources = related_objects(conn, sid)
    involved = list(dict.fromkeys(people + resources))
    if not involved:
        return []

    self_masks = schedule_masks(conn, sid, cfg, all_days)
    old_mask = self_masks.get(c.date, 0) & c.mask
    if not old_mask:
        return []
    need = popcount(old_mask)
    old_slots = decode_mask(old_mask)
    prefer_idx = old_slots[0][1]

    def free_on(d: date, exclude_self_mask: int) -> int:
        free = cfg.day_mask(d.isoweekday())
        for oid in involved:
            busy = ctx.occupancy.get((oid, d), 0) & ~exclude_self_mask
            free &= ~busy
        return free

    plans: list[Plan] = []

    if kind == "once":
        for delta in range(-window_days, window_days + 1):
            d = c.date + timedelta(days=delta)
            if d not in self_masks and d not in set(all_days):
                continue
            for cand in _candidate_masks(free_on(d, self_masks.get(d, 0)), need, prefer_idx):
                if hard_violations(ctx, involved, d, cand):
                    continue
                cost = soft_cost(ctx, involved, d, cand)
                shift = sum(abs(i - prefer_idx) for _w, i in decode_mask(cand)) / need
                plans.append(
                    Plan(
                        [Move(sid, title, c.date, old_slots, d, decode_mask(cand), False)],
                        round(cost + 0.6 * abs(delta) + 0.4 * shift, 3),
                        note=f"活动改期（{'提前' if delta < 0 else '推迟'}{abs(delta)} 天）"
                        if delta
                        else "当天换节次",
                    )
                )
            if len(plans) >= 12:
                break
        return plans

    # 周期日程：候选是"每周的哪一天哪几节"，必须在所有同周几的日期上都通过
    for delta_dow in range(-4, 5):
        target_dow = ((c.date.isoweekday() - 1 + delta_dow) % 7) + 1
        same_dow_days = [d for d in all_days if d.isoweekday() == target_dow]
        if not same_dow_days:
            continue
        # 用第一个该周几的日期生成候选掩码，再在所有同周几日期上验证
        probe = same_dow_days[0]
        probe_free = free_on(probe, self_masks.get(probe, 0))
        for cand in _candidate_masks(probe_free, need, prefer_idx):
            ok = True
            total = 0.0
            for d in same_dow_days:
                f = free_on(d, self_masks.get(d, 0))
                if cand & ~f:
                    ok = False
                    break
                total += soft_cost(ctx, involved, d, cand)
            if not ok:
                continue
            avg_cost = total / len(same_dow_days)
            shift = sum(abs(i - prefer_idx) for _w, i in decode_mask(cand)) / need
            plans.append(
                Plan(
                    [Move(sid, title, c.date, old_slots, probe, decode_mask(cand), True)],
                    round(avg_cost + 0.8 * abs(delta_dow) + 0.4 * shift, 3),
                    note="调课（每周生效，整个学期跟着变）",
                )
            )
        if len(plans) >= 10:
            break
    return plans


def resolve_and_apply(
    conn: sqlite3.Connection,
    org_id: str,
    date_from: date,
    date_to: date,
    *,
    cfg: SlotConfig | None = None,
    window_days: int = 5,
    max_rounds: int = 24,
) -> tuple[list[Plan], int]:
    """一键消解：反复"找冲突 → 应用当前最优方案 → 重新扫描"，直到没有可行方案。

    ⚠️ 为什么必须迭代、不能一次性把各冲突的最优方案都应用掉：
    每处冲突的方案都是基于**当时**的占用状态算出来的。同时应用多个方案，
    它们会互相打架（两处冲突都把东西挪到同一个空位），反而制造新冲突。
    所以每应用一个方案就重新扫描，让下一个方案基于最新状态——
    这是贪心，但对本场景足够，而且每一步都保证"冲突数不上升"。
    """
    cfg = cfg or load_slot_config(conn, org_id)
    all_days = _days(date_from, date_to)
    applied: list[Plan] = []
    prev = len(find_conflicts(build_occupancy(conn, org_id, date_from, date_to, cfg=cfg)))

    for _ in range(max_rounds):
        results = [
            r
            for r in resolve(
                conn, org_id, date_from, date_to, cfg=cfg, window_days=window_days, max_plans=1
            )
            if r.plans
        ]
        if not results:
            break
        best = min(results, key=lambda r: r.plans[0].cost)
        apply_plan(conn, best.plans[0], cfg, all_days)
        applied.append(best.plans[0])
        now = len(find_conflicts(build_occupancy(conn, org_id, date_from, date_to, cfg=cfg)))
        if now >= prev:  # 没有变好就停，避免来回震荡
            break
        prev = now
    remaining = len(find_conflicts(build_occupancy(conn, org_id, date_from, date_to, cfg=cfg)))
    return applied, remaining


def apply_plan(conn: sqlite3.Connection, plan: Plan, cfg: SlotConfig, all_days: list[date]) -> None:
    """把一个消解方案写回数据库。

    一次性活动：改 start_at / end_at（保持时长不变）。
    周期日程：改 slot_bitmap（去掉原节次、加上新节次）。
    """
    for mv in plan.moves:
        row = conn.execute(
            "SELECT kind FROM schedules WHERE id = ?", (mv.schedule_id,)
        ).fetchone()
        if row["kind"] == "once":
            # ⚠️ 不能只平移日期：新节次的起止时刻与旧节次不同，
            #    必须按新槽位的时刻重写，否则"改到第2节"实际落在第1节。
            idxs = [i for _w, i in mv.new_slots]
            dow = mv.new_date.isoweekday()
            first = cfg.get(dow, min(idxs))
            last = cfg.get(dow, max(idxs))
            if first is None or last is None:
                continue
            conn.execute(
                "UPDATE schedule_once SET start_at = ?, end_at = ? WHERE schedule_id = ?",
                (
                    f"{mv.new_date.isoformat()}T{first['start_time']}",
                    f"{mv.new_date.isoformat()}T{last['end_time']}",
                    mv.schedule_id,
                ),
            )
        else:
            r = conn.execute(
                "SELECT slot_bitmap FROM schedule_periodic WHERE schedule_id = ?",
                (mv.schedule_id,),
            ).fetchone()
            m = hex_to_mask(r["slot_bitmap"])
            for w, i in mv.old_slots:
                m &= ~bit(w, i)
            for w, i in mv.new_slots:
                m |= bit(w, i)
            conn.execute(
                "UPDATE schedule_periodic SET slot_bitmap = ? WHERE schedule_id = ?",
                (format(m, "x"), mv.schedule_id),
            )
    conn.commit()
