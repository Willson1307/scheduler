"""r18onN CSV 批量导入内核：解析 + 校验 + 落库（纯函数，不注册路由）。

文件名铁律（AGENTS §3.1）：本文件**不导出 ROUTES**，所以绝不能叫 api_*.py，
否则会被 routes.py 的 glob 扫到、按「空 ROUTES」拒载，报错还指不到真因。

用途与设计意图（答辩口径）：
  1. 老师手上的课表是 Excel 导出的 CSV。没有导入，现场只能演「种子数据造的课表」，
     社会价值维度（25%）站不住。本文件把「一段 CSV 文本 → 若干条周期日程」
     做成**先全量校验、后一次性落库**的两段式：任何一行错都不写库，
     错误必须带行号，一条也不许静默跳过（本项目最贵的 bug 类型就是静默失效）。
  2. 名字唯一性是本卡最容易出静默错的地方：objects.kind 只有
     group / user / resource 三种（教室是 resource，没有 room）；
     学生和教师都是 user，按名字查可能撞名——同名 >1 条必须 400 列出全部候选，
     🔴 绝对不许取第一条（取第一条 = 把别人的课排到别人头上，且不报错）。
  3. 权限在接口层（api_import.py）判，本文件只管「给定了 org_id 之后怎么导」。
     仅 own 档可导入的理由：导入是批量直接写库，允许 propose 档走这条路
     等于给审批流（B8）开后门。
  4. 落库走领域层 create_periodic（schedules.py:31），一行一条，
     slot_bitmap 用 slots.bit(dow, idx) 的或——与手工录入同一条路径，
     保证「导入的课表」和「手工录的课表」在存储层逐字段一致。
  5. 隐私红线（AGENTS §10 红线 2）：不落日志、不回传 CSV 原文、不进埋点。
     错误消息只引用单个字段值（如撞名候选），绝不整行/整文件回显。

只用 Python 标准库。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .http_api import ApiError

from scheduler.authz import effective_level
from scheduler.occupancy import scan
from scheduler.schedules import NewSchedule, create_periodic
from scheduler.slots import bit, load_slot_config

# ---------------------------------------------------------------------------
# 契约常量（卡面 r18onN 写死，不得自改口径）
# ---------------------------------------------------------------------------

#: 必填列（表头缺任一 → 400）
REQUIRED_COLUMNS = ("班级", "教师", "课程", "星期", "开始节次")

#: 可选列及其缺省值语义（见 _map_row）
OPTIONAL_COLUMNS = ("持续节次", "教室", "生效起始", "生效结束", "周次")

#: 表头允许出现的全部列。🔴 出现集合之外的列 → 400 并回显列名（不静默忽略）
ALL_COLUMNS = frozenset(REQUIRED_COLUMNS + OPTIONAL_COLUMNS)

#: 数据行数上限（防一次误传整学期+整校把服务拖死）
MAX_ROWS = 2000

#: 冲突统计窗口上限（天）。跳度超过则截断并给 advisory，防整学期展开拖慢。
MAX_CONFLICT_WINDOW_DAYS = 62

#: 星期列的两种合法写法：数字 1-7 / 周一..周日
_DOW_NAMES = {"周一": 1, "周二": 2, "周三": 3, "周四": 4, "周五": 5, "周六": 6, "周日": 7}

#: 周次列 → schedule_periodic.week_parity（0=每周 1=单周 2=双周）
_WEEK_PARITY = {"每周": 0, "单周": 1, "双周": 2}

#: 每天 16 节上界（slots.MAX_PERIODS_PER_DAY 同口径，卡面：开始节次 1–16）
_MAX_PERIOD = 16


@dataclass
class MappedRow:
    """一行 CSV 校验通过后的映射结果（重复行检测也用它）。"""

    line: int
    title: str
    class_id: str
    teacher_id: str
    room_id: str | None
    dow: int
    start: int
    duration: int
    parity: int
    valid_from: str
    valid_to: str

    @property
    def slot_bitmap(self) -> int:
        """slots.bit(dow, idx) 的或（卡面写死的落库口径）。"""
        out = 0
        for idx in range(self.start, self.start + self.duration):
            out |= bit(self.dow, idx)
        return out

    def dedup_key(self) -> tuple:
        """重复行判定键：映射后 班级/教师/课程/星期/节次/周次/区间 全相同。"""
        return (
            self.class_id,
            self.teacher_id,
            self.title,
            self.dow,
            self.start,
            self.duration,
            self.parity,
            self.valid_from,
            self.valid_to,
            self.room_id,
        )


# ---------------------------------------------------------------------------
# 1. 请求体 → CSV 文本（编码探测）
# ---------------------------------------------------------------------------


def extract_request(body: bytes) -> tuple[str, str, bool | None]:
    """解析 POST /api/import/csv 的请求体。

    支持两种形态（第一种是卡面契约，第二种是 GB18030 兜底，见下）：

    A. JSON：{"csv": "<全文>", "dry_run": true|false}
       → 返回 (csv 文本, "utf-8-sig", dry_run)。
       dry_run 缺省 true（安全默认：不显式说写库就只预检）。
    B. 原生 CSV 字节：请求体不是 JSON，而是 CSV 文件原文
       （真实场景：老师把 Excel 导出的 .csv 文件直接 POST 上来）。
       → 先试 utf-8-sig，失败退 gb18030（Excel 中文默认编码），
         返回 (文本, encoding, None)——dry_run 由接口层从 query 解析，
         缺省同为 true（🔴 写库必须显式要求，绝不默认写）。

    为什么必须有 B 形态：JSON 的字符串是 Unicode 文本，编码探测无从谈起；
    GB18030 字节只有作为**原始请求体**才能被如实识别（json 层会先按 UTF-8
    解码直接炸掉）。B 形态不改变 A 的契约（A 照常工作），只是多收一种真实输入。

    解不出任何编码 → ApiError(400)。
    """
    if not body or not body.strip():
        raise ApiError(400, '缺少请求体：{"csv": "<CSV 全文>", "dry_run": true|false}')

    try:
        text = body.decode("utf-8-sig")
        utf8_ok = True
    except UnicodeDecodeError:
        utf8_ok = False

    if utf8_ok:
        try:
            data = json.loads(text)
        except ValueError:
            # 不是 JSON → 当作原生 UTF-8 CSV 文本
            return text, "utf-8-sig", None
        if not isinstance(data, dict):
            raise ApiError(400, "JSON 请求体必须是对象（object），含 csv 与 dry_run 字段")
        if "csv" not in data:
            raise ApiError(400, "缺少 csv 字段（值应为 CSV 全文字符串）")
        csv_text = data["csv"]
        # 🔴 只拒空串；纯空白（如 "\n\n  \n"）放进 parse 层报「表头为空」
        # （行号=1），与 CSV 形态的报错口径一致
        if not isinstance(csv_text, str) or csv_text == "":
            raise ApiError(400, "csv 字段必须是非空字符串")
        dry_run = data.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise ApiError(400, "dry_run 必须是布尔值 true/false")
        return csv_text.lstrip("\ufeff"), "utf-8-sig", dry_run

    # UTF-8 解不开 → GB18030（Excel 中文默认）
    try:
        text = body.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise ApiError(400, "CSV 编码既不是 UTF-8 也不是 GB18030，无法解析") from exc
    return text, "gb18030", None


def parse_dry_run_query(query: dict) -> bool:
    """原生 CSV 模式下从 query 解析 dry_run（缺省 true：写库必须显式要求）。"""
    raw = (query.get("dry_run") or [""])[0].strip().lower()
    if raw in ("false", "0", "no"):
        return False
    return True


# ---------------------------------------------------------------------------
# 2. CSV 文本 → 表头 + 数据行
# ---------------------------------------------------------------------------


def parse_csv(text: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """CSV 文本 → (表头列名列表, [(物理行号, 单元格列表), ...])。

    - 表头 = 第一个非空行；无任何非空行 → ([], [])（由上层报 400）。
    - 空行跳过（行号不占）。
    - 行号是**物理行号**（含空行），错误消息报的就是用户在 Excel 里看到的行号。
    """
    reader = csv.reader(io.StringIO(text, newline=""))
    rows: list[tuple[int, list[str]]] = []
    for cells in reader:
        line_no = reader.line_num
        if not cells or all(not (c or "").strip() for c in cells):
            continue  # 空行跳过（卡面：空行跳过）
        rows.append((line_no, cells))
    if not rows:
        return [], []
    header = [c.strip() for c in rows[0][1]]
    return header, rows[1:]


# ---------------------------------------------------------------------------
# 3. 校验 + 映射（先全量校验，一条错误都不放过）
# ---------------------------------------------------------------------------


def _resolve_name(
    conn: sqlite3.Connection,
    org_id: str,
    name: str,
    kind: str,
) -> tuple[str | None, str | None]:
    """按名字 + kind 在**本组织内**解析对象 id。

    返回 (object_id, error_message)，二者互斥：
    - 唯一命中 → (id, None)
    - 0 命中   → (None, "找不到该对象（kind=xxx）")
    - >1 命中  → (None, "同名 N 个，候选：..."（列全部 name+id）)

    🔴 >1 命中绝不取第一条：学生和教师都是 user，取第一条等于
    把课排到同名另一个人头上，且**不报错**——本卡明令禁止的静默错。
    🔴 必须过滤 org_id：跨组织的同名对象既不是候选、更不能被解析成功，
    否则等于给跨组织写库开口子。
    """
    rows = conn.execute(
        "SELECT id, name FROM objects WHERE org_id = ? AND kind = ? AND name = ?"
        " ORDER BY id",
        (org_id, kind, name),
    ).fetchall()
    if not rows:
        return None, f"「{name}」找不到该对象（kind={kind}）"
    if len(rows) > 1:
        candidates = "、".join(f"{r['name']}({r['id']})" for r in rows)
        return None, f"「{name}」在本组织内有 {len(rows)} 个同名 kind={kind} 对象，无法确定是哪个：{candidates}"
    return rows[0]["id"], None


def _term_end(conn: sqlite3.Connection, org_id: str, today: date) -> str:
    """生效结束的缺省值 = 学期末。

    取覆盖 today 的学期；没有覆盖的则取第一个还没结束的学期；
    都没有 → ApiError(400)（组织级配置缺失，不是行错误）。
    """
    row = conn.execute(
        "SELECT end_date FROM terms WHERE org_id = ? AND start_date <= ? AND end_date >= ?"
        " ORDER BY start_date DESC LIMIT 1",
        (org_id, today.isoformat(), today.isoformat()),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT end_date FROM terms WHERE org_id = ? AND end_date >= ?"
            " ORDER BY start_date LIMIT 1",
            (org_id, today.isoformat()),
        ).fetchone()
    if row is None:
        raise ApiError(
            400,
            "该组织没有覆盖当前日期的学期，无法确定「生效结束」缺省值"
            "（请在 CSV 里显式给出生效结束列，或先在系统里建学期）",
        )
    return row["end_date"]


def _parse_iso(value: str) -> date | None:
    """严格 YYYY-MM-DD（不接受 20260907 这类无连字符写法）。"""
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def validate_and_map(
    conn: sqlite3.Connection,
    org_id: str,
    header: list[str],
    rows: list[tuple[int, list[str]]],
    today: date,
) -> tuple[list[MappedRow], list[dict]]:
    """全量校验 + 名字解析 + 默认值填充。

    返回 (映射成功的行, 错误列表)。🔴 错误**跨行全收**：第 3 行坏不会
    遮住第 7 行的错——「errors 列全部行号，一条也不放过」。
    每行只报**第一个**错误（一行一个错已足够定位），但每行都会被检查。
    """
    errors: list[dict] = []

    def err(line: int, message: str) -> None:
        errors.append({"line": line, "message": message})

    # ---- 表头校验（line=1：表头永远在第一行，空行已被 parse_csv 跳过）----
    if not header:
        err(1, "CSV 为空：没有表头（第一行必须是列名）")
        return [], errors
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        err(1, f"表头缺少必填列：{('、'.join(missing))}")
    unknown = [c for c in header if c not in ALL_COLUMNS]
    if unknown:
        err(1, f"表头出现不认识的列（本接口不静默忽略未知列）：{('、'.join(unknown))}")
    if errors:
        return [], errors

    # ---- 行数上限 ----
    if len(rows) > MAX_ROWS:
        err(rows[MAX_ROWS][0], f"数据行数 {len(rows)} 超过上限 {MAX_ROWS}")
        return [], errors

    col_index = {name: i for i, name in enumerate(header)}

    def cell(cells: list[str], name: str) -> str:
        """取列值。🔴 可选列可以不出现在表头里（缺列 = 缺省值路径），
        也可能比表头短（串列防护在 len 检查处）——一律返回空串。"""
        idx = col_index.get(name)
        if idx is None or idx >= len(cells):
            return ""
        return cells[idx].strip()

    # 缺省值（惰性解析：只有真的有行缺「生效结束」才查学期表）
    default_from = None  # 本周一
    default_to: str | None = None  # 学期末

    mapped: list[MappedRow] = []
    for line_no, cells in rows:
        if len(cells) > len(header):
            err(line_no, f"该行有 {len(cells)} 个单元格，比表头（{len(header)} 列）多，请检查是否串列")
            continue

        # ---- 必填字段 ----
        title = cell(cells, "课程")
        if not title:
            err(line_no, "「课程」为空（去掉首尾空白后不得为空）")
            continue
        empty_required = [c for c in ("班级", "教师", "星期", "开始节次") if not cell(cells, c)]
        if empty_required:
            err(line_no, f"必填列为空：{('、'.join(empty_required))}")
            continue

        # ---- 星期：1-7 或 周一..周日，两种都收 ----
        dow_raw = cell(cells, "星期")
        if dow_raw in _DOW_NAMES:
            dow = _DOW_NAMES[dow_raw]
        elif dow_raw in (str(d) for d in range(1, 8)):
            dow = int(dow_raw)
        else:
            err(line_no, f"「星期」取值 {dow_raw!r} 不合法（只能是 1–7 或 周一–周日）")
            continue

        # ---- 开始节次：1–16 ----
        start_raw = cell(cells, "开始节次")
        try:
            start = int(start_raw)
        except ValueError:
            err(line_no, f"「开始节次」取值 {start_raw!r} 不是整数")
            continue
        if not 1 <= start <= _MAX_PERIOD:
            err(line_no, f"「开始节次」取值 {start} 超出范围 1–{_MAX_PERIOD}")
            continue

        # ---- 持续节次：缺省 1；开始+持续-1 ≤ 16 ----
        dur_raw = cell(cells, "持续节次")
        if dur_raw:
            try:
                duration = int(dur_raw)
            except ValueError:
                err(line_no, f"「持续节次」取值 {dur_raw!r} 不是整数")
                continue
            if duration < 1:
                err(line_no, f"「持续节次」取值 {duration} 必须 ≥ 1")
                continue
        else:
            duration = 1
        if start + duration - 1 > _MAX_PERIOD:
            err(line_no, f"开始节次 {start} + 持续节次 {duration} 超出每天上限（最末节 {start + duration - 1} > {_MAX_PERIOD}）")
            continue

        # ---- 周次：每周/单周/双周，缺省每周 ----
        parity_raw = cell(cells, "周次")
        if not parity_raw:
            parity = 0
        elif parity_raw in _WEEK_PARITY:
            parity = _WEEK_PARITY[parity_raw]
        else:
            err(line_no, f"「周次」取值 {parity_raw!r} 不合法（只能是 每周/单周/双周）")
            continue

        # ---- 生效区间 ----
        vf_raw = cell(cells, "生效起始")
        if vf_raw:
            vf = _parse_iso(vf_raw)
            if vf is None:
                err(line_no, f"「生效起始」取值 {vf_raw!r} 不是合法日期（YYYY-MM-DD）")
                continue
            valid_from = vf_raw
        else:
            if default_from is None:
                default_from = today - timedelta(days=today.weekday())  # 本周一
            valid_from = default_from.isoformat()

        vt_raw = cell(cells, "生效结束")
        if vt_raw:
            vt = _parse_iso(vt_raw)
            if vt is None:
                err(line_no, f"「生效结束」取值 {vt_raw!r} 不是合法日期（YYYY-MM-DD）")
                continue
            valid_to = vt_raw
        else:
            if default_to is None:
                default_to = _term_end(conn, org_id, today)
            valid_to = default_to

        if valid_to < valid_from:
            err(line_no, f"「生效结束」({valid_to}) 早于「生效起始」({valid_from})")
            continue

        # ---- 名字解析（字段全对才查库，避免一行报多个错淹没关键信息）----
        class_name = cell(cells, "班级")
        class_id, e1 = _resolve_name(conn, org_id, class_name, "group")
        if e1:
            err(line_no, f"「班级」{e1}")
            continue
        teacher_name = cell(cells, "教师")
        teacher_id, e2 = _resolve_name(conn, org_id, teacher_name, "user")
        if e2:
            err(line_no, f"「教师」{e2}")
            continue
        room_name = cell(cells, "教室")
        room_id = None
        if room_name:
            room_id, e3 = _resolve_name(conn, org_id, room_name, "resource")
            if e3:
                err(line_no, f"「教室」{e3}")
                continue

        mapped.append(
            MappedRow(
                line=line_no,
                title=title,
                class_id=class_id,
                teacher_id=teacher_id,
                room_id=room_id,
                dow=dow,
                start=start,
                duration=duration,
                parity=parity,
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )

    # ---- 重复行（映射后口径）----
    seen: dict[tuple, int] = {}
    dedup_errors: list[dict] = []
    kept: list[MappedRow] = []
    for r in mapped:
        key = r.dedup_key()
        if key in seen:
            dedup_errors.append(
                {"line": r.line, "message": f"与第 {seen[key]} 行重复（班级/教师/课程/星期/节次/周次/区间/教室完全相同）"}
            )
        else:
            seen[key] = r.line
            kept.append(r)
    errors.extend(dedup_errors)

    errors.sort(key=lambda e: e["line"])
    return kept, errors


# ---------------------------------------------------------------------------
# 4. 落库（全量事务：任一行错 → 已写入的全部补偿删除）
# ---------------------------------------------------------------------------


def write_rows(
    conn: sqlite3.Connection,
    org_id: str,
    created_by: str | None,
    mapped: list[MappedRow],
) -> list[str]:
    """逐行 create_periodic 落库，返回 schedule id 列表。

    🔴 全量事务的实现说明（为什么不是 BEGIN/ROLLBACK）：
    create_periodic（schedules.py:59）内部自己 conn.commit()——这是领域层
    既有行为，不能改（红线）。SQLite 里外层事务撑不过它第一次 commit。
    所以这里用**补偿删除**：写第 N 行失败时，把已写入的 1..N-1 行按
    schedule id 删掉（子表 schedule_periodic / participants / resources
    都是 ON DELETE CASCADE，删主表行即全清），再 commit。
    最终效果 = 任一行错 → 库内 0 条新增。
    """
    ids: list[str] = []
    try:
        for r in mapped:
            sid = create_periodic(
                conn,
                NewSchedule(
                    title=r.title,
                    org_id=org_id,
                    created_by=created_by,
                    participants={"target": [r.class_id], "teacher": [r.teacher_id]},
                    resources=[r.room_id] if r.room_id else [],
                ),
                slot_bitmap=r.slot_bitmap,
                week_parity=r.parity,
                valid_from=r.valid_from,
                valid_to=r.valid_to,
            )
            ids.append(sid)
    except Exception:
        for sid in ids:
            conn.execute("DELETE FROM schedules WHERE id = ?", (sid,))
        conn.commit()
        raise
    return ids


def preview_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """把当前库备份成内存副本（dry_run 预演用，真库一个字节都不动）。

    dry_run 的冲突统计在**含待导入行**的库上算才有意义（预演「导进去之后
    会撞几处」）。SQLite 标准库的 Connection.backup() 做整库拷贝，
    内存副本上随便写、随便 commit，丢弃即回滚。
    """
    dst = sqlite3.connect(":memory:")
    dst.row_factory = sqlite3.Row
    conn.backup(dst)
    return dst


# ---------------------------------------------------------------------------
# 5. 冲突统计（只算导入区间，跳度 > 62 天截断 + advisory）
# ---------------------------------------------------------------------------


def conflicts_payload(
    conn: sqlite3.Connection,
    org_id: str,
    mapped: list[MappedRow],
) -> dict:
    """导入区间 [min(valid_from), max(valid_to)] 的原始冲突数（scan().count 口径）。

    - count 与 /api/conflicts 的 raw_count 同源（occupancy.scan），
      数字对得上，答辩不会被问「为什么两个接口数不一样」。
    - 跳度 > 62 天 → 截断到 62 天并附 advisory（防整学期展开拖慢响应）。
    - 无数据行（空 CSV 合法导入）→ count=0，from/to 为 None。
    """
    if not mapped:
        return {"count": 0, "from": None, "to": None}
    date_from = min(date.fromisoformat(r.valid_from) for r in mapped)
    date_to = max(date.fromisoformat(r.valid_to) for r in mapped)
    payload: dict = {"from": date_from.isoformat(), "to": date_to.isoformat()}
    if (date_to - date_from).days > MAX_CONFLICT_WINDOW_DAYS:
        date_to = date_from + timedelta(days=MAX_CONFLICT_WINDOW_DAYS)
        payload["to"] = date_to.isoformat()
        payload["advisory"] = (
            f"导入区间超过 {MAX_CONFLICT_WINDOW_DAYS} 天，冲突统计已截断至 "
            f"{date_from.isoformat()} ~ {date_to.isoformat()}（完整区间请分段导入）"
        )
    report = scan(conn, org_id, date_from, date_to, cfg=load_slot_config(conn, org_id))
    payload["count"] = report.count
    # 输出键序稳定（count 在前更直观，与 /api/conflicts 的 count/raw_count 风格一致）
    return {"count": payload.pop("count"), **payload}


# ---------------------------------------------------------------------------
# 6. 权限（仅 own 档可导入）
# ---------------------------------------------------------------------------


def require_import_level(conn: sqlite3.Connection, user_id: str, group_id: str) -> None:
    """导入权限：该组 effective_level 必须是 own，否则 403。

    与 b4_kernels.require_own_level 同语义但**不复用它的函数**——那条的错误
    文案是「固化需要 own 档」，语义对不上导入。两处口径同源：
    都只调 authz.effective_level（admin 本组织短路 → own；teacher 天花板
    propose → 永远到不了 own），不重复实现档位比较。
    """
    eff = effective_level(conn, user_id, group_id)
    if eff != "own":
        raise ApiError(
            403,
            f"批量导入需要该组的 own 档权限（当前 {eff or '无授权'}）。"
            "导入是批量直接写库，不走审批流，仅限管理员执行。",
        )


# ---------------------------------------------------------------------------
# 7. 编排：一次导入的完整流程
# ---------------------------------------------------------------------------


def run_import(
    conn: sqlite3.Connection,
    org_id: str,
    created_by: str | None,
    csv_text: str,
    encoding: str,
    dry_run: bool,
    today: date,
):
    """完整导入流程。返回 200 dict 或 (400, dict)（错误全在 errors 数组里）。

    为什么 400 用元组而不是 ApiError：ApiError 只能带一条 message，
    而卡面要求 400 响应携带**全部**行号错误列表——必须返回自定义 body。
    http_api._normalize_result 对 (status, payload) 元组原生支持
    （require_auth 的 401 就是这么返回的）。
    """
    header, rows = parse_csv(csv_text)
    mapped, errors = validate_and_map(conn, org_id, header, rows, today)

    base = {
        "ok": False,
        "dry_run": dry_run,
        "imported": 0,
        "errors": errors,
        "encoding": encoding,
    }
    if errors:
        return 400, base  # 🔴 任一行错 → 一条都不写（全量回滚的入口在源头）

    if dry_run:
        # 预演：内存副本上真写一遍，冲突数按「导进去之后」算
        preview = preview_connection(conn)
        try:
            write_rows(preview, org_id, created_by, mapped)
            conflicts = conflicts_payload(preview, org_id, mapped)
        finally:
            preview.close()
        return {
            "ok": True,
            "dry_run": True,
            "imported": len(mapped),
            "errors": [],
            "encoding": encoding,
            "conflicts": conflicts,
        }

    write_rows(conn, org_id, created_by, mapped)
    conflicts = conflicts_payload(conn, org_id, mapped)
    return {
        "ok": True,
        "dry_run": False,
        "imported": len(mapped),
        "errors": [],
        "encoding": encoding,
        "conflicts": conflicts,
    }
