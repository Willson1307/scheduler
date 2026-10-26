"""trial_ready.py —— 教师试用「一键就绪」核对（TRIAL-REANCHOR 配套）。

一条命令做 6 项核对，全过 exit 0，任一失败 exit 1 并逐项打印 PASS/FAIL：

    ① 公网 /api/health 的 server_date == 期望日期（默认本机当日）
    ② 13 个试用账号（trial_admin + trial_t01..t12）全部登录 200
    ③ trial_t01 的 /api/conflicts raw_count > 0（主场景「活动撞课表」在）
    ④ event_log 表存在且可插入一条再删掉（写权限探测，不留痕，经 ssh 直连库）
    ⑤ survey_responses 表缺失时可建（调 scripts/import_survey.py 的
       ensure_survey_table，幂等，经 ssh 在服务器上执行）
    ⑥ 一句话就绪/未就绪结论

用法：
    # 试用当天（服务器 SCHEDULER_TODAY 锚当日）：
    python deploy/trial_ready.py --base-url http://106.53.214.131:8000

    # 试用前的服务器预演（服务器已显式锚定试用日 2026-11-11）：
    python deploy/trial_ready.py --base-url http://106.53.214.131:8000 \
        --expect-today 2026-11-11

🔴 为什么 ① 要有 --expect-today：/api/conflicts 只扫 today 所在周
（b3_kernels.resolve_window）。学期锚 2026-11-09 时，只有 today 落在
11-09~11-15 那一周，③ 的冲突才非零——「server_date == 本机当日」与
「raw_count > 0」在试用周之外不可能同时成立。预演期服务器锚定试用日，
① 就要用 --expect-today 对齐同一个日期；试用当天（11-11~13）默认值即可。

口令纪律：口令只从本地 data/trial-passwords.txt 读入内存，用于登录请求；
不写进代码、不打印、不落日志。④⑤ 经 ssh 在服务器库上直接探测。

只用 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ADMIN_LOGIN = "trial_admin"
TEACHER_LOGINS = [f"trial_t{i:02d}" for i in range(1, 13)]
ALL_LOGINS = [ADMIN_LOGIN] + TEACHER_LOGINS

CHECKS: list[tuple[str, bool, str]] = []

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f"  —— {detail}"
    print(line, flush=True)
    return ok


# ---------------------------------------------------------------------------
# HTTP（公网；本地代理必须旁路，否则 10061/空响应）
# ---------------------------------------------------------------------------

def request(base: str, method: str, path: str, body=None, token=None, timeout=15.0):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except (ValueError, UnicodeDecodeError):
            return e.code, None
    except (urllib.error.URLError, OSError) as e:
        return None, {"_unreachable": repr(e)}


def login(base: str, login_name: str, password: str):
    st, body = request(base, "POST", "/api/login",
                       {"login": login_name, "password": password})
    if st == 200 and isinstance(body, dict) and body.get("token"):
        return st, body["token"]
    return st, None


# ---------------------------------------------------------------------------
# 口令表（只进内存；绝不打印/落日志）
# ---------------------------------------------------------------------------

def load_passwords(path: str) -> dict[str, str]:
    table: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                parts = ln.split(None, 1)
                if len(parts) == 2 and parts[0] in ALL_LOGINS:
                    table[parts[0]] = parts[1].strip()
    return table


# ---------------------------------------------------------------------------
# 服务器侧直连探测（④ 写权限 / ⑤ 建表），脚本经 ssh stdin 喂给 python3 -
# ---------------------------------------------------------------------------

#: ④ event_log 插一条再删一条（session_id 打探针标记，删后行数必须回到原值）。
_EVENT_LOG_PROBE = r"""
import sqlite3, sys
from datetime import datetime
db = sys.argv[1]
conn = sqlite3.connect(db)
try:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_log'"
    ).fetchone()
    if row is None:
        print("TABLE_MISSING")
        sys.exit(0)
    before = conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
    org = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()
    probe = "trial_ready_probe_" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    conn.execute(
        "INSERT INTO event_log "
        "(id, ts, trial_code, session_id, event, payload, org_id, org_kind, created_at) "
        "VALUES (?, ?, '', ?, 'task_start', '{}', ?, 'trial', datetime('now'))",
        ("probe-" + probe, datetime.now().isoformat(timespec="seconds"),
         probe, org[0] if org else ""),
    )
    conn.commit()
    n_probe = conn.execute(
        "SELECT COUNT(*) FROM event_log WHERE session_id = ?", (probe,)
    ).fetchone()[0]
    conn.execute("DELETE FROM event_log WHERE session_id = ?", (probe,))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
    if n_probe == 1 and after == before:
        print("PROBE_OK before=%d after=%d" % (before, after))
    else:
        print("PROBE_BAD inserted=%d before=%d after=%d" % (n_probe, before, after))
finally:
    conn.close()
"""

#: ⑤ survey_responses 建表探测（ensure_survey_table 幂等，建完查 sqlite_master）。
_SURVEY_PROBE = r"""
import os, sys, sqlite3
root = sys.argv[1]
db = sys.argv[2]
sys.path.insert(0, os.path.join(root, "src"))
sys.path.insert(0, os.path.join(root, "scripts"))
import import_survey  # noqa: E402  # ensure_survey_table 的唯一实现源（防口径漂移）
conn = sqlite3.connect(os.path.join(root, db))
try:
    import_survey.ensure_survey_table(conn)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='survey_responses'"
    ).fetchone()
    print("SURVEY_OK" if row else "SURVEY_MISSING")
finally:
    conn.close()
"""


def ssh_python(args: argparse.Namespace, script: str, script_args: list[str]) -> tuple[int, str, str]:
    """在服务器上跑一段 python（脚本经 stdin），返回 (rc, stdout, stderr)。"""
    remote = "python3 - " + " ".join(shlex.quote(a) for a in script_args)
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
        "-i", args.ssh_key,
        f"{args.ssh_user}@{args.ssh_host}",
        remote,
    ]
    proc = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# ---------------------------------------------------------------------------
# 六项核对
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="教师试用一键就绪核对（6 项，任一失败 exit 1）")
    ap.add_argument("--base-url", default="http://106.53.214.131:8000",
                    help="公网服务地址（默认 http://106.53.214.131:8000）")
    ap.add_argument("--expect-today", default=None, metavar="YYYY-MM-DD",
                    help="① 期望的 server_date（默认=本机当日）。试用前预演时服务器"
                         "锚定在试用日，须传同一日期；试用当天不传")
    ap.add_argument("--ssh-host", default="106.53.214.131")
    ap.add_argument("--ssh-user", default="ubuntu")
    ap.add_argument("--ssh-key",
                    default=os.path.join(os.path.expanduser("~"), ".ssh", "wb_deploy"))
    ap.add_argument("--remote-root", default="/home/ubuntu/scheduler")
    ap.add_argument("--remote-db", default="data/trial.db",
                    help="④⑤ 探测的库（相对 remote-root；默认 data/trial.db）")
    ap.add_argument("--password-file",
                    default=os.path.join(REPO, "data", "trial-passwords.txt"),
                    help="本地口令表（13 行；口令只进内存，不打印不落日志）")
    args = ap.parse_args()

    expect = args.expect_today or date.today().isoformat()
    if args.expect_today:
        try:
            date.fromisoformat(args.expect_today)
        except ValueError:
            print(f"FAIL --expect-today 需要 YYYY-MM-DD，收到 {args.expect_today!r}",
                  file=sys.stderr)
            return 2

    print("=" * 62)
    print(f"试用就绪核对（trial_ready）  base_url={args.base_url}")
    print(f"期望 server_date={expect}"
          + ("（--expect-today 显式传入）" if args.expect_today else "（默认=本机当日）"))
    print("=" * 62)

    # ── ① /api/health 的 server_date ─────────────────────────
    st, body = request(args.base_url, "GET", "/api/health")
    server_date = body.get("server_date") if isinstance(body, dict) else None
    if st != 200 or not server_date:
        check("① /api/health 可达且带 server_date", False,
              f"status={st}，body={body!r:.120}")
    else:
        diff = ""
        if server_date != expect:
            try:
                d = (date.fromisoformat(server_date) - date.fromisoformat(expect)).days
                diff = f"，相差 {d:+d} 天"
            except ValueError:
                diff = ""
        check("① server_date == 期望日期", server_date == expect,
              f"server_date={server_date} vs 期望 {expect}{diff}；"
              f"status={st}")

    # ── ② 13 个试用账号全部登录 200 ──────────────────────────
    passwords = load_passwords(args.password_file)
    missing = [lg for lg in ALL_LOGINS if lg not in passwords]
    tokens: dict[str, str] = {}
    if missing:
        check("② 13 个试用账号全部登录 200", False,
              f"口令表 {args.password_file} 缺 {len(missing)} 行：{missing[:3]}…"
              f"（先跑 deploy/reset_trial_passwords.py generate）")
    else:
        bad: list[str] = []
        for lg in ALL_LOGINS:
            st_lg, tok = login(args.base_url, lg, passwords[lg])
            if tok:
                tokens[lg] = tok
            else:
                bad.append(f"{lg}={st_lg}")
        check("② 13 个试用账号全部登录 200", not bad,
              f"{len(tokens)}/13 成功" + (f"；失败 {bad}" if bad else ""))

    # ── ③ trial_t01 的 /api/conflicts raw_count > 0 ───────────
    t01 = tokens.get("trial_t01")
    if not t01:
        check("③ trial_t01 /api/conflicts raw_count > 0", False,
              "前置失败：t01 未登录（见 ②）")
    else:
        st_c, conflicts = request(args.base_url, "GET", "/api/conflicts", token=t01)
        raw = conflicts.get("raw_count") if isinstance(conflicts, dict) else None
        win = conflicts.get("window") if isinstance(conflicts, dict) else None
        check("③ trial_t01 /api/conflicts raw_count > 0",
              st_c == 200 and isinstance(raw, int) and raw > 0,
              f"status={st_c}，raw_count={raw}，count="
              f"{conflicts.get('count') if isinstance(conflicts, dict) else '-'}，"
              f"window={win}")
        if st_c == 200 and isinstance(raw, int) and raw == 0:
            print("      ⚠️ raw_count=0：大概率是「活动过期 / today 不在冲突周」——"
                  "查 server_date 与试用学期起点（build --term-start）", flush=True)

    # ── ④ event_log 存在且可写（插一条删一条，不留痕）──────────
    # 🔴 远端路径必须用 posixpath 拼：Windows 本机 os.path.join 会产出
    #    '/home/ubuntu/scheduler\\data/trial.db'，远端 sqlite 直接打不开。
    remote_db_abs = posixpath.join(args.remote_root, args.remote_db)
    rc, out, err = ssh_python(args, _EVENT_LOG_PROBE, [remote_db_abs])
    ok4 = rc == 0 and out.startswith("PROBE_OK")
    detail4 = out.replace("\n", " | ") if out else f"rc={rc}"
    if err:
        detail4 += f"；stderr={err[:160]}"
    check("④ event_log 存在且可写（插 1 条再删，行数复原）", ok4, detail4)

    # ── ⑤ survey_responses 缺失可建（幂等）────────────────────
    rc, out, err = ssh_python(args, _SURVEY_PROBE, [args.remote_root, args.remote_db])
    ok5 = rc == 0 and out.startswith("SURVEY_OK")
    detail5 = out.replace("\n", " | ") if out else f"rc={rc}"
    if err:
        detail5 += f"；stderr={err[:160]}"
    check("⑤ survey_responses 可建/已建（ensure_survey_table 幂等）", ok5, detail5)

    # ── ⑥ 一句话结论 ─────────────────────────────────────────
    failed = [c for c in CHECKS if not c[1]]
    if failed:
        conclusion = (f"未就绪：{len(CHECKS) - len(failed)}/6 项通过，"
                      f"先处理 {'、'.join(c[0] for c in failed)} 再开试用")
    else:
        conclusion = f"就绪：6/6 项通过（server_date={server_date}），可开始教师试用"
    check("⑥ 一句话结论", not failed, conclusion)

    print("-" * 62)
    if failed:
        for name, _, detail in failed:
            print(f"  ✗ {name}  {detail[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
