"""统一占用模型：把周期日程和一次性活动都展开成「(日期, 当天槽位掩码)」。

设计意图（本文件是全系统的核心，答辩时大概率要讲）：

    课表和活动看起来是两回事，但它们在时间上的本质是一样的：
    **某人在某天的某些节次被占用**。

    所以这里把它们归一化成同一个形态：
        周期日程：week_parity + slot_bitmap + 生效区间  ─┐
        一次性活动：start_at / end_at 时刻              ─┴→  {日期: 当天掩码}

    归一化之后，冲突检测只有一条规则：
        同一个对象、同一天，两条日程的掩码按位与 ≠ 0 → 冲突
    而"与"的结果本身就是冲突发生在哪几节，可以直接用来生成消解建议。

    这样做最大的收益是：**AI 排课能看见已安排的活动**。
    如果两套东西各存各的、各算各的，排课时就会把课排在活动上——
    这是真实的功能缺陷，不是理论问题。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .db import init_db
from .slots import (
    SlotConfig,
    bit,
    decode_bit_index,
    hex_to_mask,
    load_slot_config,
    popcount,
)


# ---------------------------------------------------------------------------
# 学期与周次
# ---------------------------------------------------------------------------


@dataclass
class Term:
    id: str
    org_id: str
    name: str
    start_date: date
    end_date: date
    week1_parity: int  # 第 1 周是单周(1)还是双周(2)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def load_terms(conn: sqlite3.Connection, org_id: str) -> list[Term]:
    rows = conn.execute(
        "SELECT * FROM terms WHERE org_id = ? ORDER BY start_date", (org_id,)
    ).fetchall()
    return [
        Term(
            r["id"],
            r["org_id"],
            r["name"],
            _parse_date(r["start_date"]),
            _parse_date(r["end_date"]),
            r["week1_parity"],
        )
        for r in rows
    ]


def create_term(
    conn: sqlite3.Connection,
    org_id: str,
    name: str,
    start_date: str,
    end_date: str,
    week1_parity: int = 1,
) -> str:
    tid = "term_" + org_id.split("_")[-1] + "_" + name
    conn.execute(
        "INSERT OR REPLACE INTO terms(id, org_id, name, start_date, end_date, week1_parity)"
        " VALUES (?,?,?,?,?,?)",
        (tid, org_id, name, start_date, end_date, week1_parity),
    )
    conn.commit()
    return tid


def pick_term(terms: list[Term], d: date) -> Term | None:
    for t in terms:
        if t.start_date <= d <= t.end_date:
            return t
    return None


def week_index(term: Term, d: date) -> int:
    """第几周（从 1 开始）。约定：不入库，需要显示时由这里推算。"""
    return (d - term.start_date).days // 7 + 1


def week_parity_of(term: Term, d: date) -> int:
    """该日期是单周(1)还是双周(2)。"""
    wk = week_index(term, d)
    odd_is = term.week1_parity  # 第 1 周（奇数周）的奇偶
    even_is = 3 - odd_is
    return odd_is if wk % 2 == 1 else even_is


# ---------------------------------------------------------------------------
# 展开
# ---------------------------------------------------------------------------


# P0-3 统一信号：静默跳过的「可见化」出口。code 取值（本卡定死，加新码须改此表）：
#   periodic_no_term = 周期日程的某天落在任何 term 区间外 → 该日程当天不展开（不可见）
#   once_no_slot     = 一次性活动某天不落在任何启用槽位（如午休/周末）→ 活动当天不可见
#   orphan_schedule  = schedules 有主表行但 schedule_periodic / schedule_once 都缺扩展行
#                      → INNER JOIN 视图 v_schedule_full 不含它，冲突扫描完全看不见
#   move_no_slot     = B9：某条周期日程被 move 改点后，新掩码与当天启用槽位无交集
#                      → 改点在这些日期等于把课改没了（课表凭空不见）
#   special_slot_missing = B-1：该日切换了作息方案（mode='special'），而日程排在该方案
#                      不存在的节次（如冬季取消第 8 节）→ 那天不占用是对的，但必须
#                      让老师看见「这节课在当天不存在」，不许静默消失
ISSUE_PERIODIC_NO_TERM = "periodic_no_term"
ISSUE_ONCE_NO_SLOT = "once_no_slot"
ISSUE_ORPHAN_SCHEDULE = "orphan_schedule"
ISSUE_MOVE_NO_SLOT = "move_no_slot"
ISSUE_SPECIAL_SLOT_MISSING = "special_slot_missing"

_ISSUE_TEXT = {
    ISSUE_PERIODIC_NO_TERM: (
        "周期日程的 {count} 天落在任何 term 区间外，该日程在这些日期不展开（课表不可见）。"
        "若非假期场景，多半是 terms 表缺该学期行。日期：{dates}"
    ),
    ISSUE_ONCE_NO_SLOT: (
        "活动时段在 {count} 天不落在任何启用槽位（如午休/未启用的节次），"
        "活动在这些日期完全不可见。日期：{dates}"
    ),
    ISSUE_ORPHAN_SCHEDULE: (
        "schedules(id={schedule_id}) 在 schedule_periodic / schedule_once 两张扩展表"
        "都缺对应行——INNER JOIN 视图 v_schedule_full 不含它，冲突扫描完全看不见。"
    ),
    ISSUE_MOVE_NO_SLOT: (
        "日程 {schedule_id} 在 {count} 天被 move 改点后的新掩码与该日启用槽位无交集"
        "（如改到午休/未启用节次），改点在这些日期等于把课改没了。"
        "日期：{dates}"
    ),
    ISSUE_SPECIAL_SLOT_MISSING: (
        "该 {count} 天使用了作息方案「{extra}」，日程排在该方案里不存在的节次。"
        "当天不占用是对的，但老师在课表上看不到这几节课——若无意为，"
        "请检查该方案是否漏配这些节次。日期：{dates}"
    ),
}


@dataclass
class ExpansionIssue:
    """P0-3：一处「本来会静默跳过」的展开事实记录。

    设计约定（队长卡面纪律）：signal 必须可见——**计数并返回给调用方**
    （scan 会挂到 ConflictReport.issues、/api/conflicts 会带 warnings），
    不许只 print，也不许无声 continue。这里**不抛异常**：孤儿行/无 term/
    午休活动都可能是真实数据，抛错会一条脏数据打死整个扫描接口
    （试用期内不可接受）；load_exceptions 的 ValueError 语义是 P0-2 定的
    另一条路（配置错误宁可报错），两者不冲突。

    明确**不**算静默跳过、不记 issue 的设计内行为：
      单双周不匹配（week_parity）、周末/无启用槽位的周期日程（day_mask=0）、
      放假（calendar_exceptions holiday）——这些是业务规则，不是数据缺陷。
    """

    code: str
    schedule_id: str = ""
    schedule_kind: str = ""
    schedule_title: str = ""
    dates: list[str] = field(default_factory=list)  # 受影响日期（ISO，已排序）
    count: int = 0  # 受影响天数（orphan 恒为 1）
    #: 额外上下文，供 {extra} 占位符使用（B-1 special_slot_missing 用：方案名 + 缺的节次）。
    #: 现有 4 个 code 的文案都不引用 {extra}，故新增本字段对既有输出**逐字节无影响**。
    extra: str = ""

    @property
    def detail(self) -> str:
        return _ISSUE_TEXT[self.code].format(
            count=self.count,
            dates=", ".join(self.dates[:8]) + (" 等" if len(self.dates) > 8 else ""),
            schedule_id=self.schedule_id,
            extra=self.extra,
        )

    def to_payload(self) -> dict:
        """给 API / 前端的字典形态（/api/conflicts 的 warnings 数组元素）。"""
        return {
            "code": self.code,
            "schedule_id": self.schedule_id,
            "kind": self.schedule_kind,
            "title": self.schedule_title,
            "count": self.count,
            "dates": list(self.dates),
            "detail": self.detail,
        }


@dataclass
class DayExceptions:
    """日期级例外的统一视图（load_exceptions 的返回值，P0-2 起）。

    五类语义，分属三张表：
      holidays = 放假（calendar_exceptions, kind='holiday'）：该日停课；
      as_of    = 调休（schedule_exceptions, scope='day', mode='as_of'）：
                 该日按 as_of_dow 周几的课表上课，键为 ISO 日期、值 1..7
                 （与 period_slots.day_of_week 同口径，省一次转换）；
      cancels  = 临时取消（schedule_exceptions, scope='schedule', mode='cancel'）：
                 键 (schedule_id, ISO)，逐日取消某条周期日程的某一天；
      moves    = 临时改点（schedule_exceptions, scope='schedule', mode='move'）：
                 键 (schedule_id, ISO)，值 = 新周掩码（DB 口径，与 slot_bitmap
                 同 0-based 位序）。B9 起由 apply_schedule_exceptions 在展开
                 后覆盖当天掩码；
      specials = 换作息方案（schedule_exceptions, scope='day', mode='special'，
                 B-1）：键为 ISO 日期、值为 period_sets.id。该日「用哪套作息」
                 由它决定；`as_of` 决定「按周几的课表」，两者可同日共存。
                 special_cfgs 是 set_id → SlotConfig 的缓存（由
                 load_period_sets 预载），让 expand_periodic 不必再查库。
    """

    holidays: set[str] = field(default_factory=set)
    as_of: dict[str, int] = field(default_factory=dict)
    cancels: set[tuple[str, str]] = field(default_factory=set)
    moves: dict[tuple[str, str], int] = field(default_factory=dict)
    specials: dict[str, str] = field(default_factory=dict)
    special_cfgs: dict[str, "SlotConfig"] = field(default_factory=dict)
    special_names: dict[str, str] = field(default_factory=dict)

    def is_holiday(self, iso: str) -> bool:
        return iso in self.holidays

    def is_cancelled(self, schedule_id: str, iso: str) -> bool:
        """某条周期日程的某一天是否被临时取消（B9）。"""
        return (schedule_id, iso) in self.cancels

    def moved_mask(self, schedule_id: str, iso: str) -> int | None:
        """某条周期日程的某一天被临时改点后的新周掩码；未改点返回 None。"""
        return self.moves.get((schedule_id, iso))

    def set_id_of(self, iso: str) -> str | None:
        """该日绑定的作息方案 id；未绑定返回 None。"""
        return self.specials.get(iso)

    @classmethod
    def coerce(
        cls, exceptions: "DayExceptions | dict[str, str] | None"
    ) -> "DayExceptions":
        """兼容三种入参：本类 / 旧格式 {ISO: kind} 字典 / None。

        旧字典口径与 P0-2 之前的 expand_periodic 完全一致：
        只有 'holiday' 生效，'makeup' 等其他值一律惰性（等于无例外、不报错）。
        """
        if exceptions is None:
            return cls()
        if isinstance(exceptions, cls):
            return exceptions
        out = cls()
        for iso, kind in exceptions.items():
            if kind == "holiday":
                out.holidays.add(iso)
        return out


def expand_periodic(
    week_parity: int,
    slot_bitmap: int,
    valid_from: str,
    valid_to: str,
    cfg: SlotConfig,
    terms: list[Term],
    date_from: date,
    date_to: date,
    exceptions: "DayExceptions | dict[str, str] | None" = None,
    *,
    issues: list[ExpansionIssue] | None = None,
) -> dict[date, int]:
    """周期日程 → {日期: 当天掩码}。

    exceptions 接受 load_exceptions() 返回的 DayExceptions（放假 + 调休统一
    视图），也兼容旧格式 {ISO: kind} 字典。调休日（mode='as_of'）改按
    as_of_dow 周几的槽位展开——位号公式不变，只是换了"查哪一天的课表"。

    B-1（换作息方案）：该日命中 `mode='special'` 时，节次配置换成
    `period_sets/set_slots` 那套（`effective_slot_config` 是唯一事实来源）。
    位号公式仍不动——第 k 节永远是第 k 位，方案只是决定「这一节存不存在」。

    P0-3：日期落在任何 term 区间外时，过去是静默 continue（课表凭空不见）。
    现在仍不展开（掩码模型下无 term 就无法定周次），但必须记入 issues
    返回给调用方——计数 + 上抛，不许无声。

    B-1「缺节不静默」：换方案后日程排在该方案不存在的节次 → 那天不占用
    是对的，但必须记一条 special_slot_missing（否则就是 PR #15 那类事故：
    课表少了一节、接口 200、零报错）。
    """
    exc = DayExceptions.coerce(exceptions)
    vf, vt = _parse_date(valid_from), _parse_date(valid_to)
    out: dict[date, int] = {}
    no_term_days: list[str] = []
    missing_days: list[str] = []
    missing_idx: set[int] = set()
    missing_sets: list[str] = []
    d = date_from
    while d <= date_to:
        iso = d.isoformat()
        if vf <= d <= vt and not exc.is_holiday(iso):
            t = pick_term(terms, d)
            if t is not None:
                if week_parity == 0 or week_parity == week_parity_of(t, d):
                    # P0-2：调休日按 as_of_dow 的课表上（如周六调休按周一）；
                    # 非调休日照旧用自然周几。位号公式（bit_index）不动。
                    dow = exc.as_of.get(iso)
                    if dow is None:
                        dow = d.isoweekday()
                    # B-1：该日若绑了作息方案，节次配置换成方案那套
                    day_cfg = effective_slot_config(exc, cfg, iso)
                    default_day_mask = cfg.day_mask(dow)
                    day_mask = slot_bitmap & day_cfg.day_mask(dow)
                    if day_mask:
                        out[d] = day_mask
                    if day_cfg is not cfg:
                        # 🔴 缺节不静默：本来在默认作息下上得了的节次，
                        # 换方案后「这一节不存在」→ 必须让人看见。
                        lost = slot_bitmap & default_day_mask & ~day_cfg.day_mask(dow)
                        if lost:
                            missing_days.append(iso)
                            # 用 decode_bit_index 反查节次，不自己重写位号公式
                            rest = lost
                            while rest:
                                low = rest & -rest
                                _dow_of_bit, idx_of_bit = decode_bit_index(
                                    low.bit_length() - 1
                                )
                                missing_idx.add(idx_of_bit)
                                rest ^= low
                            set_id = exc.specials.get(iso, "")
                            name = exc.special_names.get(set_id) or set_id
                            if name not in missing_sets:
                                missing_sets.append(name)
            elif issues is not None:
                # P0-3：无 term 覆盖且不是放假——静默跳过的可见化。
                no_term_days.append(iso)
        d += timedelta(days=1)
    if no_term_days and issues is not None:
        issues.append(
            ExpansionIssue(
                code=ISSUE_PERIODIC_NO_TERM,
                dates=sorted(no_term_days),
                count=len(no_term_days),
            )
        )
    if missing_days and issues is not None:
        issues.append(
            ExpansionIssue(
                code=ISSUE_SPECIAL_SLOT_MISSING,
                dates=sorted(missing_days),
                count=len(missing_days),
                extra="{}（缺第 {} 节）".format(
                    "、".join(missing_sets) or "未知方案",
                    "、".join(str(i) for i in sorted(missing_idx)),
                ),
            )
        )
    return out


def expand_once(
    start_at: str,
    end_at: str,
    cfg: SlotConfig,
    *,
    issues: list[ExpansionIssue] | None = None,
) -> dict[date, int]:
    """一次性活动 → {日期: 当天掩码}。跨天会自然拆成多天。

    P0-3：活动时段不落在任何启用槽位（午休开会、周末活动、未启用节次）
    时，过去是 mask=0 静默丢弃——活动在系统里"不存在"。现在仍无法在
    掩码模型里表示它（没有槽位就没有位），但必须记入 issues 让上层看见。

    aligned 的取舍（mask_for_range 第二返回值）：mask 非 0 但 aligned=False
    （跨半节）时按**保守取整**保留掩码——与启用槽位重叠的课都会被算进
    冲突，只可能多报不可能漏报，故不记 issue；只有 mask==0（完全不落
    任何启用槽位）才是真·假阴性，必须记。
    """
    s = datetime.strptime(start_at, "%Y-%m-%dT%H:%M")
    e = datetime.strptime(end_at, "%Y-%m-%dT%H:%M")
    out: dict[date, int] = {}
    invisible: list[str] = []
    d = s.date()
    while d <= e.date():
        seg_start = max(s, datetime.combine(d, datetime.min.time()))
        seg_end = min(e, datetime.combine(d, datetime.min.time()) + timedelta(days=1))
        if seg_start < seg_end:
            mask, _aligned = cfg.mask_for_range(
                d.isoweekday(),
                seg_start.strftime("%H:%M"),
                seg_end.strftime("%H:%M"),
            )
            if mask:
                out[d] = mask
            else:
                # P0-3：mask=0 → 该日不可见。过去无声丢弃，现在记入 issues。
                invisible.append(d.isoformat())
        d += timedelta(days=1)
    if invisible and issues is not None:
        issues.append(
            ExpansionIssue(
                code=ISSUE_ONCE_NO_SLOT,
                dates=sorted(invisible),
                count=len(invisible),
            )
        )
    return out


# ---------------------------------------------------------------------------
# 占用与冲突
# ---------------------------------------------------------------------------


@dataclass
class OccupancyEntry:
    """一条"某对象在某天被某日程占用了哪些节次"的记录。"""

    schedule_id: str
    object_id: str
    role: str  # target / teacher / participant / resource
    date: date
    mask: int


@dataclass
class Conflict:
    schedule_a: str
    schedule_b: str
    object_id: str
    role: str
    date: date
    mask: int  # 冲突发生在哪几节（位与的结果）

    @property
    def slots(self) -> list[tuple[int, int]]:
        from .slots import decode_mask

        return decode_mask(self.mask)


def ensure_schedule_exceptions(conn: sqlite3.Connection) -> None:
    """确保 schedule_exceptions 表存在（幂等，P0-2）。

    已存在的 data/*.db 不会在启动时重建表（ensure_database 只在库缺失时
    seed），故读前调用。用 sqlite_master 判断，缺失则跑 init_db——
    schema.sql 是本表的唯一 DDL 来源，杜绝「表结构在 schema.sql 与代码里
    各写一遍」的漂移。（范式：telemetry.ensure_event_log /
    b5_kernels.ensure_demands；本项目第 7 处静默失效正是缺这一步。）
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schedule_exceptions'"
    ).fetchone()
    if row is None:
        init_db(conn)


def ensure_period_set_tables(conn: sqlite3.Connection) -> None:
    """确保 period_sets / set_slots 两张表存在（幂等，B-1 作息方案）。

    与 ensure_schedule_exceptions 同范式：已存在的 data/*.db 不会在启动时重建
    表（ensure_database 只在库文件缺失时 seed），故**读前**调用。schema.sql 是
    唯一 DDL 来源，缺表就跑 init_db，杜绝「表结构在 schema.sql 与代码里各写
    一遍」的漂移。这是本项目第 9 处静默失效防线。
    """
    missing = [
        name
        for name in ("period_sets", "set_slots")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is None
    ]
    if missing:
        init_db(conn)


def load_period_sets(conn: sqlite3.Connection, org_id: str) -> dict[str, dict]:
    """读本组织的作息方案 → {set_id: {"name": 方案名, "cfg": SlotConfig}}。

    🔴 set_slots 语义（写死在这里，别处不许另解释）：
      · 展开时**只认 enabled = 1** 的行（SlotConfig.day_mask 已按 enabled 过滤）；
      · enabled = 0 与**缺行完全等价**，都表示「该方案这一天没有这一节」；
      · 方案存在但 set_slots 零行 = 该方案全天无课（合法，不抛错，靠 advisory 提示）。
    """
    out: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT id, name FROM period_sets WHERE org_id = ?", (org_id,)
    ).fetchall():
        out[r["id"]] = {"name": r["name"], "cfg": SlotConfig(org_id=org_id)}
    if not out:
        return out
    for r in conn.execute(
        "SELECT s.set_id, s.day_of_week, s.idx, s.start_time, s.end_time, s.enabled"
        " FROM set_slots s JOIN period_sets p ON p.id = s.set_id"
        " WHERE p.org_id = ?",
        (org_id,),
    ).fetchall():
        entry = out.get(r["set_id"])
        if entry is None:  # 理论不可达（JOIN 已限定），防御性跳过
            continue
        entry["cfg"].slots[(r["day_of_week"], r["idx"])] = {
            "name": None,  # set_slots 无 name 列，节次名沿用默认方案口径
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "enabled": bool(r["enabled"]),
        }
    return out


def effective_slot_config(
    exc: "DayExceptions", cfg: SlotConfig, iso: str
) -> SlotConfig:
    """该日**实际生效**的节次配置：命中 special 返回方案配置，否则返回默认 cfg。

    🔴 这是「作息方案」的唯一事实来源：expand_periodic（显示/占用）与测试都读
    它，不许别处再自己判断一次 set_id（只改一处 = 占用显示空着、排课按有课）。

    注意与 as_of 的分工：`as_of` 决定「按周几的课表」（dow），`special` 决定
    「用哪套作息」（本函数）。两者可同日共存，互不替代。
    """
    set_id = exc.specials.get(iso)
    if set_id is None:
        return cfg
    set_cfg = exc.special_cfgs.get(set_id)
    if set_cfg is None:
        # DayExceptions 被手工构造却没预载 special_cfgs：宁可报错，不悄悄用默认作息
        raise ValueError(
            f"日期 {iso} 绑定了作息方案 {set_id!r}，但 special_cfgs 里没有它——"
            f"请用 load_exceptions() 构造 DayExceptions（拒绝静默回退默认作息）"
        )
    return set_cfg


def load_exceptions(conn: sqlite3.Connection, org_id: str) -> DayExceptions:
    """同时读两张例外表（P0-2，队长 2026-10-12 拍板；B9 扩展第三类语义）：

    - calendar_exceptions：只服务 kind='holiday'（「放假」是日历事实，老逻辑
      保留不动）。旧表里的 makeup 行不再有任何效果——修前它本来也只是
      惰性（周六照旧查周六槽位 → 掩码 0），调休一律改走新表。
    - schedule_exceptions, scope='day' 且 mode='as_of'：调休（P0-2）。
    - schedule_exceptions, scope='schedule' 且 mode='cancel'/'move'：B9 的
      「某条周期日程的某一天」临时取消/改点。
    - schedule_exceptions, scope='day' 且 mode='special'：B-1 换作息方案，
      该日改按 period_sets/set_slots 那套节次配置展开。

    🔴 队长定死的语义：mode='as_of' 但 as_of_dow 为 NULL / 越界 / 同日多条
    dow 互相矛盾 → 抛 ValueError，绝不静默跳过（P0-3 教训：假阴性——
    系统说没冲突、实际有——直接毁掉本系统的全部价值主张）。

    🔴 B-1 同口径：mode='special' 但 set_id 为 NULL / 指向不存在的方案 /
    同日多条互相矛盾 → 同样抛 ValueError。「方案不存在就当默认作息用」
    是典型静默失效（卡面必验 5 明确要求报错）。
    """
    ensure_schedule_exceptions(conn)
    ensure_period_set_tables(conn)
    exc = DayExceptions()
    for r in conn.execute(
        "SELECT date, kind FROM calendar_exceptions WHERE org_id = ?", (org_id,)
    ).fetchall():
        if r["kind"] == "holiday":
            exc.holidays.add(r["date"])
    for r in conn.execute(
        "SELECT id, date, as_of_dow FROM schedule_exceptions"
        " WHERE org_id = ? AND scope = 'day' AND mode = 'as_of'",
        (org_id,),
    ).fetchall():
        if r["as_of_dow"] is None:
            raise ValueError(
                f"schedule_exceptions(id={r['id']}, date={r['date']}): "
                f"mode='as_of' 但 as_of_dow 为 NULL——调休日必须指明按周几的课表上课，"
                f"拒绝静默跳过（P0-3 语义）"
            )
        if not 1 <= r["as_of_dow"] <= 7:
            raise ValueError(
                f"schedule_exceptions(id={r['id']}, date={r['date']}): "
                f"as_of_dow 必须在 1..7，实际为 {r['as_of_dow']}"
            )
        prev = exc.as_of.get(r["date"])
        if prev is not None and prev != r["as_of_dow"]:
            raise ValueError(
                f"schedule_exceptions(date={r['date']}): 存在多条 as_of 且 dow 互相矛盾"
                f"（{prev} vs {r['as_of_dow']}）——请先修正数据"
            )
        exc.as_of[r["date"]] = r["as_of_dow"]
    for r in conn.execute(
        "SELECT id, target_id, date, mode, new_mask FROM schedule_exceptions"
        " WHERE org_id = ? AND scope = 'schedule' AND mode IN ('cancel', 'move')",
        (org_id,),
    ).fetchall():
        if r["target_id"] is None:
            raise ValueError(
                f"schedule_exceptions(id={r['id']}): scope='schedule' 但 target_id 为"
                f" NULL——取消/改点必须指明是哪条日程（P0-3 语义）"
            )
        key = (r["target_id"], r["date"])
        if r["mode"] == "cancel":
            exc.cancels.add(key)
        else:
            if r["new_mask"] is None:
                raise ValueError(
                    f"schedule_exceptions(id={r['id']}): scope='schedule' 且 mode='move'"
                    f" 但 new_mask 为 NULL——改点必须给出新的槽位掩码（P0-3 语义）"
                )
            # DB 存 hex 文本（与 slot_bitmap 同口径），这里转成 int 掩码；
            # 非法 hex 会让 int() 抛 ValueError——配置错误宁可报错（P0-3）。
            try:
                new_mask = hex_to_mask(r["new_mask"])
            except ValueError as exc_err:
                raise ValueError(
                    f"schedule_exceptions(id={r['id']}): new_mask={r['new_mask']!r} "
                    f"不是合法十六进制——改点目标掩码无法解析（P0-3 语义）"
                ) from exc_err
            prev = exc.moves.get(key)
            if prev is not None and prev != new_mask:
                raise ValueError(
                    f"schedule_exceptions(target_id={r['target_id']}, "
                    f"date={r['date']}): 存在多条 move 且 new_mask 互相矛盾"
                    f"（{prev:#x} vs {new_mask:#x}）——请先修正数据"
                )
            exc.moves[key] = new_mask
    # B-1 换作息方案：mode='special' → 该日绑定的 period_sets.id
    # 刻意**不限 scope**：scope='schedule'+special 是没实现的组合，若过滤掉就成了
    # 静默忽略（与「缺节不静默」同一条纪律），所以取出来显式报错。
    special_rows = conn.execute(
        "SELECT id, scope, date, set_id FROM schedule_exceptions"
        " WHERE org_id = ? AND mode = 'special'",
        (org_id,),
    ).fetchall()
    if special_rows:
        sets = load_period_sets(conn, org_id)
        for r in special_rows:
            if r["scope"] != "day":
                raise ValueError(
                    f"schedule_exceptions(id={r['id']}, date={r['date']}): "
                    f"mode='special' 只支持 scope='day'（整天换作息），"
                    f"实际 scope={r['scope']!r}——拒绝静默忽略这条例外"
                )
            if r["set_id"] is None:
                raise ValueError(
                    f"schedule_exceptions(id={r['id']}, date={r['date']}): "
                    f"mode='special' 但 set_id 为 NULL——换作息必须指明用哪套方案，"
                    f"拒绝静默按默认作息展开（P0-3 语义）"
                )
            if r["set_id"] not in sets:
                raise ValueError(
                    f"schedule_exceptions(id={r['id']}, date={r['date']}): "
                    f"set_id={r['set_id']!r} 在本组织的 period_sets 里不存在——"
                    f"拒绝静默回退默认作息（那会让课表与配置不一致且无人知晓）"
                )
            prev_set = exc.specials.get(r["date"])
            if prev_set is not None and prev_set != r["set_id"]:
                raise ValueError(
                    f"schedule_exceptions(date={r['date']}): 存在多条 special 且 set_id"
                    f" 互相矛盾（{prev_set} vs {r['set_id']}）——请先修正数据"
                )
            exc.specials[r["date"]] = r["set_id"]
        exc.special_cfgs = {sid: v["cfg"] for sid, v in sets.items()}
        exc.special_names = {sid: v["name"] for sid, v in sets.items()}
    return exc


def _orphan_schedules(
    conn: sqlite3.Connection, org_id: str
) -> list[sqlite3.Row]:
    """P0-3：找出 schedules 有主表行、但两张扩展表都缺行的孤儿。

    v_schedule_full 是 INNER JOIN（schema.sql:184/190，CREATE VIEW IF NOT
    EXISTS 对已存在的 data/*.db 不会重建，改视图定义对存量库无效），
    所以孤儿行的正确查法是读路径上一次 LEFT JOIN 反查——不改 schema，
    存量库立即可用。
    """
    return conn.execute(
        "SELECT s.id, s.kind, s.title FROM schedules s"
        " LEFT JOIN schedule_periodic p ON p.schedule_id = s.id"
        " LEFT JOIN schedule_once o ON o.schedule_id = s.id"
        " WHERE s.org_id = ? AND p.schedule_id IS NULL AND o.schedule_id IS NULL",
        (org_id,),
    ).fetchall()


def apply_schedule_exceptions(
    days: dict[date, int],
    schedule_id: str,
    exc: DayExceptions,
    *,
    cfg: SlotConfig | None = None,
    issues: list[ExpansionIssue] | None = None,
) -> dict[date, int]:
    """B9：把 scope='schedule' 的 cancel / move 例外应用到已展开的 {日期: 掩码}。

    在 build_occupancy 循环内、expand_* 之后调用（拿不到 days 就没法逐日
    定位）。语义：
      cancel → 该日程这一天直接消失（占用撤掉，相关冲突立即消失）；
      move   → 该日程这一天改到新槽位（当天掩码被 new_mask 整体覆盖）。

    为什么**不会误伤其他周**（Q2 的结构性保证）：例外键是
    (schedule_id, ISO 日期)，逐日查/逐日改。同一 dow 的其他日期键不同，
    不会被连带修改；跨 term 边界（如第 2 周取消、第 3 周照常）同样安全。
    删掉例外行后 days 恢复 expand_* 的原始结果——完全还原。

    不抛异常、不静默跳过（P0-3）：
      - move 的新掩码与当天启用槽位无交集（传入 cfg 时检查）→ 这一天等于
        把课改没了，冲突检测必然漏它，记 ISSUE_MOVE_NO_SLOT 信号上抛；
      - 例外指向的 (schedule_id, ISO) 不在 days 里（那天本来就不展开，
        如周末/假期/term 外）→ 无操作、不报错：例外比展开结果窄是合法的
        （老师可以预先取消一节还没排的课），不构成数据缺陷。
    """
    if not exc.cancels and not exc.moves:
        return days
    out: dict[date, int] = {}
    blind: list[str] = []
    for d, mask in days.items():
        iso = d.isoformat()
        key = (schedule_id, iso)
        if key in exc.cancels:
            continue
        moved = exc.moves.get(key)
        if moved is not None:
            mask = moved
            if cfg is not None and not (mask & cfg.day_mask(d.isoweekday())):
                blind.append(iso)
        out[d] = mask
    if blind and issues is not None:
        issues.append(
            ExpansionIssue(code=ISSUE_MOVE_NO_SLOT, dates=sorted(blind), count=len(blind))
        )
    return out


def build_occupancy(
    conn: sqlite3.Connection,
    org_id: str,
    date_from: date,
    date_to: date,
    *,
    cfg: SlotConfig | None = None,
    expand_groups: bool = True,
    issues: list[ExpansionIssue] | None = None,
) -> list[OccupancyEntry]:
    """把某组织在给定日期范围内的所有日程展开成占用条目。

    参与者（target/teacher/participant）与资源（教室）都算"被占用对象"，
    因为教师和教室同样会撞课——这正是把资源纳入统一模型的意义。

    P0-3：issues 传入一个 list 时，展开过程中的「静默跳过」事实会以
    ExpansionIssue 记入（孤儿行 / 无 term 日期 / 不落槽位活动）。
    不传（含 solver/m2 旧调用点）= 调用方主动放弃信号，行为与修前一致；
    scan() 会传并挂到 ConflictReport.issues。
    """
    cfg = cfg or load_slot_config(conn, org_id)
    terms = load_terms(conn, org_id)
    exceptions = load_exceptions(conn, org_id)
    out_issues: list[ExpansionIssue] = [] if issues is None else issues

    # P0-3 静默点 4：孤儿行检测（先做，视图行里不可能出现孤儿，天然去重）。
    for r in _orphan_schedules(conn, org_id):
        out_issues.append(
            ExpansionIssue(
                code=ISSUE_ORPHAN_SCHEDULE,
                schedule_id=r["id"],
                schedule_kind=r["kind"],
                schedule_title=r["title"],
                count=1,
            )
        )

    entries: list[OccupancyEntry] = []
    rows = conn.execute(
        "SELECT * FROM v_schedule_full WHERE org_id = ?", (org_id,)
    ).fetchall()
    for r in rows:
        local: list[ExpansionIssue] = []
        if r["kind"] == "periodic":
            days = expand_periodic(
                r["week_parity"],
                hex_to_mask(r["slot_bitmap"]),
                r["valid_from"],
                r["valid_to"],
                cfg,
                terms,
                date_from,
                date_to,
                exceptions,
                issues=local,
            )
        else:
            days = expand_once(r["start_at"], r["end_at"], cfg, issues=local)

        # B9：scope='schedule' 的 cancel/move 在展开之后逐日生效
        # （cancel 撤掉当天占用 → 冲突立即消失；move 覆盖当天掩码）。
        # 放在这里而不是塞进 expand_*：不改 expand_* 签名（卡面铁律），
        # 且一次性活动同样受益（接口层仍按卡面只放行周期日程）。
        days = apply_schedule_exceptions(
            days, r["id"], exceptions, cfg=cfg, issues=local
        )

        # P0-3：expand_* 只知道日期，日程上下文在这里补齐并聚合
        # （同一日程的同类问题合成一条 issue，防止整学期扫描刷屏）。
        for iss in local:
            iss.schedule_id = r["id"]
            iss.schedule_kind = r["kind"]
            iss.schedule_title = r["title"]
            out_issues.append(iss)

        if not days:
            continue

        links: list[tuple[str, str]] = []
        for p in conn.execute(
            "SELECT object_id, role FROM schedule_participants WHERE schedule_id = ?",
            (r["id"],),
        ).fetchall():
            links.append((p["object_id"], p["role"]))
        for res in conn.execute(
            "SELECT resource_id FROM schedule_resources WHERE schedule_id = ?", (r["id"],)
        ).fetchall():
            links.append((res["resource_id"], "resource"))

        # 组作为参与者时：既保留组自身（两个活动都占用同一班级能一次命中），
        # 也展开为其下所有成员（这样"给高一(3)班排课"会占用该班每个学生的
        # 时间，学生被单独安排时会撞上本班的课——这是本系统的核心价值之一）。
        #
        # ⚠️ 性能权衡：展开成员会让条目数放大约一个班的人数倍。
        #    演示/试用规模（几百人）毫无压力；全校 1500 人扫描一整学期时，
        #    可传 expand_groups=False 只做组级与教师/教室级检测。
        expanded: list[tuple[str, str]] = []
        for oid, role in links:
            kind_row = conn.execute(
                "SELECT kind FROM objects WHERE id = ?", (oid,)
            ).fetchone()
            is_group = bool(kind_row and kind_row["kind"] == "group")
            expanded.append((oid, role))
            if is_group and expand_groups:
                for member in conn.execute(
                    "SELECT descendant_id FROM object_edges WHERE ancestor_id = ? AND depth > 0",
                    (oid,),
                ).fetchall():
                    expanded.append((member["descendant_id"], role))

        for d, mask in days.items():
            if not (date_from <= d <= date_to):
                continue
            for oid, role in expanded:
                entries.append(OccupancyEntry(r["id"], oid, role, d, mask))
    return entries


def find_conflicts(entries: list[OccupancyEntry]) -> list[Conflict]:
    """冲突 = 同一对象同一天，两条不同日程的掩码按位与 ≠ 0。"""
    buckets: dict[tuple[str, date], list[OccupancyEntry]] = {}
    for e in entries:
        buckets.setdefault((e.object_id, e.date), []).append(e)

    conflicts: list[Conflict] = []
    for (_oid, _d), group in buckets.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.schedule_id == b.schedule_id:
                    continue
                inter = a.mask & b.mask
                if inter:
                    conflicts.append(
                        Conflict(
                            a.schedule_id,
                            b.schedule_id,
                            a.object_id,
                            a.role,
                            a.date,
                            inter,
                        )
                    )
    return conflicts


def busy_mask(
    entries: list[OccupancyEntry], object_ids: list[str], d: date
) -> int:
    """某一天，若干对象的合并占用掩码。"""
    ids = set(object_ids)
    m = 0
    for e in entries:
        if e.object_id in ids and e.date == d:
            m |= e.mask
    return m


def find_free_slots(
    entries: list[OccupancyEntry],
    object_ids: list[str],
    d: date,
    cfg: SlotConfig,
) -> int:
    """找空闲：当天所有启用槽位 减去 已占用。返回掩码（0 表示排满）。"""
    return cfg.day_mask(d.isoweekday()) & ~busy_mask(entries, object_ids, d)


def load_of(
    entries: list[OccupancyEntry],
    object_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[date, int]:
    """某对象的负载：{日期: 占用节数}。用于"教师日均课时"等统计。"""
    out: dict[date, int] = {}
    for e in entries:
        if e.object_id != object_id:
            continue
        if date_from and e.date < date_from:
            continue
        if date_to and e.date > date_to:
            continue
        out[e.date] = out.get(e.date, 0) + popcount(e.mask)
    return out


def total_load(entries: list[OccupancyEntry], object_id: str) -> int:
    return sum(load_of(entries, object_id).values())


# ---------------------------------------------------------------------------
# 汇总报告（供 UI 与 AI 消解建议共用）
# ---------------------------------------------------------------------------


@dataclass
class ConflictReport:
    date_from: date
    date_to: date
    conflicts: list[Conflict] = field(default_factory=list)
    # P0-3：展开过程中的静默跳过信号（孤儿行 / 无 term / 不落槽位活动）。
    # 空列表 = 无此类数据缺陷。下游 /api/conflicts 会以 warnings 字段透出。
    issues: list[ExpansionIssue] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.conflicts)

    def by_object(self) -> dict[str, list[Conflict]]:
        out: dict[str, list[Conflict]] = {}
        for c in self.conflicts:
            out.setdefault(c.object_id, []).append(c)
        return out


def scan(
    conn: sqlite3.Connection,
    org_id: str,
    date_from: date,
    date_to: date,
    *,
    cfg: SlotConfig | None = None,
) -> ConflictReport:
    """一次性完成：展开占用 → 检测冲突 → 出报告（P0-3 起含静默跳过信号）。"""
    issues: list[ExpansionIssue] = []
    entries = build_occupancy(conn, org_id, date_from, date_to, cfg=cfg, issues=issues)
    return ConflictReport(date_from, date_to, find_conflicts(entries), issues)


def occupancy_map(
    entries: list[OccupancyEntry],
) -> dict[tuple[str, date], int]:
    """把占用条目压成 {(对象, 日期): 掩码}，供求解器直接当占用矩阵用。"""
    out: dict[tuple[str, date], int] = {}
    for e in entries:
        k = (e.object_id, e.date)
        out[k] = out.get(k, 0) | e.mask
    return out
