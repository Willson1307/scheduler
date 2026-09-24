"""节次槽位与位图：把「周几第几节」编码成一个整数掩码。

设计意图（这是整个系统最关键的一个抽象，建议队员先读这个文件）：

1. **槽位不是写死在代码里的**。
   真正的真相在 `period_slots` 表（数据库里可任意增删、留余量），
   本文件只是"读取配置 + 做位运算"的工具层。改作息不用改代码。

2. **位序固定，与配置解耦**。
   位号 = (周几 - 1) * 16 + (节次 - 1)，其中 16 是每天最多节数的**上界**。
   也就是说：今天配 10 节、明天改成 12 节，已有掩码的含义不会错位。
   这是有意的设计——如果位序跟着"当天的实际节数"走，改一次配置历史数据全乱。

3. **位图是"加速层"，不是"真相层"**。
   真相是 period_slots 配置 + 周期规则 + 一次性事件的起止时刻；
   位图只是检测时展开出来的中间表示，用于让冲突判断变成一次按位与。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# 每天最多节数的上界。留余量是有意的：位序固定，配置怎么改都不影响历史掩码
MAX_PERIODS_PER_DAY = 16
WEEK_MASK_BITS = 7 * MAX_PERIODS_PER_DAY  # 112 位，SQLite 整数放不下 → 存十六进制文本


def bit_index(day_of_week: int, period_index: int) -> int:
    """周几(1-7) + 第几节(1-based) → 位号(0-based)。"""
    if not 1 <= day_of_week <= 7:
        raise ValueError(f"day_of_week 必须在 1..7，收到 {day_of_week}")
    if not 1 <= period_index <= MAX_PERIODS_PER_DAY:
        raise ValueError(f"period_index 必须在 1..{MAX_PERIODS_PER_DAY}，收到 {period_index}")
    return (day_of_week - 1) * MAX_PERIODS_PER_DAY + (period_index - 1)


def bit(day_of_week: int, period_index: int) -> int:
    """单个槽位的掩码值。"""
    return 1 << bit_index(day_of_week, period_index)


def decode_bit_index(pos: int) -> tuple[int, int]:
    """位号 → (周几, 第几节)。"""
    return pos // MAX_PERIODS_PER_DAY + 1, pos % MAX_PERIODS_PER_DAY + 1


def mask_to_hex(mask: int) -> str:
    return format(mask, "x")


def hex_to_mask(value: str) -> int:
    return int(value, 16) if value else 0


def popcount(mask: int) -> int:
    """掩码中置位的个数 = 占用的节数（Python 3.8+ 用 int.bit_count）。"""
    return mask.bit_count()


def decode_mask(mask: int) -> list[tuple[int, int]]:
    """掩码 → [(周几, 第几节), ...]，按时间顺序。"""
    out = []
    pos = 0
    m = mask
    while m:
        if m & 1:
            out.append(decode_bit_index(pos))
        m >>= 1
        pos += 1
    return out


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@dataclass
class SlotConfig:
    """某个组织的节次配置（从 period_slots 表读出来后的内存表示）。"""

    org_id: str
    slots: dict[tuple[int, int], dict] = field(default_factory=dict)

    def get(self, day_of_week: int, period_index: int) -> dict | None:
        return self.slots.get((day_of_week, period_index))

    def enabled_slots(self, day_of_week: int) -> list[tuple[int, int]]:
        return sorted(
            (dow, idx)
            for (dow, idx), s in self.slots.items()
            if dow == day_of_week and s["enabled"]
        )

    def all_enabled_mask(self) -> int:
        """一周内所有启用槽位的掩码（用于"满占用"参照）。"""
        m = 0
        for (dow, idx), s in self.slots.items():
            if s["enabled"]:
                m |= bit(dow, idx)
        return m

    def day_mask(self, day_of_week: int) -> int:
        m = 0
        for dow, idx in self.enabled_slots(day_of_week):
            m |= bit(dow, idx)
        return m

    def mask_for_range(
        self, day_of_week: int, start_hhmm: str, end_hhmm: str
    ) -> tuple[int, bool]:
        """把一段连续时刻映射成当天的槽位掩码。

        返回 (掩码, 是否完全对齐整节)。
        中学大部分安排是整节的；遇到午休开会、跨半节这类非整节安排，
        这里会返回 is_aligned=False，调用方可以退化为区间重叠判断。
        映射规则：与区间**有重叠**的槽位都算被占用（保守，避免漏判冲突）。
        """
        s, e = _minutes(start_hhmm), _minutes(end_hhmm)
        mask = 0
        aligned = True
        touched = False
        for dow, idx in self.enabled_slots(day_of_week):
            slot = self.slots[(dow, idx)]
            ss, se = _minutes(slot["start_time"]), _minutes(slot["end_time"])
            if s < se and ss < e:  # 区间重叠
                touched = True
                mask |= bit(dow, idx)
                if not (ss <= s and e <= se):
                    aligned = False
        if not touched:
            aligned = False
        return mask, aligned


def load_slot_config(conn: sqlite3.Connection, org_id: str) -> SlotConfig:
    """从数据库读取某组织的节次配置。"""
    rows = conn.execute(
        "SELECT day_of_week, idx, name, start_time, end_time, enabled "
        "FROM period_slots WHERE org_id = ?",
        (org_id,),
    ).fetchall()
    cfg = SlotConfig(org_id=org_id)
    for r in rows:
        cfg.slots[(r["day_of_week"], r["idx"])] = {
            "name": r["name"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "enabled": bool(r["enabled"]),
        }
    return cfg


def setup_default_slots(
    conn: sqlite3.Connection,
    org_id: str,
    periods_per_day: int = 10,
    days: int = 7,
    start_times: list[str] | None = None,
    duration_min: int = 45,
    break_min: int = 10,
) -> SlotConfig:
    """为组织写入一套默认节次（可随后在后台改）。

    默认按 45 分钟一节、课间 10 分钟连续生成，8:00 开始。
    这只是**初始配置**，不是硬编码——真正生效的是 period_slots 表里的内容。
    """
    if start_times is None:
        start_times = []
        t = 8 * 60
        for _ in range(periods_per_day):
            start_times.append(f"{t // 60:02d}:{t % 60:02d}")
            t += duration_min + break_min
    for dow in range(1, days + 1):
        for idx in range(1, periods_per_day + 1):
            st = start_times[idx - 1]
            sh, sm = st.split(":")
            end = int(sh) * 60 + int(sm) + duration_min
            conn.execute(
                "INSERT OR IGNORE INTO period_slots"
                "(id, org_id, day_of_week, idx, name, start_time, end_time, enabled)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"slot_{org_id}_{dow}_{idx}",
                    org_id,
                    dow,
                    idx,
                    f"第{idx}节",
                    st,
                    f"{end // 60:02d}:{end % 60:02d}",
                    1 if dow <= 5 else 0,  # 周六周日默认不排课
                ),
            )
    conn.commit()
    return load_slot_config(conn, org_id)
