"""B5（S17）共用内核：自然语言需求「本地解析 + 冲突检测」的唯一口径源。

🔴🔴 本文件存在的唯一理由：让 /api/demands 的「解析规则」与「冲突反馈」
集中在后端，前端永远不知道规则长什么样（docs/07 §7.1/§7.2）。

用途与设计意图（答辩口径）：
  1. 本地解析（零第三方、断网可跑）：用正则规则从原话里尽量识别
     「星期（下周三）/ 时段（下午）/ 对象（实验室A）/ 节数（两节课）」。
     识别不到就诚实返回 None，绝不编造——「不静默失败」的前提是「不假装听懂」。
  2. 🔴 数据合规（AGENTS §7 / docs/07 §7.5）：本模块**绝不外发任何内容**。
     将来若要接外部大模型，只允许把 raw_text 本身发出去，严禁附课表/班级/
     姓名/联系方式。本实现纯本地，天然满足。
  3. 冲突检测走领域层 build_occupancy + busy_mask（与 /api/conflicts 同源口径），
     **不自己另写一套位运算**——否则会和 b3_kernels 的掩码口径漂移（已付过代价的坑）。

只用 Python 标准库。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from scheduler.db import init_db
from scheduler.occupancy import build_occupancy, busy_mask
from scheduler.slots import SlotConfig, decode_mask, load_slot_config


# ---------------------------------------------------------------------------
# 建表（幂等）
# ---------------------------------------------------------------------------

#: 调整幅度档位词表（S18）。词汇与 /api/resolve 的 mode 同源（docs/07 §6），
#: 默认 minimal（docs/19 §224 泄压阀：先做默认档起步，不做全套档位）。
DEMAND_LEVELS = ("minimal", "balanced", "optimal")

#: 缺省档位：请求不传 level 时的取值，也是旧数据（无档位行）读出的值。
DEFAULT_DEMAND_LEVEL = "minimal"

#: S18 档位存储：1:1 旁表（schema.sql 末尾）。🔴 为什么不是
#: ALTER demands ADD COLUMN level——ALTER 非幂等，而 init_db 契约是
#: 「可重复调用」，seeds.seed_demo 内部会二次 init_db，放 ALTER 会让既有
#: 「init_db + seed_demo」调用链全部 duplicate column name（2026-10-13 实测
#: 5 个既有测试 ERROR）。旁表 CREATE IF NOT EXISTS 幂等，新库旧库同一套 DDL。
_LEVEL_TABLES = ("demands", "demand_levels")


def ensure_demands(conn: sqlite3.Connection) -> None:
    """确保 demands 表与 demand_levels 档位表都存在（幂等）。

    🔴 与 telemetry.ensure_event_log 同款问题（2026-10-11 PR#5 审查实测发现）：
    ensure_database 只在**库文件不存在**时 seed，已存在的 data/*.db（trial.db /
    demo.db）**不会补进 schema.sql 新增的表**。缺表时 POST /api/demands 会
    直接 500（sqlite3.OperationalError: no such table），而 /api/health 仍报
    200 → 典型的假绿灯。

    S18 新增 demand_levels 旁表后加深一层：demands 表在（S17 建的旧库）、
    **demand_levels 不在** → 只查 demands 会误判「已就绪」，写档位行时报
    no such table: demand_levels（同为静默失效家族）。故两张表都查，
    缺任一则 init_db（schema.sql 全量 IF NOT EXISTS，幂等安全）。

    **schema.sql 是这两张表的唯一 DDL 来源**，杜绝表结构在两处各写一遍的漂移。
    """
    missing = [
        name for name in _LEVEL_TABLES
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is None
    ]
    if missing:
        init_db(conn)


def read_level(conn: sqlite3.Connection, demand_id: str) -> str:
    """读某条需求的调整幅度档位；旧数据（无旁表行）→ 默认 minimal。

    🔴 旧库升级后，S18 之前登记的 demands 没有 demand_levels 行——
    COALESCE 兜底为 minimal（docs/19 §224：默认档行为与无此字段一致），
    绝不返回 None 让调用方各自猜。
    """
    row = conn.execute(
        "SELECT COALESCE(dl.level, ?) AS level FROM demands d "
        "LEFT JOIN demand_levels dl ON dl.demand_id = d.id WHERE d.id = ?",
        (DEFAULT_DEMAND_LEVEL, demand_id),
    ).fetchone()
    return row["level"] if row else DEFAULT_DEMAND_LEVEL


# ---------------------------------------------------------------------------
# 解析规则（本地 · 纯规则，可扩展）
# ---------------------------------------------------------------------------

#: 中文星期 → ISO isoweekday（周一=1 … 周日=7；"天/日"都指周日）
_WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

#: 中文数字 → 数值（"两"=2）
_CN_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

#: 相对日期词 → 天数偏移（"今天"=0、"明天"=1…）
_DAY_OFFSET = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}

#: 周偏移词 → 周偏移（"上周"=-1、"本周/这周"=0、"下周"=+1）
_WEEK_OFFSET = {"上": -1, "下": 1, "本": 0, "这": 0}

#: 「(上/下/本/这) + (个)? + 周/星期/礼拜 + 星期几」→ (周偏移, 星期汉字)
_WEEK_RE = re.compile(r"(上|下|本|这)?(?:个)?(?:周|星期|礼拜)([一二三四五六日天])")

#: 相对日期词「今天/明天/后天/大后天」
_DAY_RE = re.compile(r"(今天|明天|后天|大后天)")

#: 时段词（保留在 parsed 里供人看，不做「时段→节次」硬映射——作息是数据不是代码）
_PERIOD_RE = re.compile(r"(上午|中午|下午|晚上|早读|晚自习|午休)")

#: 节数：「两节课 / 2 节课 / 三节」→ 数量
_COUNT_RE = re.compile(r"([一二两三四五六七八九十]|\d+)\s*节")

#: 对象名：「实验室A / 教室01 / 机房2 …」→ 名称（含字母/数字/中文数字尾缀）
_OBJECT_RE = re.compile(
    r"(实验室|教室|机房|会议室|操场|礼堂|场地|报告厅|体育馆|图书馆|音乐教室|美术教室|舞蹈房|科学教室)"
    r"[A-Za-z0-9一二三四五六七八九十]{0,3}"
)


def _weekday_date(today: date, weekday_iso: int, week_offset: int) -> date:
    """today 所在周（周一起）的星期 weekday_iso，再按 week_offset 平移整周。"""
    monday = today - timedelta(days=today.weekday())
    base = monday + timedelta(days=weekday_iso - 1)
    return base + timedelta(days=week_offset * 7)


def parse_demand(raw_text: str, hard: bool, today: date) -> dict:
    """把用户原话解析成结构化描述（纯本地规则，尽力而为）。

    返回 dict 的键（稳定、可扩展，供落库追溯与前端展示）：
      text / hard / recognized / date / weekday / week_offset / period / object / count

    🔴 原则：识别不到就置 None + recognized=False，**不编造**。
    这样 detect_demand_conflicts 才能据此「明确反馈解析不了」，而不是假装听懂。
    """
    parsed: dict = {
        "text": raw_text,
        "hard": bool(hard),
        "recognized": False,
        "date": None,          # ISO 'YYYY-MM-DD' 或 None
        "weekday": None,       # 汉字，如 "三"
        "week_offset": None,   # -1/0/+1
        "period": None,        # 时段词，如 "下午"
        "object": None,        # 对象名，如 "实验室A"
        "count": None,         # 节数，如 2
    }

    # -- 星期（含上下周）与相对日期 --
    m = _WEEK_RE.search(raw_text)
    if m:
        offset_word, weekday_han = m.group(1), m.group(2)
        parsed["weekday"] = weekday_han
        parsed["week_offset"] = _WEEK_OFFSET.get(offset_word, 0)
        parsed["date"] = _weekday_date(today, _WEEKDAY[weekday_han], parsed["week_offset"]).isoformat()
        parsed["recognized"] = True
    else:
        m = _DAY_RE.search(raw_text)
        if m:
            parsed["date"] = (today + timedelta(days=_DAY_OFFSET[m.group(1)])).isoformat()
            parsed["recognized"] = True

    # -- 时段 --
    m = _PERIOD_RE.search(raw_text)
    if m:
        parsed["period"] = m.group(1)

    # -- 节数 --
    m = _COUNT_RE.search(raw_text)
    if m:
        tok = m.group(1)
        parsed["count"] = _CN_NUM.get(tok, int(tok) if tok.isdigit() else None)

    # -- 对象名 --
    m = _OBJECT_RE.search(raw_text)
    if m:
        parsed["object"] = m.group(0)

    return parsed


# ---------------------------------------------------------------------------
# 冲突检测（与 /api/conflicts 同源口径）
# ---------------------------------------------------------------------------


def _find_object(conn: sqlite3.Connection, org_id: str, name: str) -> dict | None:
    """按名称子串匹配本组织对象（优先资源/教室，其次任意对象）。"""
    row = conn.execute(
        "SELECT id, name, kind FROM objects WHERE org_id = ? AND name = ?",
        (org_id, name),
    ).fetchone()
    if row:
        return {"id": row["id"], "name": row["name"], "kind": row["kind"]}
    row = conn.execute(
        "SELECT id, name, kind FROM objects WHERE org_id = ? AND name LIKE ? LIMIT 1",
        (org_id, f"%{name}%"),
    ).fetchone()
    if row:
        return {"id": row["id"], "name": row["name"], "kind": row["kind"]}
    return None


def detect_demand_conflicts(
    conn: sqlite3.Connection,
    org_id: str,
    parsed: dict,
    cfg: SlotConfig | None = None,
) -> tuple[list[dict], str]:
    """根据解析结果检测与现有日程的冲突，返回 (conflicts, message)。

    🔴 绝不静默失败（docs/07 §7）——四种情况都给明确 message：
      1. 识别不到对象 → 明说「未识别到对象」，不假装检测。
      2. 识别不到日期 → 明说「未识别到时间」，不假装检测。
      3. 识别到对象+日期、确有占用 → conflicts 列出占用节次 + message 明说冲突。
      4. 识别到对象+日期、无占用 → message 明说「暂无冲突」。
    """
    cfg = cfg or load_slot_config(conn, org_id)

    obj_name = parsed.get("object")
    date_str = parsed.get("date")

    if not obj_name:
        return [], (
            "已按原话登记需求，但未能识别出时间或地点对象，"
            "请人工确认后补充（绝不静默失败）"
        )
    if not date_str:
        return [], (
            f"已按原话登记需求（对象「{obj_name}」），但未能识别出具体日期，"
            "请人工确认时间后补充"
        )

    obj = _find_object(conn, org_id, obj_name)
    if obj is None:
        return [], (
            f"已按原话登记需求，但组织内未找到对象「{obj_name}」，"
            "可能名称不符或对象不存在，请人工核对"
        )

    target = date.fromisoformat(date_str)
    entries = build_occupancy(conn, org_id, target, target, cfg=cfg)
    occupied = busy_mask(entries, [obj["id"]], target)

    if occupied == 0:
        hard_label = "（硬需求，必须满足）" if parsed.get("hard") else "（软需求，尽量安排）"
        return [], f"对象「{obj['name']}」在 {date_str} 暂无冲突，需求已登记 {hard_label}"

    slots = [s for s in decode_mask(occupied) if s[0] == target.isoweekday()]
    readable = [{"dow": int(d), "period": int(p)} for d, p in slots]
    n = len(readable)
    conflicts = [{
        "object_id": obj["id"],
        "object_name": obj["name"],
        "date": date_str,
        "readable": readable,
        "message": f"对象「{obj['name']}」在 {date_str} 已有 {n} 节占用，与需求冲突",
    }]
    return conflicts, (
        f"该需求与现有 {n} 节课表冲突，无法直接满足，已明确列出冲突节次"
    )
