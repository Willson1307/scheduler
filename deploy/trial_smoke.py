"""trial_smoke.py —— 服务器侧「试用就绪」验收脚本（TRIAL-SYNC-2）。

在服务器上跑一条命令，证明「服务器上是新代码 + 试用库是新的 + admin 范围已收敛 +
埋点能落库 + 问卷工具在」：

    python3 deploy/trial_smoke.py [--db data/trial.db] [--live-port 8000]

判据全部写成可粘贴命令 + 期望数值（不接受「验证通过」这类描述）。全部在**临时副本**
上起临时服务验证，不动线上 trial.db；只对线上服务做只读 health 探测。

期望数值（2026-10-23 实测锁定，与 build_trial_org.py --per-teacher + TERM_START 对齐）：
    有授权   admin /api/groups = 37
             t01   /api/groups = 2
             t01   /api/conflicts count=5 raw_count=30
    无授权   admin /api/groups = 1
             跨组织 /api/objects = 403

只用 Python 标准库。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

RUN_SERVER = os.path.join(REPO, "scripts", "run_server.py")

ADMIN_LOGIN = "trial_admin"
ADMIN_PASSWORD = "trial123"
T01_LOGIN = "trial_t01"
T01_SMOKE_PASSWORD = "smoke-t01-2026"  # 只在临时副本上临时覆盖，线上库不动

EXPECT_ADMIN_GROUPS = 37
EXPECT_T01_GROUPS = 2
EXPECT_T01_CONFLICT_COUNT = 5
EXPECT_T01_RAW_COUNT = 30
EXPECT_NOAUTH_ADMIN_GROUPS = 1

CHECKS: list[tuple[str, bool, str]] = []

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    mark = "OK  " if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f"  —— {detail}"
    print(line, flush=True)
    return ok


def request(base: str, method: str, path: str, body=None, token=None, timeout=8.0):
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
    st, body = request(base, "POST", "/api/login", {"login": login_name, "password": password})
    if st == 200 and isinstance(body, dict) and body.get("token"):
        return body["token"]
    return None


class Server:
    def __init__(self, db_path: str, today: str):
        self.err = tempfile.NamedTemporaryFile(prefix="smoke-", suffix=".err", delete=False)
        self.proc = subprocess.Popen(
            [sys.executable, RUN_SERVER, "--host", "127.0.0.1", "--port", "0",
             "--today", today, "--db", db_path],
            stdout=subprocess.PIPE, stderr=self.err, text=True,
        )
        self.port: int | None = None

    def wait_ready(self, timeout: float = 20.0) -> int:
        result: dict = {}

        def _reader():
            line = self.proc.stdout.readline()
            if line.startswith("READY"):
                for kv in line.split()[1:]:
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        result[k] = v

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        if "port" not in result:
            self.err.flush()
            tail = open(self.err.name, encoding="utf-8", errors="replace").read()[-1500:]
            raise RuntimeError(f"服务未就绪：{tail}")
        self.port = int(result["port"])
        return self.port

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.err.close()
        try:
            os.unlink(self.err.name)
        except OSError:
            pass


def _term_start(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT MIN(start_date) AS d FROM terms").fetchone()
        return row[0] if row and row[0] else "2026-10-12"
    finally:
        conn.close()


def _reset_t01_password(db_path: str) -> None:
    from scheduler.object_graph import hash_password
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE login = ?",
            (hash_password(T01_SMOKE_PASSWORD), T01_LOGIN),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"临时副本重置 t01 口令失败，影响行数 {cur.rowcount}")
        conn.commit()
    finally:
        conn.close()


def _delete_admin_grants(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        uid = conn.execute("SELECT id FROM users WHERE login = ?", (ADMIN_LOGIN,)).fetchone()
        if uid is None:
            raise RuntimeError("临时副本缺 trial_admin")
        n = conn.execute("DELETE FROM group_roles WHERE user_id = ?", (uid[0],)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _event_log_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="试用就绪验收")
    ap.add_argument("--db", default=os.path.join(REPO, "data", "trial.db"))
    ap.add_argument("--live-port", type=int, default=8000)
    args = ap.parse_args()

    print("=" * 62)
    print("试用就绪验收（trial_smoke）")
    print("=" * 62)

    # ── 1. 新代码在位（静态检查）───────────────────────────────
    authz_path = os.path.join(REPO, "src", "scheduler", "authz.py")
    with open(authz_path, encoding="utf-8") as f:
        n_marker = f.read().count("本组织短路")
    check("CP-H-S2 修复在位（authz.py 含「本组织短路」）", n_marker >= 1, f"命中 {n_marker} 处（旧代码 0）")

    survey = os.path.join(REPO, "scripts", "import_survey.py")
    check("问卷工具在位（scripts/import_survey.py）", os.path.exists(survey),
          "存在" if os.path.exists(survey) else "缺失")

    # ── 2. 线上服务健康（只读）──────────────────────────────────
    live = f"http://127.0.0.1:{args.live_port}"
    st, body = request(live, "GET", "/api/health")
    check("线上服务 /api/health 200", st == 200 and isinstance(body, dict) and body.get("status") == "ok",
          f"status={st}，server_date={body.get('server_date') if isinstance(body, dict) else '-'}")

    if not os.path.exists(args.db):
        check("trial.db 存在", False, args.db)
        return _summary()
    check("trial.db 存在", True, args.db)

    today = _term_start(args.db)
    print(f"[info] 学期起点（--today 用）= {today}", flush=True)

    # ── 3. 有授权副本：admin 37 组 / t01 2 组 / 冲突 5/30 / 埋点落库 ──
    workdir = tempfile.mkdtemp(prefix="trial-smoke-")
    try:
        copy_ok = os.path.join(workdir, "ok.db")
        shutil.copy2(args.db, copy_ok)
        _reset_t01_password(copy_ok)

        srv = Server(copy_ok, today)
        try:
            port = srv.wait_ready()
            base = f"http://127.0.0.1:{port}"

            at = login(base, ADMIN_LOGIN, ADMIN_PASSWORD)
            check("admin 登录成功（trial_admin/trial123）", at is not None, "有 token" if at else "无 token")
            if at:
                st, g = request(base, "GET", "/api/groups", token=at)
                n = len(g) if isinstance(g, list) else -1
                check(f"admin /api/groups = {EXPECT_ADMIN_GROUPS}（有授权）", st == 200 and n == EXPECT_ADMIN_GROUPS,
                      f"status={st}，组数={n}")

            tt = login(base, T01_LOGIN, T01_SMOKE_PASSWORD)
            check("t01 登录成功（临时副本已知口令）", tt is not None, "有 token" if tt else "无 token")
            if tt:
                st, g = request(base, "GET", "/api/groups", token=tt)
                n = len(g) if isinstance(g, list) else -1
                check(f"t01 /api/groups = {EXPECT_T01_GROUPS}", st == 200 and n == EXPECT_T01_GROUPS,
                      f"status={st}，组数={n}")

                st, c = request(base, "GET", "/api/conflicts", token=tt)
                cnt = c.get("count") if isinstance(c, dict) else -1
                raw = c.get("raw_count") if isinstance(c, dict) else -1
                check(f"t01 /api/conflicts count={EXPECT_T01_CONFLICT_COUNT} raw_count={EXPECT_T01_RAW_COUNT}",
                      st == 200 and cnt == EXPECT_T01_CONFLICT_COUNT and raw == EXPECT_T01_RAW_COUNT,
                      f"status={st}，count={cnt}，raw_count={raw}")

                # 埋点落库：ping 一次，event_log 应 +1 且 org_kind=trial
                before = _event_log_count(copy_ok)
                st, _ = request(base, "POST", "/api/ping",
                                {"event": "task_start", "session_id": "smoke-verify-1",
                                 "payload": {"score": 5}}, token=tt)
                after = _event_log_count(copy_ok)
                conn = sqlite3.connect(copy_ok)
                last_kind = conn.execute(
                    "SELECT org_kind FROM event_log WHERE session_id='smoke-verify-1' ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                conn.close()
                check("埋点落库（ping → event_log +1，org_kind=trial）",
                      st == 200 and after == before + 1 and last_kind and last_kind[0] == "trial",
                      f"status={st}，event_log {before}→{after}，org_kind={last_kind[0] if last_kind else '-'}")
        finally:
            srv.close()

        # ── 4. 无授权副本：admin 1 组 / 跨组织 403 ────────────────
        copy_noauth = os.path.join(workdir, "noauth.db")
        shutil.copy2(args.db, copy_noauth)
        n_del = _delete_admin_grants(copy_noauth)

        srv2 = Server(copy_noauth, today)
        try:
            port2 = srv2.wait_ready()
            base2 = f"http://127.0.0.1:{port2}"
            at2 = login(base2, ADMIN_LOGIN, ADMIN_PASSWORD)
            check("无授权副本 admin 登录成功", at2 is not None, f"已删 {n_del} 条授权")
            if at2:
                st, g = request(base2, "GET", "/api/groups", token=at2)
                n = len(g) if isinstance(g, list) else -1
                check(f"无授权副本 admin /api/groups = {EXPECT_NOAUTH_ADMIN_GROUPS}",
                      st == 200 and n == EXPECT_NOAUTH_ADMIN_GROUPS, f"status={st}，组数={n}")

                # 跨组织 group：取 t01 所属组织的班级组，admin（无授权）访问应 403
                conn = sqlite3.connect(copy_noauth)
                cross = conn.execute(
                    "SELECT g.id FROM objects g JOIN objects u ON g.org_id = u.org_id "
                    "WHERE u.id = (SELECT object_id FROM users WHERE login=?) "
                    "AND g.kind='group' AND g.is_root=0 LIMIT 1",
                    (T01_LOGIN,),
                ).fetchone()
                conn.close()
                if cross:
                    st, _ = request(base2, "GET", "/api/objects?group=" + cross[0], token=at2)
                    check("跨组织 /api/objects = 403（无授权）", st == 403, f"status={st}")
                else:
                    check("跨组织 /api/objects = 403（无授权）", False, "未找到跨组织组 id")
        finally:
            srv2.close()

        # ── 5. 线上库干净（event_log=0）────────────────────────
        n = _event_log_count(args.db)
        check("线上 trial.db event_log = 0（试用数据从零采）", n == 0, f"event_log={n}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return _summary()


def _summary() -> int:
    failed = [c for c in CHECKS if not c[1]]
    print("-" * 62)
    if failed:
        print(f"验收结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过，🔴 {len(failed)} 项未过：")
        for name, _, detail in failed:
            print(f"  ✗ {name}  {detail}")
        return 1
    print(f"验收结果：{len(CHECKS)}/{len(CHECKS)} 项全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
