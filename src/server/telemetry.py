"""S14 埋点后端核心（分支 feat/ops）。

本文件是「四类试用数据」聚合口径的**唯一实现源**，`api_telemetry.py` 与
`scripts/telemetry_report.py` 都调用这里的函数，禁止各自再算一遍
（对齐 b3_kernels.py 的「同一口径只实现一份，防止漂移」铁律）。

口径唯一来源：docs/08-试用组织数据口径.md。要点：
  - 只出聚合数、永不外发；payload 只放结构化参数，禁止姓名/班级名/课表内容。
  - trial_code = 稳定短码（人数去重）；session_id = 每会话随机串（时长）。
  - org_kind = trial/demo（隔离现场演示点击）；聚合只统计 org_kind='trial'。
  - 事件三元组 task_start / task_success / task_abandon（完成率分母/分子/放弃），
    满意度走 event='survey'，payload={"score": 1..5}。

🔴 命名铁律：本文件是纯内核，**不叫 api_*.py、不导出 ROUTES**（否则会被
routes.scan() 扫到并因「未导出非空 ROUTES」崩启动）。路由注册在
api_ping.py（POST /api/ping）与 api_telemetry.py（GET /api/telemetry）。

只用 Python 标准库。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime

from scheduler.db import init_db, new_id

from .http_api import ApiError

# ---------------------------------------------------------------------------
# 契约常量
# ---------------------------------------------------------------------------

#: 合法事件白名单（docs/08 §3.2 三事件 + survey 满意度）。
EVENTS = {"task_start", "task_success", "task_abandon", "survey"}

#: 防挂机阈值（秒）：相邻事件间隔 ≤ 该值计入有效时长，超过则整段不计（docs/08 §3.3）。
MAX_GAP_SECONDS = 300

#: 单次会话最小时长（秒）：低于此视为误触，不计入（docs/08 §3.3）。
MIN_SESSION_SECONDS = 5

_TRIAL_LOGIN_RE = re.compile(r"^trial_t(\d+)$")


# ---------------------------------------------------------------------------
# 建表（幂等）
# ---------------------------------------------------------------------------


def ensure_event_log(conn: sqlite3.Connection) -> None:
    """确保 event_log 表存在（幂等）。

    已存在的 data/*.db 不会在启动时重建表（ensure_database 只在库缺失时 seed），
    故写/读前调用。用 sqlite_master 判断，缺失则跑 init_db——schema.sql 是
    event_log 的**唯一 DDL 来源**，杜绝「表结构在 schema.sql 与代码里各写一遍」的漂移。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'event_log'"
    ).fetchone()
    if row is None:
        init_db(conn)


# ---------------------------------------------------------------------------
# 身份 / 组织派生
# ---------------------------------------------------------------------------


def derive_trial_code(login: str) -> str:
    """从登录名推导稳定短码：`trial_t01` → `T01`；其余（demo 教师 `t01`、
    管理员 `admin`/`trial_admin` 等）→ 空串（不计入试用人数）。

    由后端推导、前端不传，杜绝伪造；映射 trial_tNN → TNN 是 1:1 确定性。
    """
    m = _TRIAL_LOGIN_RE.match(login or "")
    return f"T{m.group(1)}" if m else ""


def org_of_user(conn: sqlite3.Connection, object_id: str) -> str:
    """用户对象（token 里的 object_id）→ 所属组织 id。"""
    row = conn.execute("SELECT org_id FROM objects WHERE id = ?", (object_id,)).fetchone()
    if row is None:
        raise ApiError(500, "用户对象缺失，无法定位组织")
    return row["org_id"]


def org_kind_of(conn: sqlite3.Connection, org_id: str) -> str:
    """组织 → trial/demo：该组织内存在 login 以 `trial_` 开头的用户即 trial，否则 demo。

    demo 与 trial 的组织名都是「试用学校」（seeds.py 与 build_trial_org.py 同名），
    无法按名区分；登录名前缀是两者唯一稳定差异（trial_admin/trial_tNN vs admin/tNN）。
    """
    row = conn.execute(
        "SELECT 1 FROM users u JOIN objects o ON u.object_id = o.id "
        "WHERE o.org_id = ? AND u.login LIKE 'trial\\_%' ESCAPE '\\' LIMIT 1",
        (org_id,),
    ).fetchone()
    return "trial" if row else "demo"


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def write_event(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    trial_code: str,
    session_id: str,
    event: str,
    payload: dict,
    org_kind: str,
) -> str:
    """落一条事件，返回事件 id。ts 用服务端当前时间（秒级 ISO）。"""
    ts = datetime.now().isoformat(timespec="seconds")
    eid = new_id("log")
    conn.execute(
        "INSERT INTO event_log "
        "(id, ts, trial_code, session_id, event, payload, org_id, org_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eid,
            ts,
            trial_code,
            session_id,
            event,
            json.dumps(payload, ensure_ascii=False),
            org_id,
            org_kind,
        ),
    )
    conn.commit()
    return eid


# ---------------------------------------------------------------------------
# 四类聚合（只统计 org_kind='trial'，只出聚合数）
# ---------------------------------------------------------------------------


def trial_participants(conn: sqlite3.Connection) -> int:
    """试用人数：去重 trial_code，且该 code 至少有 1 条 task_start（docs/08 §1）。"""
    row = conn.execute(
        "SELECT COUNT(DISTINCT trial_code) AS c FROM event_log "
        "WHERE org_kind = 'trial' AND trial_code != '' AND event = 'task_start'"
    ).fetchone()
    return row["c"]


def task_completion(conn: sqlite3.Connection) -> dict:
    """任务完成率 = task_success / task_start；abandon 单独报（不是失败，docs/08 §3.2）。

    🔴 口径（rPm5gf 问题 11 修复）：必须过滤 trial_code != ''——trial 组织的
    管理员 trial_admin（org_kind=trial 但 trial_code=''）的事件不计入，
    否则「完成率分母 > 人数」口径不自洽。与 trial_participants 对齐。
    """
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN event = 'task_start' THEN 1 ELSE 0 END) AS starts, "
        "SUM(CASE WHEN event = 'task_success' THEN 1 ELSE 0 END) AS success, "
        "SUM(CASE WHEN event = 'task_abandon' THEN 1 ELSE 0 END) AS abandon "
        "FROM event_log WHERE org_kind = 'trial' AND trial_code != ''"
    ).fetchone()
    starts = row["starts"] or 0
    success = row["success"] or 0
    abandon = row["abandon"] or 0
    return {
        "starts": starts,
        "success": success,
        "abandon": abandon,
        "completion_rate": round(success / starts, 4) if starts else None,
    }


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def session_durations(conn: sqlite3.Connection) -> dict:
    """平均使用时长（docs/08 §3.3 防挂机规则）。

    有效区间 = 会话内相邻事件间隔 ≤ 5 分钟（MAX_GAP_SECONDS）的区间之和；
    超过 5 分钟的空闲整段不计；单次会话 < 5 秒（MIN_SESSION_SECONDS）不计（误触）。
    报告两个数：按会话平均、按人平均（docs/08 §1「两个数都给」）。

    🔴 口径（rPm5gf 问题 11 修复）：过滤 trial_code != ''——trial_admin 的
    会话不计入 session_count / 平均时长，与 trial_participants 对齐。
    """
    rows = conn.execute(
        "SELECT session_id, trial_code, ts FROM event_log "
        "WHERE org_kind = 'trial' AND trial_code != '' ORDER BY session_id, ts"
    ).fetchall()

    sessions: dict[str, list] = defaultdict(list)
    for r in rows:
        sessions[r["session_id"]].append(r)

    per_session: list[float] = []
    per_person: dict[str, float] = defaultdict(float)
    for sid, evs in sessions.items():
        evs.sort(key=lambda r: r["ts"])  # ISO 秒级字符串排序 == 时间序
        total = 0.0
        for a, b in zip(evs, evs[1:]):
            gap = (_parse_ts(b["ts"]) - _parse_ts(a["ts"])).total_seconds()
            if 0 <= gap <= MAX_GAP_SECONDS:
                total += gap
        if total < MIN_SESSION_SECONDS:
            continue  # 误触，不计
        per_session.append(total)
        # 会话归属：取该会话第一条非空 trial_code（会话内 code 应稳定）
        code = next((r["trial_code"] for r in evs if r["trial_code"]), "") or ""
        if code:
            per_person[code] += total

    return {
        "session_count": len(per_session),
        "person_count": len(per_person),
        "avg_session_seconds": round(sum(per_session) / len(per_session), 1) if per_session else 0,
        "avg_person_seconds": round(sum(per_person.values()) / len(per_person), 1) if per_person else 0,
    }


def satisfaction(conn: sqlite3.Connection) -> dict:
    """满意度：event='survey' 的 payload.score（1..5）→ 均值 + ≥4 占比 + 分布（docs/08 §4.2）。

    🔴 口径（rPm5gf 问题 11 修复）：过滤 trial_code != ''——trial_admin 的
    问卷不计入，否则管理员点几下就把满意度均值拉偏。与 trial_participants 对齐。
    """
    rows = conn.execute(
        "SELECT payload FROM event_log "
        "WHERE org_kind = 'trial' AND trial_code != '' AND event = 'survey'"
    ).fetchall()
    scores: list[float] = []
    for r in rows:
        try:
            p = json.loads(r["payload"])
            s = p.get("score")
            if isinstance(s, (int, float)) and 1 <= s <= 5:
                scores.append(float(s))
        except (ValueError, TypeError):
            continue
    n = len(scores)
    if n == 0:
        return {"n": 0, "mean": None, "ge4_ratio": None,
                "distribution": {i: 0 for i in range(1, 6)}}
    mean = sum(scores) / n
    ge4 = sum(1 for s in scores if s >= 4) / n
    dist = {i: sum(1 for s in scores if int(s) == i) for i in range(1, 6)}
    return {
        "n": n,
        "mean": round(mean, 2),
        "ge4_ratio": round(ge4, 4),
        "distribution": dist,
    }


def aggregate(conn: sqlite3.Connection) -> dict:
    """四类聚合的完整出口（api_telemetry 与 telemetry_report 共用，单一口径源）。

    只返回聚合数，不含任何个人可识别字段；输出全序/确定性，两次调用逐字一致。
    """
    ensure_event_log(conn)
    return {
        "participants": trial_participants(conn),
        "duration": session_durations(conn),
        "completion": task_completion(conn),
        "satisfaction": satisfaction(conn),
    }
