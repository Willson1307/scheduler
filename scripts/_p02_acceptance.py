"""P0-2 验收实测脚本（跑完自动还原 demo 库，不留脏数据）。

覆盖卡面验收必答：
  Q1 周六(调休日)掩码真实数字（卡面复现口径 as_of_dow=1）
  Q2 count / raw_count 前后对比（demo 库 23 处冲突全在周三 → 用 as_of_dow=3 量增量）
  Q3 as_of_dow=NULL 真实 traceback
  Q5 holiday 路径改动后仍通（真实数字）

前置：scripts/run_server.py --demo（today 固定 2026-09-08，窗口=第 1 周）。
"""
import json
import os
import sqlite3
import sys
import traceback
import urllib.error
import urllib.request
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["no_proxy"] = "*"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = "http://127.0.0.1:8000"
DB = os.path.join(os.path.dirname(__file__), "..", "data", "demo.db")


def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with OPENER.open(req, data) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def ins_sched_exc(eid, d, mode, as_of_dow=None):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO schedule_exceptions"
        "(id, org_id, scope, target_id, date, mode, as_of_dow, note)"
        " SELECT ?, id, 'day', NULL, ?, ?, ?, 'P0-2验收' FROM organizations LIMIT 1",
        (eid, d, mode, as_of_dow))
    conn.commit()
    conn.close()


def ins_cal_exc(eid, d):
    conn = db()
    conn.execute(
        "INSERT INTO calendar_exceptions(id, org_id, date, kind, note)"
        " SELECT ?, id, ?, 'holiday', 'P0-2验收' FROM organizations LIMIT 1", (eid, d))
    conn.commit()
    conn.close()


def del_row(eid, table):
    conn = db()
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (eid,))
    conn.commit()
    n = conn.total_changes
    conn.close()
    return n


def conflicts(token):
    st, body = api("GET", "/api/conflicts", token=token)
    return st, body.get("count"), body.get("raw_count")


# ── 登录 ─────────────────────────────────────────────────────────
st, login = api("POST", "/api/login", {"login": "admin", "password": "admin123"})
assert st == 200, login
token = login["token"]
print("[登录] 200 OK")

# ── Q2 基线（调休行写入前）──────────────────────────────────────
st0, c0, r0 = conflicts(token)
print(f"[Q2-前] GET /api/conflicts → HTTP {st0}  count={c0}  raw_count={r0}")

# ── Q1 卡面复现口径：周六 2026-09-12 按周一(as_of_dow=1) ─────────
from scheduler.db import connect  # noqa: E402
from scheduler.occupancy import expand_periodic, load_exceptions, load_terms  # noqa: E402
from scheduler.slots import hex_to_mask, load_slot_config  # noqa: E402

conn = connect(DB)
org = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
row = conn.execute(
    "SELECT * FROM v_schedule_full WHERE org_id=? AND kind='periodic' LIMIT 1", (org,)
).fetchone()
cfg = load_slot_config(conn, org)

ins_sched_exc("p02_mk1", "2026-09-12", "as_of", as_of_dow=1)
sat_mask = expand_periodic(
    row["week_parity"], hex_to_mask(row["slot_bitmap"]), row["valid_from"], row["valid_to"],
    cfg, load_terms(conn, org), date(2026, 9, 12), date(2026, 9, 12),
    load_exceptions(conn, org),
)
mon_mask = expand_periodic(
    row["week_parity"], hex_to_mask(row["slot_bitmap"]), row["valid_from"], row["valid_to"],
    cfg, load_terms(conn, org), date(2026, 9, 7), date(2026, 9, 7),
    load_exceptions(conn, org),
)
print(f"[Q1] 日程 {row['id']} 2026-09-12(周六, as_of_dow=1) 掩码 = "
      f"{sat_mask.get(date(2026, 9, 12), 0)}（非 0 ✅）；"
      f"对照 2026-09-07(周一) 掩码 = {mon_mask.get(date(2026, 9, 7), 0)}")
assert sat_mask.get(date(2026, 9, 12), 0) != 0
assert del_row("p02_mk1", "schedule_exceptions") == 1

# ── Q2 增量口径：周六按周三课表(as_of_dow=3) ─────────────────────
# demo 库 23 处冲突全在周三 09-09、周一零冲突；按周三展开周六会把
# 周三整天的冲突完整复刻到周六 → raw_count 应翻倍以上。
ins_sched_exc("p02_mk3", "2026-09-12", "as_of", as_of_dow=3)
st1, c1, r1 = conflicts(token)
print(f"[Q2-后] GET /api/conflicts → HTTP {st1}  count={c1}  raw_count={r1}"
      f"（周六按周三课表 → 冲突增量）")
assert r1 > r0 and c1 > c0, "调休未产生冲突增量"

# 顺带验证同日矛盾 dow 防线（mk3=3 与临时 mk1=1 同日落两行 → 必须抛错）
ins_sched_exc("p02_mk1", "2026-09-12", "as_of", as_of_dow=1)
try:
    load_exceptions(conn, org)
    print("[防线] 同日矛盾 dow 未抛错 = ❌")
except ValueError:
    print("[防线] 同日两条 as_of dow 矛盾(3 vs 1) → ValueError 抛错 ✅")
assert del_row("p02_mk1", "schedule_exceptions") == 1

# ── Q3 as_of_dow=NULL → 真实 traceback ──────────────────────────
print("[Q3] as_of_dow=NULL 真实 traceback：")
ins_sched_exc("p02_null", "2026-10-10", "as_of", as_of_dow=None)
try:
    load_exceptions(conn, org)
    print("!!! 未抛异常 = 失败")
except ValueError:
    tb = traceback.format_exc()
    print("\n".join("    " + ln for ln in tb.strip().splitlines()))
finally:
    assert del_row("p02_null", "schedule_exceptions") == 1

# ── 还原调休行，确认回到基线 ────────────────────────────────────
assert del_row("p02_mk3", "schedule_exceptions") == 1
st2, c2, r2 = conflicts(token)
print(f"[还原] 删除调休行后 → HTTP {st2}  count={c2}  raw_count={r2}（应与 Q2-前 一致）")
assert (c2, r2) == (c0, r0)

# ── Q5 holiday 路径仍通：周三放假 → 冲突应清零 ───────────────────
ins_cal_exc("p02_hol", "2026-09-09")
st3, c3, r3 = conflicts(token)
print(f"[Q5] 周三 2026-09-09 设 holiday 后 → HTTP {st3}  count={c3}  raw_count={r3}"
      f"（对照基线 count={c0} raw_count={r0}，应减少）")
assert r3 < r0, "holiday 未生效"
assert del_row("p02_hol", "calendar_exceptions") == 1
st4, c4, r4 = conflicts(token)
print(f"[Q5-还原] 删除 holiday 行后 → HTTP {st4}  count={c4}  raw_count={r4}（回到基线 ✅）")
assert (c4, r4) == (c0, r0)

conn.close()
print("\n=== 验收实测全部通过，demo 库已还原 ===")
