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
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from . import auth
from . import b3_kernels as K
from . import config
from .http_api import ApiError

from scheduler.occupancy import build_occupancy, occupancy_map
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


ROUTES = {
    ("GET", "/api/schedules"): list_schedules,
}
