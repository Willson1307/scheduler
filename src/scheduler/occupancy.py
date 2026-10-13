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
from .slots import SlotConfig, bit, hex_to_mask, load_slot_config, popcount


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


@dataclass
class DayExceptions:
    """日期级例外的统一视图（load_exceptions 的返回值，P0-2 起）。

    两类语义，分属两张表：
      holidays = 放假（calendar_exceptions, kind='holiday'）：该日停课；
      as_of    = 调休（schedule_exceptions, mode='as_of'）：该日按 as_of_dow
                 周几的课表上课，键为 ISO 日期、值 1..7（与
                 period_slots.day_of_week 同口径，省一次转换）。
    """

    holidays: set[str] = field(default_factory=set)
    as_of: dict[str, int] = field(default_factory=dict)

    def is_holiday(self, iso: str) -> bool:
        return iso in self.holidays

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
) -> dict[date, int]:
    """周期日程 → {日期: 当天掩码}。

    exceptions 接受 load_exceptions() 返回的 DayExceptions（放假 + 调休统一
    视图），也兼容旧格式 {ISO: kind} 字典。调休日（mode='as_of'）改按
    as_of_dow 周几的槽位展开——位号公式不变，只是换了"查哪一天的课表"。
    """
    exc = DayExceptions.coerce(exceptions)
    vf, vt = _parse_date(valid_from), _parse_date(valid_to)
    out: dict[date, int] = {}
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
                    day_mask = slot_bitmap & cfg.day_mask(dow)
                    if day_mask:
                        out[d] = day_mask
        d += timedelta(days=1)
    return out


def expand_once(
    start_at: str, end_at: str, cfg: SlotConfig
) -> dict[date, int]:
    """一次性活动 → {日期: 当天掩码}。跨天会自然拆成多天。"""
    s = datetime.strptime(start_at, "%Y-%m-%dT%H:%M")
    e = datetime.strptime(end_at, "%Y-%m-%dT%H:%M")
    out: dict[date, int] = {}
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
        d += timedelta(days=1)
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


def load_exceptions(conn: sqlite3.Connection, org_id: str) -> DayExceptions:
    """同时读两张例外表（P0-2，队长 2026-10-12 拍板）：

    - calendar_exceptions：只服务 kind='holiday'（「放假」是日历事实，老逻辑
      保留不动）。旧表里的 makeup 行不再有任何效果——修前它本来也只是
      惰性（周六照旧查周六槽位 → 掩码 0），调休一律改走新表。
    - schedule_exceptions：本卡只消费 scope='day' 且 mode='as_of' 的行；
      其他 mode（cancel/move/special）与 scope='schedule' 留给 B9，本卡忽略。

    🔴 队长定死的语义：mode='as_of' 但 as_of_dow 为 NULL / 越界 / 同日多条
    dow 互相矛盾 → 抛 ValueError，绝不静默跳过（P0-3 教训：假阴性——
    系统说没冲突、实际有——直接毁掉本系统的全部价值主张）。
    """
    ensure_schedule_exceptions(conn)
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
    return exc


def build_occupancy(
    conn: sqlite3.Connection,
    org_id: str,
    date_from: date,
    date_to: date,
    *,
    cfg: SlotConfig | None = None,
    expand_groups: bool = True,
) -> list[OccupancyEntry]:
    """把某组织在给定日期范围内的所有日程展开成占用条目。

    参与者（target/teacher/participant）与资源（教室）都算"被占用对象"，
    因为教师和教室同样会撞课——这正是把资源纳入统一模型的意义。
    """
    cfg = cfg or load_slot_config(conn, org_id)
    terms = load_terms(conn, org_id)
    exceptions = load_exceptions(conn, org_id)

    entries: list[OccupancyEntry] = []
    rows = conn.execute(
        "SELECT * FROM v_schedule_full WHERE org_id = ?", (org_id,)
    ).fetchall()
    for r in rows:
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
            )
        else:
            days = expand_once(r["start_at"], r["end_at"], cfg)

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
    """一次性完成：展开占用 → 检测冲突 → 出报告。"""
    entries = build_occupancy(conn, org_id, date_from, date_to, cfg=cfg)
    return ConflictReport(date_from, date_to, find_conflicts(entries))


def occupancy_map(
    entries: list[OccupancyEntry],
) -> dict[tuple[str, date], int]:
    """把占用条目压成 {(对象, 日期): 掩码}，供求解器直接当占用矩阵用。"""
    out: dict[tuple[str, date], int] = {}
    for e in entries:
        k = (e.object_id, e.date)
        out[k] = out.get(k, 0) | e.mask
    return out
