"""B9 共用内核：/api/exceptions（临时微调）的**唯一口径源**。

🔴🔴 本文件存在的唯一理由（与 b3_kernels 同一条纪律）：防止同一口径在多个
接口各算一遍而漂移。`api_exceptions.py` 只做「取参数 → 调这里 → 拼响应」，
**禁止自己再实现一遍**校验/掩码/去重逻辑。

---------------------------------------------------------------------------
🔴🔴 掩码有两套位序，混用会让页面全部错位且不报错（本项目最贵的坑）
---------------------------------------------------------------------------

| 口径 | 函数 | 位序 | 示例 |
|---|---|---|---|
| **周掩码**（入库 storage） | `slots.bit(dow, idx)` | 0-based，位号 =(周几-1)*16+(节次-1) | `0x100000000` = 周三第 1 节 |
| **单日掩码**（API 出入参） | 本文件 `api_mask_to_week` / `week_to_api_mask` | 1-based，period N → 位 N | `0x000002`=第1节 `0x0008`=第3节 |

黄金用例：API 收 `"0x000002"`（周三第 1 节）→ 入库 `"100000000"`（周掩码裸
hex，与 `schedules.slot_bitmap` 同口径，无 `0x` 前缀）。
🔴 `0x100000000` 与 `0x000002` 绝不相等——测试里必须反向断言这一点，
否则「用错位序」这个 bug 会安静地通过测试。

---------------------------------------------------------------------------
Q1 定论：new_mask 校验链（全部走 400，绝不静默兜底）
---------------------------------------------------------------------------

按顺序逐条校验，第一条不过即 400（错误信息说清是哪一条）：

  1. 必须是字符串、非空、可解析为十六进制（`0x` 前缀可选）；
  2. 必须 > 0（全零 = 「把这节课删掉」，那是 mode='cancel' 的语义，
     不允许用 move 表达——否则取消和改点两种意图会互相污染）；
  3. 只允许位 1..16（位 0 恒为 0，且不超过 16 节/天的掩码宽度）；
  4. 转成周掩码后，**每一个置位都必须是当天已启用的槽位**
     （`cfg.get(dow, period)` 查得到且 `enabled` 为真）。未配置的节次与
     「已配置但当天未启用」的节次（如午休）分别报不同的错——两者修法不同，
     合成一条消息教师不知道该改哪儿。改到未启用节次会让这节课直接不可见，
     属于 P0-3 明令禁止的假阴性。

---------------------------------------------------------------------------
Q3 定论：hex 存取规范化（幂等）
---------------------------------------------------------------------------

  入库 = `mask_to_hex(week_mask)`（裸 hex，无前缀，与 slot_bitmap 一致）；
  出库 = `day_mask_hex(week_to_api_mask(dow, week_mask))`（`0x%06x`）。
  规范化保证 `week_to_api_mask(api_mask_to_week(m)) == m`（m 只含位 1..16），
  因此同一份数据反复读写不会漂移。

只用 Python 标准库。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from . import b3_kernels as B3
from .http_api import ApiError

from scheduler.occupancy import (
    ensure_schedule_exceptions,
    expand_once,
    expand_periodic,
    load_exceptions,
    load_terms,
)
from scheduler.slots import (
    MAX_PERIODS_PER_DAY,
    bit,
    decode_mask,
    load_slot_config,
    mask_to_hex,
)

# ---------------------------------------------------------------------------
# 契约常量（docs/07 §11）
# ---------------------------------------------------------------------------

#: POST 接受的 mode（v1.0 冻结）。'special' 是 DDL 里预留的枚举值，
#: 本接口刻意不开放——没有「换一套作息」的前端与规则支撑前，开放它
#: 等于开放一条没有语义的写入路径。
EXCEPTION_MODES = ("cancel", "move")

#: 单日掩码的合法位范围：1..16（位 0 不用，见 parse_api_mask 第 3 条）。
_API_MASK_MIN_BIT = 1
_API_MASK_MAX_BIT = MAX_PERIODS_PER_DAY

#: API 出入参日期格式。🔴 不用 date.fromisoformat 的宽松解析（3.11+ 接受
#: '20260907'），与 api_schedules._WEEK_RE 同一条纪律：前端拼错参数必须
#: 查得出来，不能悄悄通过。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 单日掩码十六进制字符串（`0x` 前缀可选，大小写不敏感，首尾空白剥掉）。
_HEX_RE = re.compile(r"^(?:0[xX])?([0-9a-fA-F]+)$")


# ---------------------------------------------------------------------------
# 1. 掩码口径（🔴 本项目最贵的坑，勿删上面的 docstring）
# ---------------------------------------------------------------------------


def api_mask_to_week(dow: int, api_mask: int) -> int:
    """单日掩码（1-based）→ 周掩码（0-based，落在 dow 这一天的 16 位车道）。

    只搬位、不做任何校验（校验在 `parse_api_mask`）。黄金用例：
    api_mask_to_week(3, 0x000002) == 0x100000000。
    """
    out = 0
    for period in range(_API_MASK_MIN_BIT, _API_MASK_MAX_BIT + 1):
        if (api_mask >> period) & 1:
            out |= bit(dow, period)
    return out


def week_to_api_mask(dow: int, week_mask: int) -> int:
    """周掩码（0-based）→ 单日掩码（1-based），只取 dow 当天的车道。

    黄金用例：week_to_api_mask(3, 0x100000000) == 0x000002。
    其他天的位被静默丢弃——周掩码按 (周几, 节次) 编址，
    属于当天的只有 dow 那一段车道。
    """
    out = 0
    for period in range(_API_MASK_MIN_BIT, _API_MASK_MAX_BIT + 1):
        if week_mask & bit(dow, period):
            out |= 1 << period
    return out


def parse_api_mask(raw, dow: int, cfg) -> int:
    """API 的 new_mask 字符串 → **周掩码 int**（入库用）。

    完整校验链见模块 docstring「Q1 定论」。任何一条不过都抛
    ApiError(400)——本接口的核心承诺是「改错了立刻知道」，
    静默兜底（比如把越界位悄悄截掉）会让教师以为改成功了。
    """
    if not isinstance(raw, str):
        raise ApiError(
            400, f"new_mask 必须是字符串（如 \"0x000002\"），收到 {type(raw).__name__}"
        )
    text = raw.strip()
    if not text:
        raise ApiError(400, "new_mask 不能为空（取消请用 mode='cancel'）")
    matched = _HEX_RE.match(text)
    if matched is None:
        raise ApiError(400, f"new_mask 不是合法十六进制：{raw!r}")
    api_mask = int(matched.group(1), 16)

    if api_mask == 0:
        raise ApiError(400, "new_mask 不能为全零（取消这节课请用 mode='cancel'）")
    if api_mask & 1:
        raise ApiError(400, f"new_mask 位 0 无意义（节次从 1 开始），收到 {raw!r}")
    if api_mask >> (_API_MASK_MAX_BIT + 1):
        raise ApiError(400, f"new_mask 超出 {_API_MASK_MAX_BIT} 节/天的宽度，收到 {raw!r}")

    week_mask = api_mask_to_week(dow, api_mask)
    # 🔴 必须逐位查 cfg.get(dow, period) 而不是跟 all_enabled_mask 按位与：
    #    后者只含"已启用"的槽位，"未配置"与"配置了但当天未启用"会被合并成
    #    同一种错，教师不知道该找管理员加节次还是去开午休。
    missing: list[int] = []
    disabled: list[int] = []
    for period in range(_API_MASK_MIN_BIT, _API_MASK_MAX_BIT + 1):
        if not (api_mask >> period) & 1:
            continue
        slot = cfg.get(dow, period)
        if slot is None:
            missing.append(period)
        elif not slot["enabled"]:
            disabled.append(period)
    if missing:
        raise ApiError(
            400,
            "new_mask 包含未配置的节次："
            + "、".join(f"周{dow}第{p}节" for p in missing)
            + "——请先让管理员在节次配置里补上这些节次",
        )
    if disabled:
        raise ApiError(
            400,
            f"new_mask 包含周{dow}未启用的槽位（如午休/当日不用的节次）："
            + "、".join(f"第{p}节" for p in disabled)
            + "——改到这些节次这节课会直接不可见",
        )
    return week_mask


def parse_date(raw) -> str:
    """date 参数 → ISO 字符串（严格 YYYY-MM-DD，且必须是真实存在的日期）。"""
    if not isinstance(raw, str) or _DATE_RE.match(raw.strip()) is None:
        raise ApiError(400, f"date 需要 YYYY-MM-DD 格式，收到 {raw!r}")
    text = raw.strip()
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ApiError(400, f"date 不是合法日期：{raw!r}") from exc
    return text


def api_mask_readable(dow: int, api_mask: int) -> list[dict]:
    """单日掩码 → 前端可直接显示的 [{dow, period}]（契约 §12.2 第 5 条）。

    🔴 绝不能把单日掩码喂给 `slots.decode_mask`——那个函数按**周掩码**
    位序解码（0-based），会把「第 3 节」解成「周一第 4 节」且不报错。
    单日掩码的位号本身就是节次号（1-based），dow 由调用方显式给出。
    黄金用例：api_mask_readable(3, 0x000008) == [{"dow": 3, "period": 3}]。
    """
    return B3.readable(
        [(dow, p) for p in range(_API_MASK_MIN_BIT, _API_MASK_MAX_BIT + 1)
         if (api_mask >> p) & 1]
    )


# ---------------------------------------------------------------------------
# 2. 目标日程解析（组织隔离 + 只放行周期日程）
# ---------------------------------------------------------------------------

#: 取 v_schedule_full（schedules ⋈ periodic/once 的视图行）——一次拿到
#: kind/title/week_parity/slot_bitmap/valid_from/valid_to/start_at/end_at，
#: 免得到处 JOIN。🔴 视图是 INNER JOIN：孤儿日程（两张扩展表都缺行）
#: 查不到 → 404，与 P0-3 的 orphan_schedule 信号各司其职（前者拒绝写入，
#: 后者在扫描时上抛）。
_TARGET_SQL = "SELECT * FROM v_schedule_full WHERE id = ? AND org_id = ?"


def load_target(conn: sqlite3.Connection, org_id: str, schedule_id) -> sqlite3.Row:
    """schedule_id → v_schedule_full 行；不存在/跨组织 → 404，非周期 → 400。

    🔴 为什么不存在也给 404 而不是 400：契约 docs/07 §11「越权 → 404」。
    用 400 会泄露「这个 id 到底存不存在」（跨组织探测），
    404 对合法用户与越权者是同一个答案。
    """
    if not isinstance(schedule_id, str) or not schedule_id.strip():
        raise ApiError(400, "缺少 schedule_id（要临时取消/改点哪条日程）")
    row = conn.execute(_TARGET_SQL, (schedule_id.strip(), org_id)).fetchone()
    if row is None:
        raise ApiError(404, "日程不存在或不属于当前组织")
    if row["kind"] != "periodic":
        raise ApiError(
            400,
            f"只支持对周期日程做临时取消/改点，该日程 kind={row['kind']!r}。"
            "一次性活动请直接修改或删除活动本身。",
        )
    return row


# ---------------------------------------------------------------------------
# 3. 生效性探测（诚实回报，不静默）
# ---------------------------------------------------------------------------


def exception_effective(
    conn: sqlite3.Connection,
    org_id: str,
    row: sqlite3.Row,
    iso: str,
    cfg,
    mode: str,
    new_mask_week: int | None,
) -> bool:
    """这条例外写下去，当天占用是否真的会变。

    返回 False 的合法场景（都不报错，只是让响应体如实说「没生效」）：
    该日期不在日程有效期内、是周末/假期、单双周不匹配，或 move 的目标
    掩码与原掩码相同——老师可以预先取消一节还没排的课，撤销时也不会
    因为「当时没生效」而卡住。

    🔴 这与 P0-3「不许静默跳过」不矛盾：这里返回的是**写入结果的事实**，
    调用方把它显式放进响应体的 `effective` 字段（docs/07 §12.3 非破坏性
    加字段），教师看得见；领域层 `apply_schedule_exceptions` 对「例外比
    展开结果窄」同样不抛异常，因为那不是数据缺陷。
    """
    d = date.fromisoformat(iso)
    if not (row["valid_from"] <= iso <= row["valid_to"]):
        return False
    if row["kind"] == "periodic":
        # 🔴 P2 修复（卡 rvdL9a 顺手项）：第 9 个参数曾是 None ——
        #    等于告诉 expand_periodic「这天没有任何例外」。而本函数要回答的
        #    正是「写下这条例外，当天占用是否真的会变」，于是：
        #      · 该日是调休日（mode='as_of'）→ 按默认作息展开，天数/掩码都可能不同；
        #      · 该日换了作息方案（mode='special'）→ 默认 8 节 vs 方案 7 节，
        #        「第 8 节取消了」这种例外会被算成 effective=true，**字段撒谎**。
        #    两类都是「返回 true 但实际不生效」的假阳性，属于本项目最忌的静默失效。
        #    改为一律读真例外：与 expand_periodic 在占用计算时的入参保持同源。
        days = expand_periodic(
            row["week_parity"],
            int(row["slot_bitmap"], 16) if row["slot_bitmap"] else 0,
            row["valid_from"],
            row["valid_to"],
            cfg,
            load_terms(conn, org_id),
            d,
            d,
            load_exceptions(conn, org_id),
        )
    else:
        days = expand_once(row["start_at"], row["end_at"], cfg)
    if d not in days:
        return False
    if mode == "move":
        return days[d] != new_mask_week
    return True


# ---------------------------------------------------------------------------
# 4. 写入 / 撤销 / 查询
# ---------------------------------------------------------------------------


def find_active(
    conn: sqlite3.Connection, org_id: str, schedule_id: str, iso: str
) -> sqlite3.Row | None:
    """同 (日程, 日期) 上是否已有一条 cancel/move 例外。

    🔴 必须拦（否则会静默失效或直接 500）：
      · cancel + move 同 key 两条行 → load_exceptions 同时填 cancels 与
        moves，apply_schedule_exceptions 里 cancel 先命中、move 永远不
        生效，教师看到「改点没反应」且没有任何提示；
      · 两条 move 且 new_mask 不同 → load_exceptions 抛 ValueError →
        全组织 /api/conflicts 500（一条脏数据打死一个接口）。
    """
    return conn.execute(
        "SELECT id, mode, new_mask FROM schedule_exceptions"
        " WHERE org_id = ? AND scope = 'schedule' AND target_id = ? AND date = ?"
        " AND mode IN ('cancel', 'move')",
        (org_id, schedule_id, iso),
    ).fetchone()


def new_exception_id() -> str:
    """例外 id（`exc_` 前缀，与契约 §11 的示例 `exc_x` 同族）。"""
    from scheduler.db import new_id

    return new_id("exc")


def create(
    conn: sqlite3.Connection,
    org_id: str,
    schedule_id: str,
    iso: str,
    mode: str,
    new_mask_week: int | None,
    note: str | None,
) -> str:
    """写入一条 scope='schedule' 例外并 commit，返回 exception_id。"""
    exception_id = new_exception_id()
    conn.execute(
        "INSERT INTO schedule_exceptions"
        " (id, org_id, scope, target_id, date, mode, new_mask, note)"
        " VALUES (?, ?, 'schedule', ?, ?, ?, ?, ?)",
        (
            exception_id,
            org_id,
            schedule_id,
            iso,
            mode,
            mask_to_hex(new_mask_week) if new_mask_week is not None else None,
            note,
        ),
    )
    conn.commit()
    return exception_id


def revoke(conn: sqlite3.Connection, org_id: str, exception_id: str) -> dict:
    """按 id 撤销一条例外并 commit；不存在/跨组织 → 404。"""
    if not isinstance(exception_id, str) or not exception_id.strip():
        raise ApiError(400, "缺少参数 id（要撤销哪条例外）")
    row = conn.execute(
        "SELECT id, target_id, date FROM schedule_exceptions"
        " WHERE id = ? AND org_id = ?",
        (exception_id.strip(), org_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "例外不存在或不属于当前组织")
    conn.execute("DELETE FROM schedule_exceptions WHERE id = ?", (row["id"],))
    conn.commit()
    return {
        "exception_id": row["id"],
        "schedule_id": row["target_id"],
        "date": row["date"],
    }


def list_for_schedule(
    conn: sqlite3.Connection, org_id: str, schedule_id: str
) -> list[dict]:
    """某条日程的全部未撤销例外（按日期、再按 id 全序 → 两次调用逐字一致）。"""
    rows = conn.execute(
        "SELECT id, target_id, date, mode, new_mask, note, created_at"
        " FROM schedule_exceptions"
        " WHERE org_id = ? AND scope = 'schedule' AND target_id = ?"
        " ORDER BY date, id",
        (org_id, schedule_id),
    ).fetchall()
    return [exception_payload(conn, r) for r in rows]


def exception_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """例外行 → API 字典。new_mask 从周掩码转回单日掩码（Q3 口径）。

    🔴 `readable` 必须给（契约 §12.2 第 5 条：前端只透传展示，不做位运算），
    否则撤销按钮上只能显示一串 hex，教师无法确认撤的是哪一条。
    """
    d = date.fromisoformat(row["date"])
    new_mask = None
    readable = None
    if row["new_mask"]:
        api_mask = week_to_api_mask(d.isoweekday(), int(row["new_mask"], 16))
        new_mask = B3.day_mask_hex(api_mask)
        # 🔴 必须用 api_mask_readable（单日位序），不是 decode_mask（周位序）
        readable = api_mask_readable(d.isoweekday(), api_mask)
    title = conn.execute(
        "SELECT title FROM schedules WHERE id = ?", (row["target_id"],)
    ).fetchone()
    return {
        "id": row["id"],
        "schedule_id": row["target_id"],
        "title": (title["title"] if title else None),
        "date": row["date"],
        "mode": row["mode"],
        "new_mask": new_mask,
        "readable": readable,
        "note": row["note"],
        "created_at": row["created_at"],
    }


__all__ = [
    "EXCEPTION_MODES",
    "api_mask_to_week",
    "week_to_api_mask",
    "api_mask_readable",
    "parse_api_mask",
    "parse_date",
    "load_target",
    "exception_effective",
    "find_active",
    "create",
    "revoke",
    "list_for_schedule",
    "exception_payload",
    "new_exception_id",
    "ensure_schedule_exceptions",
    "load_slot_config",
]
