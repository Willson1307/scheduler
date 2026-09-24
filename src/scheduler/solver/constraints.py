"""硬约束与软约束：求解器的"规则层"。

设计意图：

**硬约束**是"违反就不成立"的规则，求解器必须保证输出 100% 满足，
这样现场才能理直气壮地说"这一版课表不存在任何硬冲突"。
**软约束**是"尽量满足"的偏好，用**代价**表达而不是用布尔值表达——
因为偏好是有强度的（"最好上午"和"绝对不能末节"不是一回事），
而且代价可以直接相加，天然支持多目标权衡。

软约束的权重默认给 1.0，但可以由 B6 批次从本校往期课表学出来
（w = −ln(1−p)，p 为历史满足率），学完直接替换这里的默认值。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..slots import SlotConfig, bit, decode_mask, popcount, hex_to_mask


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


@dataclass
class ObjectInfo:
    id: str
    kind: str
    name: str
    capacity: int | None = None


@dataclass
class SolverContext:
    """求解器需要的一切输入。构建一次，多个模式复用。"""

    cfg: SlotConfig
    objects: dict[str, ObjectInfo]
    occupancy: dict[tuple[str, "object"], int] = field(default_factory=dict)
    # {(对象, 日期): 掩码}
    daily_limit: int = 6          # 同一教师/班级单日课时上限（软约束）
    avoid_last_period: bool = True  # 主科不排末节（软约束）
    prefer_continuous: bool = True  # 偏好连堂（软约束）
    weights: dict[str, float] = field(default_factory=dict)

    def w(self, key: str) -> float:
        return self.weights.get(key, 1.0)


def load_objects(conn: sqlite3.Connection, org_id: str) -> dict[str, ObjectInfo]:
    out: dict[str, ObjectInfo] = {}
    for r in conn.execute(
        "SELECT id, kind, name, capacity FROM objects WHERE org_id = ?", (org_id,)
    ).fetchall():
        out[r["id"]] = ObjectInfo(r["id"], r["kind"], r["name"], r["capacity"])
    return out


# ---------------------------------------------------------------------------
# 硬约束
# ---------------------------------------------------------------------------


def hard_violations(
    ctx: SolverContext,
    object_ids: list[str],
    day: "object",
    mask: int,
    *,
    exclude: set[str] | None = None,
    group_size: int | None = None,
) -> list[str]:
    """检查"把这些对象安排在这一天的这些节次"是否违反硬约束。返回违规说明列表。

    exclude: 需要忽略的日程 id（例如正在被移动的那条本身）
    """
    problems: list[str] = []
    exclude = exclude or set()

    for oid in object_ids:
        busy = ctx.occupancy.get((oid, day), 0)
        clash = busy & mask
        if clash:
            info = ctx.objects.get(oid)
            name = info.name if info else oid
            slots = "、".join(f"第{i}节" for _d, i in decode_mask(clash))
            problems.append(f"{name} 在该时段已被占用（{slots}）")

    # 容量：教室装不下这个班
    if group_size is not None:
        for oid in object_ids:
            info = ctx.objects.get(oid)
            if info and info.kind == "resource" and info.capacity is not None:
                if group_size > info.capacity:
                    problems.append(
                        f"{info.name} 容量 {info.capacity}，容纳不下 {group_size} 人"
                    )
    return problems


def resource_conflicts(
    ctx: SolverContext,
    resource_ids: list[str],
    day: "object",
    mask: int,
    *,
    exclude: set[str] | None = None,
) -> list[str]:
    """资源（教室/场地）层面的撞车。与人员分开列，便于给出更精确的建议。"""
    exclude = exclude or set()
    out: list[str] = []
    for rid in resource_ids:
        clash = ctx.occupancy.get((rid, day), 0) & mask
        if clash:
            info = ctx.objects.get(rid)
            out.append(f"{(info.name if info else rid)} 该时段已被占用")
    return out


# ---------------------------------------------------------------------------
# 软约束（代价，越小越好）
# ---------------------------------------------------------------------------


def soft_cost(
    ctx: SolverContext,
    object_ids: list[str],
    day: "object",
    mask: int,
    *,
    exclude: set[str] | None = None,
) -> float:
    """计算把某些对象安排在这一天的这些节次时的软约束代价。"""
    exclude = exclude or set()
    slots = decode_mask(mask)
    if not slots:
        return 0.0
    dow, _ = slots[0]
    cost = 0.0
    n = popcount(mask)

    # 1) 单日课时上限
    if ctx.daily_limit:
        for oid in object_ids:
            before = popcount(ctx.occupancy.get((oid, day), 0))
            if before + n > ctx.daily_limit:
                cost += ctx.w("daily_limit") * (before + n - ctx.daily_limit)

    # 2) 不排末节（末节 = 当天启用的最大节次）
    if ctx.avoid_last_period:
        last_idx = max(
            (i for d, i in ctx.cfg.enabled_slots(dow)), default=0
        )
        if any(i >= last_idx for _d, i in slots):
            cost += ctx.w("avoid_last_period")

    # 3) 连堂偏好：与相邻节次连成一片反而更好（负代价）
    if ctx.prefer_continuous and n >= 1 and object_ids:
        merged = mask | ctx.occupancy.get((object_ids[0], day), 0)
        for _d, i in slots:
            neighbour = bit(dow, i + 1) if i < 16 else 0
            if merged & neighbour and not (mask & neighbour):
                cost -= ctx.w("prefer_continuous") * 0.5
                break

    # 4) 上午偏好：越靠后越轻微加代价
    avg_idx = sum(i for _d, i in slots) / n
    cost += ctx.w("morning") * max(0.0, (avg_idx - 4)) * 0.3

    return cost


def is_hard_ok(ctx: SolverContext, object_ids: list[str], day, mask, **kw) -> bool:
    return not hard_violations(ctx, object_ids, day, mask, **kw)


def total_cost(ctx: SolverContext, object_ids: list[str], day, mask, **kw) -> float:
    """硬约束违规折算成巨大代价，保证搜索永远不会选中它们。"""
    v = hard_violations(ctx, object_ids, day, mask, **kw)
    return 1e6 * len(v) + soft_cost(ctx, object_ids, day, mask, **kw)


__all__ = [
    "ObjectInfo",
    "SolverContext",
    "load_objects",
    "hard_violations",
    "resource_conflicts",
    "soft_cost",
    "is_hard_ok",
    "total_cost",
    "hex_to_mask",
]
