"""B3 周视图网格接口：GET /api/schedules?group=&week=（docs/07 §4）。

响应 200：{"objects":[...], "days":[...], "grid": {"<object_id>|<date>": "<mask_hex>"}}

用途与设计意图（答辩口径）：
  1. **周视图 = 5 天 × 每格一个掩码**。前端拿到 grid 就能直接渲染课表网格，
     不需要二次请求，也不需要自己解掩码（契约 §4：前端只渲染，不做位运算）。
     `days` 独立给出而不是让前端自己推算，是为了让「这一周到底是哪几天」
     成为一个**服务端事实**——跨月、跨学期时前端不会算错。
  2. **网格是稀疏的**：只给「某对象某天确实被占用」的格子。
     空格 = 无安排，前端不必为每个 (对象, 天) 组合都发一份 0x0；
     1280 条日程展开后只落地真正有占用的格子，响应体与实际排课密度成正比。
  3. **组解析 / 掩码位序 / 窗口口径全部复用 `b3_kernels`**，
     本文件**不重复实现任何一个**（见下方「🔴 掩码：本项目最贵的坑」）。

---------------------------------------------------------------------------
🔴🔴 掩码：本项目最贵的坑，本文件的核心正确性全在这里
---------------------------------------------------------------------------
系统里有两套位序，混用会让页面**全部错位且不报错**：

| 口径 | 位序 | 周三第 1 节 |
|---|---|---|
| 周掩码（存储 / 领域层内部） | 0-based，位号 =(周几-1)*16+(节次-1) | `0x100000000` |
| **单日掩码（API 出参必须用这个）** | 1-based，period N → 位 N | `0x000002` |

🔴 **`build_occupancy()` 返回的 `OccupancyEntry.mask` 是「周掩码里属于当天的
那一段车道」，不是 API 单日掩码**——它虽然按 (对象, 日期) 分了桶，但桶里的值
仍是周位序。证据（`scheduler/occupancy.py:131`，`expand_periodic()`）：

        day_mask = slot_bitmap & cfg.day_mask(dow)

`cfg.day_mask(dow)` 用的还是 `slots.bit(dow, idx)`（`slots.py:104-108`），
即周位序 `(dow-1)*16 + (idx-1)`。实测演示库 2026-09-07 那周 **175/175 条**
entry 直接 `format(mask,'x')` 都与正确值不符：

        entry.mask = 0x1 → decode_mask → [(1, 1)] → API 单日掩码 0x000002
        直接 format 会给出 0x000001   ← 整体错一位，肉眼可见地全错

所以本文件**不能**把 `entry.mask` 直接丢给 `K.day_mask_hex()`，必须先解码成
(dow, period) 列表、再经 `m2.day_slots_mask()` 转成 1-based 单日掩码：

        K.day_mask_hex(m2.day_slots_mask(decode_mask(mask)))

黄金用例（务必在测试里反向断言，否则这个 bug 会安静地通过）：
        decode_mask(0x100000000) == [(3, 1)]  →  单日掩码 "0x000002"
        0x100000000 != 0x000002

---------------------------------------------------------------------------
契约未写清之处（本文件的假设，与队长确认后可能调整）
---------------------------------------------------------------------------
  A. **`group` 的作用范围**：契约 §4 只写「group（可选）」，**没写 objects
     是否要限制到该组闭包**（§3 /api/objects 才有「只返回当前组闭包内的对象」）。
     本实现取**只用于解析 org_id**（`K.resolve_org`），objects 仍是
     「该组织这一周有占用的全部对象」——与 B3 另外两个接口同口径。
     理由：周视图要能同时看见班级、任课教师、教室各自的占用，
     只取组闭包会把教师和教室全部滤掉（教师不是任何班级的成员），
     那样就看不出「谁和谁撞课」了，本接口失去意义。
     若队长要求按组过滤，改 `_load_objects()` 一处即可。
  B. **`week` 传周内任一日期**：契约 §4 已明写「传该周内任一日期即可，
     服务端自行归到周一」，故对周六/周日**不做拒绝**（归到同一周的周一）。
  C. **周视图只取周一~周五 5 天**（`K.DAYS_IN_WEEK`）：学校周末不上课。
     🔴 这带来一条实现约束——grid 的 key 必须**只落在 days 之内**。
     `config.ensure_weekend_slots()` 会给周六周日补上 enabled=1 的槽位行，
     所以「周末也可能有占用」是真实存在的；若直接把领域层返回的日期全塞进
     grid，就会出现一个**前端没有对应列的格子**。因此本文件在**查询阶段**
     就把占用范围收窄为 [周一, 周五]（`_WEEK_SPAN_DAYS - 1` 天），
     让「grid 的日期 ⊆ days」成为结构性保证，而不是靠事后过滤。
  D. **grid 只给非零格**：领域层不会产出零掩码，本文件再判一次 `if not mask:
     continue`，纯粹是防御——0x0000 画出来和空格一样，白占响应体。

签名约定（写死，不得改）：handle(method, path, query, body, headers, context=None)。
只用 Python 标准库。

---------------------------------------------------------------------------
追加（卡 rYoMa7）：同文件另挂一个**只读**接口
`GET /api/schedules/detail?group=&object_id=&date=`（docs/07 §4.1）
---------------------------------------------------------------------------
`/api/schedules` 的 grid 值是**按位或后**的掩码，前端双击一格时不知道要
取消哪一条日程（一格多条日程正是冲突本身），也拿不到「这天实际生效的节次」。
detail 把这两件事一次给出：`cells[]`（按节次拆开、每格带 `schedules[]` 数组）
+ `periods[]`（当天生效作息）。

🔴 三条不钉就会静默失效的点：
  1. `cells[].schedules` 必须是**数组**——给单对象等于替前端静默挑一条；
  2. 每条 schedule 必须带 `kind`——`POST /api/exceptions` 对
     `kind != 'periodic'` 直接 400，前端要靠它决定双击能不能点；
  3. `object_id` 不属于该组织必须 **404**——返回空 `cells` 等于
     告诉前端「这天没课」（假阴性，本项目最痛形态）。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from . import auth
from . import b3_kernels as K
from . import config
from .http_api import ApiError

from scheduler.occupancy import (
    build_occupancy,
    effective_slot_config,
    load_exceptions,
    occupancy_map,
)
from scheduler.slots import decode_mask, load_slot_config
from scheduler.solver import m2

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

#: `week` 的严格格式。🔴 不用 date.fromisoformat 的宽松解析：
#: Python 3.11+ 的 fromisoformat 接受 '20260907'（无连字符），
#: 而契约 §通用约定写的是 ISO 日期 '2026-09-23'。宽松解析会让
#: '20260907' 悄悄通过，前端拼错参数却查不出问题。
_WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 周视图的天数（周一~周五）。引用内核常量，口径只有一处。
DAYS_IN_WEEK = K.DAYS_IN_WEEK


def _parse_week(query: dict, cfg) -> date:
    """week 参数 → 该周的**周一**；缺省/空串 → 本周周一。

    契约 §4：week 可缺省（= 本周），且「传该周内任一日期即可，服务端自行
    归到周一」——所以这里只做格式校验 + 取周一，不校验「必须是周一」。
    缺省走 `K.resolve_window()`，与 /api/conflicts、/api/resolve 同一条口径，
    免得三个接口对「本周是哪一周」各算一遍（那正是 B3 内核存在的理由）。
    """
    raw = (query.get("week") or [""])[0].strip()
    if not raw:
        return K.resolve_window(cfg)[0]

    if _WEEK_RE.match(raw) is None:
        raise ApiError(400, f"week 需要 YYYY-MM-DD 格式，收到 {raw!r}")
    try:
        d = date.fromisoformat(raw)
    except ValueError as exc:  # 正则过了但日期本身不存在（如 2026-02-30）
        raise ApiError(400, f"week 不是合法日期：{raw!r}") from exc
    return config.monday_of(d)


def _week_days(monday: date) -> list[date]:
    """周一 → 周一~周五的 5 个日期（周六周日学校不上课，不进网格）。"""
    return [monday + timedelta(days=i) for i in range(DAYS_IN_WEEK)]


# ---------------------------------------------------------------------------
# objects：这一周有占用的对象
# ---------------------------------------------------------------------------

#: 只取 id / kind / name 三个字段（契约 §4「结构与 /api/objects 保持一致」）。
#: 🔴 ORDER BY 覆盖全部输出列且末列是唯一键 id ⇒ 全序 ⇒ 两次调用逐字一致。
#:    objects.id 是 uuid4（随机），没有 ORDER BY 时行序完全取决于 SQLite 的
#:    存储顺序，「刷新一次对象列表顺序就变了」肉眼可见。
_SQL_OBJECTS = """
SELECT id, kind, name
FROM objects
WHERE org_id = ? AND id IN ({placeholders})
ORDER BY kind, name, id
"""


def _load_objects(conn: sqlite3.Connection, org_id: str, object_ids: set[str]) -> list[dict]:
    """把占用涉及的对象 id 集合查成 objects 数组（按 kind, name, id 全序）。"""
    if not object_ids:
        return []
    ids = sorted(object_ids)
    sql = _SQL_OBJECTS.format(placeholders=",".join("?" * len(ids)))
    return [
        {"id": r["id"], "kind": r["kind"], "name": r["name"]}
        for r in conn.execute(sql, [org_id, *ids]).fetchall()
    ]


# ---------------------------------------------------------------------------
# grid：<object_id>|<date> → 单日掩码十六进制
# ---------------------------------------------------------------------------

#: grid key 的分隔符。🔴 契约 §4 明文规定竖线，**不得改**——
#: 前端按 `key.split('|')` 拆分（tests/test_contract.py:250 断言竖线恰好 1 个）。
#: 为什么用竖线：object_id 与 ISO 日期都不含竖线，拆分无歧义；
#: 用逗号/下划线做分隔符则一旦对象名或 id 未来允许该字符就会歧义。
_KEY_SEP = "|"


def _grid(conn: sqlite3.Connection, org_id: str, monday: date) -> dict[str, str]:
    """展开占用 → 按 (对象, 日期) 合并 → 输出 grid。

    🔴 口径分工（本文件最关键的三行）：
      1. `build_occupancy(..., expand_groups=True)` —— 领域层把「组」展开成
         其下全部成员。**必须传 True（默认值）**：班级课表会同时占用每个
         学生的时间，只做组级检测会漏掉 20 处学生冲突。实测演示库
         同一窗口 expand_groups=False 只有 3 处冲突、True 有 23 处，
         与 scan() 的基准口径一致。
      2. `occupancy_map()` —— 把同 (对象, 日期) 的多条 entry 按位或合并成
         一天的最终掩码（同一班两节课占同一天，要并成一个格子而不是两个）。
      3. `decode_mask` + `m2.day_slots_mask` + `K.day_mask_hex` —— 周位序
         转 1-based 单日掩码再转 hex。**顺序不能颠倒，也不能把领域层的 mask
         直接喂给这两个函数**：`K.day_mask_hex()` 收的是**已经转成 1-based 的
         单日掩码 int**（喂周位序进去会原样输出错值），`K.slots_hex()` 收的是
         **(dow, period) 列表**而非 mask。`format(mask,'x')` 同样错（位序）。
    """
    days = _week_days(monday)
    # 日期范围只取周一~周五：让 grid 的日期 ⊆ days 成为结构性保证（见假设 C）。
    date_from, date_to = days[0], days[-1]

    slot_cfg = load_slot_config(conn, org_id)
    entries = build_occupancy(conn, org_id, date_from, date_to, cfg=slot_cfg)
    merged = occupancy_map(entries)

    # 排序保证逐字一致：先按日期、再按 object_id（uuid4，必须显式定序）。
    out: dict[str, str] = {}
    for object_id, d in sorted(merged, key=lambda k: (k[1], k[0])):
        mask = merged[(object_id, d)]
        if not mask:
            continue  # 防御：0 掩码画出来就是空格，不占响应体（假设 D）
        out[_KEY_SEP.join((object_id, d.isoformat()))] = K.day_mask_hex(
            m2.day_slots_mask(decode_mask(mask))
        )
    return out


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


@auth.require_auth
def list_schedules(method, path, query, body, headers, context=None):
    """GET /api/schedules?group=&week= → 200（契约 docs/07 §4）。

    成功 200： {"objects":[{"id","kind","name"}], "days":["2026-09-07",...],
                "grid":{"<object_id>|<date>":"0x000006",...}}
    未登录 → 401（@require_auth）；越权 group → 403（K.resolve_authorized_org）
    week 格式非法 → 400；group 不存在 → 404。

    `group` 用 `K.parse_group`（可缺省）而**不是** `K.require_group`。
    🔴 真实理由是**契约测试裸调**：`tests/test_contract.py:233` 的
    `_schedules()` 五个调用点全都不带 group，而 `test_response_structure`
    断言 200 + 非空——用 `require_group`（缺失即 400）会直接把它打挂。
    契约 §4 表格本身也标的是「group（可选）」。
    缺省时经 `K.resolve_authorized_org` 用 `authz.default_group_id` 定位
    用户自己的组（不再取第一个组织）。
    """
    cfg = config.get_current_config()
    user_id = context["user_id"]
    group = K.parse_group(query)
    monday = _parse_week(query, cfg)

    conn = config.open_db()
    try:
        # 越权校验 + group → org_id：不存在即 404，越权 403，
        # 而不是返回空 grid（空 grid 会被前端当成「这周没课」，比报错更难解释）。
        _group, org_id = K.resolve_authorized_org(conn, user_id, group)

        grid = _grid(conn, org_id, monday)
        object_ids = {key.split(_KEY_SEP)[0] for key in grid}
        return {
            "objects": _load_objects(conn, org_id, object_ids),
            "days": [d.isoformat() for d in _week_days(monday)],
            "grid": grid,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# detail：单格日程归属 + 当天生效作息（卡 rYoMa7 / docs/07 §4.1）
# ---------------------------------------------------------------------------

#: detail 的必填查询参数。缺一个就是 400，绝不用「空结果」兜底。
_DETAIL_REQUIRED = ("object_id", "date")


def _param(query: dict, name: str) -> str | None:
    """单值参数；缺省/空串/全空白 → None（parse_qs 给的是 list）。"""
    vals = query.get(name)
    if not vals:
        return None
    return (vals[0] or "").strip() or None


def _required(query: dict, name: str) -> str:
    v = _param(query, name)
    if v is None:
        raise ApiError(400, f"缺少参数 {name}")
    return v


def _parse_day(query: dict) -> date:
    """`date` → date；缺省/格式错/日期不存在 → 400。

    🔴 与 `_parse_week` 同样用严格正则，不用 `date.fromisoformat` 的宽松解析：
    Python 3.11+ 会接受 `20260909`（无连字符），契约写的是 ISO `YYYY-MM-DD`，
    宽松解析会让前端拼错参数却查不出问题。
    """
    raw = _required(query, "date")
    if _WEEK_RE.match(raw) is None:
        raise ApiError(400, f"date 需要 YYYY-MM-DD 格式，收到 {raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:  # 正则过了但日期不存在（如 2026-02-30）
        raise ApiError(400, f"date 不是合法日期：{raw!r}") from exc


def _require_object_in_org(conn: sqlite3.Connection, org_id: str, object_id: str) -> None:
    """object_id 必须属于 org_id，否则 **404**。

    🔴 为什么不返回 200 + 空 cells：空 cells 的语义是「这天这个对象没课」，
    前端双击空格会当成「可以排」。把「对象不存在/不属于本组织」也映射成
    空 cells，就是把一次越权/拼错参数伪装成「没课」——静默失效。
    对象不存在与不属于本组织都归 404：对调用方而言是同一件事
    （「你问的这个 id 在我这儿查不到」），且 404 不会泄漏别组织是否有该 id。
    """
    row = conn.execute("SELECT org_id FROM objects WHERE id = ?", (object_id,)).fetchone()
    if row is None or row["org_id"] != org_id:
        raise ApiError(404, f"对象不属于该组织：{object_id}")


def _periods(day_cfg, dow: int) -> list[dict]:
    """当天**生效**的节次清单（已过 `effective_slot_config`）。

    🔴 只给 `enabled = 1` 的节次：`SlotConfig.enabled_slots()` 与
    `day_mask()` 是同一份过滤（`expand_periodic` 用的就是 `day_mask`），
    所以 `periods` 的 idx 集合恒等于「占用掩码可能命中的位」。
    若把停用节次也列出来，前端「改到第 N 节」会让用户选到一条排不进去的
    节次——正是本项目最痛的静默失效。`enabled` 字段仍保留（契约字段名写死），
    当前恒为 true，将来若开放「停用但仍要展示」可直接改这一处。

    `new_mask`（2026-11-13 新增，纯追加字段）：这一节作为 move 目标时应传的
    单日掩码，格式与 `cells[].mask` 完全一致（`"0x%06x"`，第 1 节 = `0x000002`）。
    存在的唯一理由：`POST /api/exceptions` 的 `new_mask` 必须是「第 N 节 → 位 N」
    的单日掩码，而空白节次在 `cells` 里没有对应值——没有这个字段，前端只能自己
    `1 << idx` 算，违反「前端不做位运算，mask 由服务端给」。
    """
    out: list[dict] = []
    for _dow, idx in day_cfg.enabled_slots(dow):
        slot = day_cfg.get(dow, idx) or {}
        out.append(
            {
                "idx": idx,
                # 换作息方案（period_sets/set_slots）时没有 name 列，回退成「第N节」
                "name": slot.get("name") or f"第{idx}节",
                "start": slot.get("start_time"),
                "end": slot.get("end_time"),
                "enabled": True,
                # 单日掩码（1-based，第 N 节 → 位 N），与 cells[].mask 同格式。
                # ⛔ 不是周位序（(dow-1)*16+(idx-1)）——混用会让课整节挪到别的日子。
                "new_mask": "0x%06x" % (1 << idx),
            }
        )
    return out


def _cells(
    conn: sqlite3.Connection,
    entries,
    object_id: str,
    d: date,
    dow: int,
) -> list[dict]:
    """占用条目 → 按节次拆开的 cells（每格带完整的 schedules 数组）。

    🔴 聚合口径：
      1. 只认真实占用（`build_occupancy` 的 entry），**不另写一套展开**——
         否则「detail 说这格有课、grid 说没有」两套事实互相打架。
      2. `decode_mask` 出来的 `(dow, idx)` 的 dow 必须等于当天生效 dow；
         不等说明掩码里混进了别的车道（数据异常），跳过而不是算到别的节次上。
      3. 同一节次可能被**多条日程**占用 —— 这正是冲突本身。
         所以 `schedules` 是**数组**，绝不给单对象。
      4. 排序：cells 按 idx 升序；schedules 按 (id, role) 全序 ⇒ 两次调用逐字一致
         （schedule id 是 uuid，不定序会导致同格两条日程顺序随机）。
    """
    by_idx: dict[int, list[tuple[str, str]]] = {}
    for e in entries:
        if e.object_id != object_id or e.date != d:
            continue
        for bit_dow, idx in decode_mask(e.mask):
            if bit_dow != dow:
                continue  # 防御：掩码不该含别的 dow 的位
            by_idx.setdefault(idx, []).append((e.schedule_id, e.role))

    if not by_idx:
        return []  # 空白日：cells 为空数组（前端据此判断「这天没课」）

    ids = sorted({sid for pairs in by_idx.values() for sid, _role in pairs})
    sql = "SELECT id, title, kind FROM schedules WHERE id IN ({})".format(
        ",".join("?" * len(ids))
    )
    meta = {
        r["id"]: {"title": r["title"], "kind": r["kind"]}
        for r in conn.execute(sql, ids).fetchall()
    }

    cells: list[dict] = []
    for idx in sorted(by_idx):
        # 去重但保序：同一日程若对该对象挂了两个 role，两条都如实给出，
        # 完全重复的 (id, role) 只留一条（否则前端会出现重复列）。
        pairs = list(dict.fromkeys(by_idx[idx]))
        schedules = [
            {
                "id": sid,
                # 主表行缺失时给空串而不是丢条目：丢一条 = 少报一条冲突
                "title": meta.get(sid, {}).get("title", ""),
                "kind": meta.get(sid, {}).get("kind", ""),
                "role": role,
            }
            for sid, role in sorted(pairs)
        ]
        cells.append(
            {
                "idx": idx,
                # 单日掩码（1-based，period N → 位 N），与 /api/schedules 的 grid
                # 同一口径：0x000002 = 第 1 节。⛔ 不是 entry.mask 的周位序。
                "mask": K.day_mask_hex(m2.day_slots_mask([(dow, idx)])),
                "schedules": schedules,
            }
        )
    return cells


def _detail(conn: sqlite3.Connection, org_id: str, object_id: str, d: date) -> dict:
    """某对象某天的明细（只读）。

    🔴 两条「同源」是硬要求，任何一条走偏都会产出与主路径不一致的事实：
      1. 占用 = `build_occupancy(conn, org_id, d, d)`（与 /api/conflicts、
         /api/schedules 同一个函数），本文件不重写展开逻辑。
      2. 节次 = `load_exceptions` → `effective_slot_config`（与
         `expand_periodic` 同一条真相链），⛔ 不直接查 `period_slots`/`set_slots`。

    `dow` 取**生效**周几（调休日 = `as_of_dow`），与 `expand_periodic:319-321`
    完全同一口径。若这里改用 `d.isoweekday()`，调休日（周六按周一上课）就会
    拿周六的槽位去配周一的占用 —— periods 与 cells 对不上，页面错位且不报错。
    """
    iso = d.isoformat()
    slot_cfg = load_slot_config(conn, org_id)
    exc = load_exceptions(conn, org_id)

    dow = exc.as_of.get(iso) or d.isoweekday()
    day_cfg = effective_slot_config(exc, slot_cfg, iso)

    entries = build_occupancy(conn, org_id, d, d, cfg=slot_cfg)
    return {
        "object_id": object_id,
        "date": iso,
        "day_of_week": dow,
        "periods": _periods(day_cfg, dow),
        "cells": _cells(conn, entries, object_id, d, dow),
    }


@auth.require_auth
def schedule_detail(method, path, query, body, headers, context=None):
    """GET /api/schedules/detail?group=&object_id=&date=（只读，docs/07 §4.1）。

    成功 200：
        {"object_id":"obj_x","date":"2026-09-09","day_of_week":3,
         "periods":[{"idx":1,"name":"第1节","start":"08:00","end":"08:45",
                     "enabled":true}, ...],
         "cells":[{"idx":1,"mask":"0x000002",
                   "schedules":[{"id":"sch_x","title":"高一(1)班 物理",
                                 "kind":"periodic","role":"target"}]}]}
    未登录 → 401（@require_auth，嵌套错误体）
    group 越权 → 403（K.resolve_authorized_org，与 /api/schedules 同源）
    object_id 不存在 / 不属于该组织 → 404
    object_id 或 date 缺失、date 格式非法 → 400
    空白日 → **200 + cells=[]**（「这天没课」是正常业务态，不是错误）

    `group` 与 /api/schedules 一样用 `K.parse_group`（可缺省）：缺省时
    经 `resolve_authorized_org` 走 `authz.default_group_id` 定位用户自己的组，
    「没传 group」不会绕过授权。
    """
    user_id = context["user_id"]
    group = K.parse_group(query)
    object_id = _required(query, "object_id")
    d = _parse_day(query)

    conn = config.open_db()
    try:
        _group, org_id = K.resolve_authorized_org(conn, user_id, group)
        _require_object_in_org(conn, org_id, object_id)
        return _detail(conn, org_id, object_id, d)
    finally:
        conn.close()


ROUTES = {
    ("GET", "/api/schedules"): list_schedules,
    ("GET", "/api/schedules/detail"): schedule_detail,
}
