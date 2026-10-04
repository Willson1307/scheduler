"""B3 共用内核：objects / schedules / conflicts 三接口的**唯一口径源**。

🔴🔴 本文件存在的唯一理由：防止同一口径在多个接口各算一遍而漂移。

  2026-10-04 血泪教训（B4/api_resolve.py 的 `_slots_hex`）：
  「把(dow, period) 列表转成十六进制」这件事在同一个文件里写了两遍，
  两遍一旦不一致，接口之间的数据就对不上，且**不报错**——静默失效。
  B3 有三个接口都要用「窗口 / 组解析 / 单日掩码 / 聚合」这四件事，
  所以全部收拢到本文件，三个api_*.py 一律调用这里的函数，
  **禁止自己再实现一遍**。

---------------------------------------------------------------------------
🔴🔴 掩码有两套位序，混用会导致页面全部错位且不报错（已付出过代价的坑）
---------------------------------------------------------------------------

| 口径 | 函数 | 位序 | 示例 |
|---|---|---|---|
| **周掩码**（存储用） | `slots.bit(dow, idx)` | 0-based，位号 = (周几-1)*16 + (节次-1) | `0x100000000` = 周三第 1 节 |
| **单日掩码**（API 出参） | `m2.day_slots_mask(slots)` | 1-based，period N → 位 N | `0x2`=第1节 `0x8`=第3节 `0x6`=第1+2节 |

**API 响应一律用 `slots_hex()`**（即单日掩码）。
黄金用例：周掩码 `0x100000000`（周三第 1 节）→ 单日掩码 `0x000002`。
🔴 注意 `0x100000000` 与 `0x000002` 绝不相等——测试里必须反向断言这一点，
否则「用错位序」这个 bug 会安静地通过测试。

---------------------------------------------------------------------------
🔴 聚合口径（契约 docs/07 §5「必须服务端聚合」+ tests/test_contract.py:321-334）
---------------------------------------------------------------------------

`find_conflicts()` 返回**原始条数**（演示库 23条：1 条教师冲突 + 两个班各 11 条学生冲突）。
按「日程对 × 日期 × 节次」聚合后是 **3 组**。

- `count`     = len(items) = 3   ← 界面说「3 组冲突待处理」
- `raw_count` = 23              ← 界面说「共消解 23 处冲突」（= scan().count，与 B4 的
  `remaining_before` 同源，口径一致，答辩时不会被问住）

聚合键用 (schedule_a, schedule_b, date, mask)，并**全序排序**后再输出——
`object_id` 是 uuid4，若代表对象选取不定序，「高一(1)班」刷新一次会变成
「学生1-07」，破坏「两次调用逐字一致」这条硬要求。

只用Python 标准库。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import config
from .http_api import ApiError

from scheduler.occupancy import scan
from scheduler.slots import decode_mask, load_slot_config
from scheduler.solver import m2

# ---------------------------------------------------------------------------
# 契约常量（docs/07 v1.0）
# ---------------------------------------------------------------------------

#: objects 接口的 kind 白名单——恰好三值（契约冻结）
OBJECT_KINDS = ("group", "user", "resource")

#: 周视图的槽位数量（10 节）。周视图网格 = 5 天 × 10 节。
SLOTS_PER_DAY = 10

#: 周视图的天数（周一~周五）。周六周日学校不上课，不进网格。
DAYS_IN_WEEK = 5

#: 单日掩码转十六进制时的最小补零位数。
#: 0x2 → "0x000002"（与 B4 的 _slots_hex 行为一致，前端已在用这个宽度）
HEX_WIDTH = 6


# ---------------------------------------------------------------------------
# 1. 窗口口径
# ---------------------------------------------------------------------------


def resolve_window(cfg) -> tuple[date, date]:
    """求解/查询窗口 = **cfg.today 所在周的周一~周日**（契约 docs/07 §4：本周）。

    🔴🔴 现场演示铁律：启动必须带 `--today 2026-09-08`。
    演示库的学期起点固定 2026-09-07，**23 处冲突全在第 1 周**，
    第 2/3/4 周各只有 1 处。不带 --today 时（当天 10-03 落在第 5 周）
    只扫到 **1 处**，而接口**不报错**（termination 仍是 ok、remaining_after 仍是 0），
    页面会显示「已消解 1 处冲突」——比直接报错更难解释。
    详见 docs/17 §1（该坑已由队长两组对照实测确认）。
    """
    start = config.monday_of(cfg.today)
    return start, start + timedelta(days=6)


def window_payload(cfg) -> dict:
    """给 API 响应用的window 字段（前端要能显示「扫的是哪一周」）。"""
    date_from, date_to = resolve_window(cfg)
    return {
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "today": cfg.today.isoformat(),
    }


# ---------------------------------------------------------------------------
# 2. 组解析口径
# ---------------------------------------------------------------------------


def parse_group(query: dict) -> str | None:
    """从 query 取 group 参数；缺省/空串 → None（表示全体）。"""
    group = (query.get("group") or [""])[0]
    return group or None


def resolve_org(conn: sqlite3.Connection, group: str | None) -> str:
    """group → org_id。group 为 None 时取第一个组织。

    与 B4 `_scope()` 同语义。⚠️ B2 补鉴权后这里要加 org 归属校验
    （B2 卡 rhJz9F 已抓到「可跨组织消解他组织课表」的 P0，本函数是同一个出口）。
    """
    if group:
        row = conn.execute("SELECT org_id FROM objects WHERE id = ?", (group,)).fetchone()
        if row is None:
            raise ApiError(404, "组不存在")
        return row["org_id"]
    row = conn.execute("SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise ApiError(404, "无组织数据")
    return row["id"]


def require_group(query: dict) -> str:
    """B3 三接口共用的「必须给group」校验。

    契约docs/07 §1：三个接口的 group **缺省= 所在最高组**。
    MVP 阶段单组织演示库，缺省取第一个组即可，但**必须显式校验缺失**，
    不能静默落到「全体」——那会让用户看到别的班级的课表。
    """
    group = parse_group(query)
    if not group:
        raise ApiError(400, "缺少参数 group（该接口不接受缺省，契约 docs/07 §1）")
    return group


# ---------------------------------------------------------------------------
# 3. 掩码口径（🔴 本项目最贵的坑，函数 docstring 已写死，勿删）
# ---------------------------------------------------------------------------


def slots_hex(slots) -> str:
    """(dow, period) 列表 → API 的**单日掩码**十六进制字符串。

    🔴 必须用 `m2.day_slots_mask`（1-based，period N → 位 N），
    **不是** `slots.bit`（0-based 周掩码）。两套位序混用会让页面全部错位
    且不报错。黄金用例：周掩码 0x100000000（周三第 1 节）→ 此函数返回 "0x000002"。

    与 B4 的 `_slots_hex()` 行为完全一致（同样的"0x" + format(x, "x")）。
    """
    return "0x" + format(m2.day_slots_mask(slots), "x")


def readable(slots) -> list[dict]:
    """(dow, period) 列表 → 前端可直接显示的 [{dow, period}]。

    🔴 服务端给什么前端显示什么（契约 §12 灵活性条款），
    **前端不做位运算、不内置节次映射表**。
    """
    return [{"dow": int(d), "period": int(p)} for d, p in slots]


def day_mask_hex(mask: int) -> str:
    """单日掩码 int → 十六进制字符串（width 对齐，0x2 → "0x000002"）。"""
    return "0x" + format(mask, "0{}x".format(HEX_WIDTH))


# ---------------------------------------------------------------------------
# 4. 聚合口径（🔴 契约要求服务端聚合，count=组数 / raw_count=原始条数）
# ---------------------------------------------------------------------------


def aggregate_conflicts(conflicts) -> tuple[list[dict], int]:
    """原始冲突列表 → (items, raw_count)。

    聚合键 = (schedule_a, schedule_b, date, mask) —— 即「同一个日程对、
    同一天、同一批节次」算**一组**。
    🔴 为什么必须聚合：同一个班级的一节课与 10 个学生的各自日程撞车，
    领域层会吐出 11 条，但用户看到的是**一个**冲突点。不聚合会刷屏，
    且与契约 §5「必须服务端聚合」和 test_contract.py:321-334 的
    「(日程对, date, slots) 三元组不得重复」断言冲突。

    返回的 items 已按 (date, mask, schedule_a, schedule_b) **全序排序**，
    保证同一请求两次调用逐字一致（object_id 是 uuid4，
    代表对象若不定序，「高一(1)班」刷新会变成「学生1-07」）。

    `raw_count` = len(conflicts)（原始条数，与 B4 的 remaining_before 同源口径）。
    """
    buckets: dict[tuple, list] = {}
    for c in conflicts:
        key = (c.schedule_a, c.schedule_b, c.date, c.mask)
        buckets.setdefault(key, []).append(c)

    items: list[dict] = []
    for key in sorted(buckets, key=lambda k: (k[2], k[3], k[0], k[1])):
        group = buckets[key]
        # 代表对象全序选取：role → object_id，取字典序第一个。
        # 🔴 不能写 min(level) 之类；role 的固定优先级见 ROLE_ORDER。
        rep = sorted(group, key=lambda c: (ROLE_ORDER.get(c.role, 99), c.object_id))[0]
        schedule_a, schedule_b, cdate, mask = key
        slots = decode_mask(mask)
        items.append({
            "a": {"schedule_id": schedule_a, "role": rep.role},
            "b": {"schedule_id": schedule_b},
            "object_id": rep.object_id,
            "date": cdate.isoformat(),
            "slots": slots_hex(slots),
            "raw_count": len(group),
            "readable": readable(slots),
        })
    return items, len(conflicts)


#: 代表对象选取的 role 优先级：教师冲突比学生冲突更值得优先展示
ROLE_ORDER = {"teacher": 0, "target": 1, "participant": 2, "resource": 3}


def conflict_payload(conn: sqlite3.Connection, org_id: str, cfg) -> dict:
    """/api/conflicts 的完整响应体（聚合口径的唯一入口）。

    count     = 聚合后组数（演示库 3）
    raw_count = 原始条数（演示库 23）
    """
    date_from, date_to = resolve_window(cfg)
    slot_cfg = load_slot_config(conn, org_id)
    report = scan(conn, org_id, date_from, date_to, cfg=slot_cfg)
    items, raw_count = aggregate_conflicts(report.conflicts)
    return {
        "count": len(items),
        "raw_count": raw_count,
        "window": window_payload(cfg),
        "items": items,
    }
