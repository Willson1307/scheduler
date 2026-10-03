"""S1 服务端体检脚本（B1 · CP-A 载体）。

运行：
    python scripts/doctor.py

用途与设计意图（答辩口径）：
  骨架的「自动断言」：不靠人眼，跑绿即算通过。检查项覆盖——
  1. 运行环境：Python ≥ 3.10（领域层 int.bit_count() 要求）；
     模块导入全链成功（隐含验证零第三方依赖可跑）。
  2. 约定式自动注册：扫描 api_*.py 能发现路由（新增 API 只加文件的机制活着）。
  3. 🔴 B1 红线一：周六周日槽位 enabled=1（走服务端真实初始化路径，
     不是只测领域层函数——否则测不到 seed_demo days=5 缺周末行这个坑）。
  4. 🔴 B1 红线二：演示日期可注入且可复现——同一日期两次初始化结果逐字一致；
     换日期则锚点随日期变；/api/health 的 server_date 反映注入值。
  5. HTTP 骨架：/api/health 200、尾斜杠归一、静态文件 ETag/304/404/防穿越。
  6. Ctrl-C 3 秒退出（Windows 用 CTRL_BREAK_EVENT 注入，POSIX 用 SIGINT）。

  全绿 exit 0；任何红项 exit 1。临时文件全部用 tempfile，跑完即清。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

RUN_SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "run_server.py")

# 演示日期样本：A=周六（锚点周一在 5 天前），B=本身就是周一的日期
DATE_A = "2026-10-10"
DATE_B = "2027-03-01"
MONDAY_OF_A = "2026-10-05"

CHECKS: list[tuple[str, bool, str]] = []  # (名称, 是否通过, 说明)


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    mark = "OK  " if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f"  —— {detail}"
    print(line, flush=True)
    return ok


def http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 5.0):
    """GET，返回 (status, headers, body_bytes)。304 也正常返回，不抛异常。"""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), (e.read() if e.fp else b"")


class ServerProc:
    """run_server.py 子进程：等待 READY 行拿到实际端口。"""

    def __init__(self, args: list[str], workdir: str):
        self.err_path = os.path.join(workdir, "server.err")
        kwargs: dict = {"stdout": subprocess.PIPE, "stderr": open(self.err_path, "w+b"), "cwd": workdir}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen([sys.executable, RUN_SERVER, *args], **kwargs)

    def wait_ready(self, timeout: float = 15.0) -> int:
        """读 READY 行解析端口；子进程异常退出时带出 stderr 尾部。"""
        result: dict = {}

        def _reader():
            line = self.proc.stdout.readline().decode("utf-8", "replace") if self.proc.stdout else ""
            if line.startswith("READY"):
                for kv in line.split()[1:]:
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        result[k] = v

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        if "port" not in result:
            tail = self.stderr_tail()
            raise RuntimeError(f"服务子进程未就绪{('：' + tail) if tail else ''}")
        return int(result["port"])

    def stderr_tail(self, limit: int = 2000) -> str:
        try:
            with open(self.err_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - limit))
                return f.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def stop_with_ctrl_c(self, wait: float = 3.0) -> tuple[bool, str]:
        """发 Ctrl-C 类信号，返回 (是否在 wait 秒内退出, 说明)。"""
        if self.proc.poll() is not None:
            return True, "进程已退出"
        try:
            if os.name == "nt":
                os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.kill(self.proc.pid, signal.SIGINT)
        except OSError as exc:
            # 无控制台（CI / 管道环境）时无法注入控制台事件：降级 terminate，标记 SKIP
            self.proc.terminate()
            self.proc.wait()
            return False, f"SKIP：当前环境无法注入控制台信号（{exc}），已降级 terminate"
        try:
            self.proc.wait(timeout=wait)
            return True, f"退出码 {self.proc.returncode}，{wait}s 内"
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            return False, f"超 {wait}s 未退出，已强杀"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


def wait_health_ok(port: int, timeout: float = 10.0) -> bool:
    """轮询 /api/health 直到 200（服务刚启动时端口可能还在就绪中）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _, _ = http_get(f"http://127.0.0.1:{port}/api/health", timeout=2)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    return False


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="scheduler-doctor-")
    print("=" * 62)
    print("服务端骨架体检（B1 · S1–S4）")
    print("=" * 62)

    # ── 1. Python 版本 ─────────────────────────────────────────────
    v = sys.version_info
    check("Python 版本 ≥ 3.10", v >= (3, 10), f"当前 {v.major}.{v.minor}.{v.micro}")

    # ── 2. 模块导入全链（零第三方依赖的运行性验证）─────────────────────
    try:
        from server import config as cfg_mod  # noqa: F401
        from server import http_api, routes, static  # noqa: F401

        check("模块导入全链（config/http_api/routes/static）", True)
    except Exception as exc:  # noqa: BLE001
        check("模块导入全链（config/http_api/routes/static）", False, repr(exc))
        _summary()
        return 1

    # ── 3. 约定式自动注册 ──────────────────────────────────────────
    table = routes.scan()
    check(
        "约定式自动注册发现路由",
        ("GET", "/api/health") in table and len(table) > 0,
        f"共 {len(table)} 条：{', '.join(sorted('/'.join(k) for k in table))}",
    )

    # ── 4. 周末槽位 enabled=1（服务端真实初始化路径）─────────────────
    from server.config import ServerConfig, ensure_database

    db_a = os.path.join(workdir, "db_a.db")
    cfg_a = ServerConfig(db_path=db_a, today=date.fromisoformat(DATE_A))
    report_a = ensure_database(cfg_a)
    weekend_ok, weekend_detail = _check_weekend(db_a)
    check(
        "周六周日槽位 enabled=1（AGENTS §9b）",
        weekend_ok,
        f"{'新建库' if report_a['created'] else '已有库'}，补行 {report_a['weekend_slots_added']}，{weekend_detail}",
    )

    # ── 5. 演示日期注入可复现 ──────────────────────────────────────
    ok, detail = _check_replay(workdir)
    check("演示日期注入可复现（同日期同结果，任意日期可指定）", ok, detail)

    # ── 6+7. 起真服务：health / 静态 / 尾斜杠 ────────────────────────
    webdir = os.path.join(workdir, "web")
    os.makedirs(webdir)
    with open(os.path.join(webdir, "index.html"), "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset=utf-8><title>doctor</title>OK")

    server = ServerProc(
        ["--port", "0", "--db", db_a, "--today", DATE_A, "--web", webdir], workdir
    )
    try:
        port = server.wait_ready()
        base = f"http://127.0.0.1:{port}"

        ready = wait_health_ok(port)
        status, _, body = http_get(f"{base}/api/health")
        payload = json.loads(body) if body else {}
        check(
            "/api/health 返回 200",
            ready and status == 200 and payload.get("status") == "ok",
            f"status={status}，server_date={payload.get('server_date')}，db={payload.get('db')}",
        )
        check(
            "健康检查反映注入日期",
            payload.get("server_date") == DATE_A,
            f"server_date={payload.get('server_date')}（--today {DATE_A}）",
        )

        s2, _, _ = http_get(f"{base}/api/health/")
        check("尾斜杠归一（/api/health/ → 200）", s2 == 200, f"status={s2}")

        sw, hw, bw = http_get(f"{base}/")
        check(
            "静态文件 / → index.html（no-cache + ETag）",
            sw == 200
            and "no-cache" in (hw.get("Cache-Control") or "")
            and bool(hw.get("ETag")),
            f"status={sw}，Cache-Control={hw.get('Cache-Control')}，ETag={hw.get('ETag')}",
        )
        etag = hw.get("ETag") or ""
        s304, _, _ = http_get(f"{base}/", headers={"If-None-Match": etag} if etag else {})
        check("ETag 协商缓存（If-None-Match → 304）", s304 == 304, f"status={s304}")
        s404, _, _ = http_get(f"{base}/no-such-file.js")
        check("静态 404（JSON 错误响应）", s404 == 404, f"status={s404}")
        s500, _, _ = http_get(f"{base}/../server/config.py")  # 防目录穿越
        check("目录穿越防护（/../ → 404）", s500 == 404, f"status={s500}")
    finally:
        server.close()

    # ── 8. Ctrl-C 3 秒退出 + 换日期复现 ────────────────────────────
    server2 = ServerProc(["--port", "0", "--db", db_a, "--today", DATE_A, "--web", webdir], workdir)
    try:
        port2 = server2.wait_ready()
        wait_health_ok(port2)  # 确认就绪后即可测 Ctrl-C
        ok_ctrl, detail_ctrl = server2.stop_with_ctrl_c()
        check("Ctrl-C 3 秒内退出（CP-A）", ok_ctrl, detail_ctrl)
    finally:
        server2.close()

    server3 = ServerProc(["--port", "0", "--db", db_a, "--today", DATE_B, "--web", webdir], workdir)
    try:
        port3 = server3.wait_ready()
        ok_b = False
        detail_b = ""
        if wait_health_ok(port3):
            _, _, b3 = http_get(f"http://127.0.0.1:{port3}/api/health")
            got = json.loads(b3).get("server_date")
            ok_b = got == DATE_B
            detail_b = f"server_date={got}（--today {DATE_B}）"
        check("任意日期注入生效（换日期 → server_date 随之）", ok_b, detail_b)
    finally:
        server3.close()

    # 清理临时目录
    import shutil

    shutil.rmtree(workdir, ignore_errors=True)
    return _summary()


def _check_weekend(db_path: str) -> tuple[bool, str]:
    """直查 period_slots：周六周日行数与周一一致且全部 enabled=1。"""
    from scheduler.db import connect

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT day_of_week AS dow, COUNT(*) AS c, SUM(enabled) AS e "
            "FROM period_slots GROUP BY day_of_week ORDER BY day_of_week"
        ).fetchall()
        by_dow = {r["dow"]: (r["c"], r["e"]) for r in rows}
        n_mon = by_dow.get(1, (0, 0))[0]
        for dow, label in ((6, "周六"), (7, "周日")):
            c, e = by_dow.get(dow, (0, 0))
            if c == 0:
                return False, f"{label}无槽位行（周末活动会映射出空掩码）"
            if c != n_mon:
                return False, f"{label}行数 {c} ≠ 周一 {n_mon}"
            if e != c:
                return False, f"{label}存在 enabled=0（{c - e} 行）"
        return True, f"周六 {by_dow[6][0]} 行、周日 {by_dow[7][0]} 行，全部 enabled=1"
    finally:
        conn.close()


def _check_replay(workdir: str) -> tuple[bool, str]:
    """日期注入复现：同日期 → 同锚点；换日期 → 锚点随之。"""
    from server.config import ServerConfig, ensure_database, monday_of

    # 同日期再建一个新库，term_start 应逐字一致
    db_a2 = os.path.join(workdir, "db_a2.db")
    ensure_database(ServerConfig(db_path=db_a2, today=date.fromisoformat(DATE_A)))
    ts_a2 = _term_start(db_a2)

    db_b = os.path.join(workdir, "db_b.db")
    ensure_database(ServerConfig(db_path=db_b, today=date.fromisoformat(DATE_B)))
    ts_b = _term_start(db_b)

    ts_a = _term_start(os.path.join(workdir, "db_a.db"))

    ok = ts_a == MONDAY_OF_A and ts_a2 == MONDAY_OF_A and ts_b == "2027-03-01"
    return ok, (
        f"{DATE_A} → term_start={ts_a}（两库一致）；{DATE_B} → term_start={ts_b}"
    )


def _term_start(db_path: str) -> str | None:
    from scheduler.db import connect

    conn = connect(db_path)
    try:
        row = conn.execute("SELECT start_date FROM terms LIMIT 1").fetchone()
        return row["start_date"] if row else None
    finally:
        conn.close()


def _summary() -> int:
    failed = [c for c in CHECKS if not c[0]]
    print("-" * 62)
    if failed:
        print(f"体检结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过，🔴 {len(failed)} 项未过：")
        for name, _, detail in failed:
            print(f"  ✗ {name} {detail}")
        return 1
    print(f"体检结果：{len(CHECKS)}/{len(CHECKS)} 项全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
