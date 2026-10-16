"""F6 验收脚本：删 data/ → 一条命令 → 30 秒内可演示（CP-G 判据的可执行形态）。

运行：
    python deploy/acceptance.py              # 默认：备份并清空 data/，跑一条命令，验完恢复
    python deploy/acceptance.py --keep-data  # 不动 data/（只验证一条命令能起来）

判据（对齐任务卡 F6 与 docs/19 CP-G「删 data/ → 一条命令 → 30 秒可演示」）：
  1. data/ 清空后，`deploy/start.py` 一条命令能起来，并重新生成 data/demo.db；
  2. **30 秒内** GET /api/health 返回 200 且 ok=true（硬上限，超时即 FAIL）；
  3. GET / 返回 200 且 Content-Type 为 text/html（「页面可打开」）+ GET /app.js 200；
  4. 补三项防「假绿」：POST /api/login 200 拿得到 token、GET /api/conflicts 是
     23 处 / 3 组、POST /api/resolve 可达。

   第 4 条不是加戏。docs/17 §7 的事故里 health / doctor / demo_cli **全绿**，
   但 main 上 /api/resolve 是 404 —— 只验 health 会漏掉「接口不在这个分支上」。

data/ 安全（本脚本会真的删 data/，所以先说清保护措施）：
   真实 data/ 里有教师试用库 trial.db（脱敏数据，但已投入试用）与 scrub.salt。
   本脚本先把 data/ 中除 demo.db 系列以外的文件复制到临时备份目录，**确认备份成功
   之后**才删除 data/；验收结束再把备份恢复回去。备份失败即中止，绝不先删后想。
   演示库 demo.db 本身属可再生产物（冷启动重造），不在备份范围。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = os.path.join(REPO_ROOT, "deploy", "start.py")
DATA_DIR = os.path.join(REPO_ROOT, "data")
DEMO_DB = os.path.join(DATA_DIR, "demo.db")

DEMO_TODAY = "2026-09-08"
DEADLINE_SECONDS = 30.0

#: seed_demo 建的默认管理员（与 scripts/smoke.py、tests/test_server_api.py 一致）
ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "admin123"

#: 演示库口径（students_per_class=10 → 30 名学生 → 23 处原始冲突 / 3 个冲突组）
RAW_COUNT_EXPECT = 23
COUNT_EXPECT = 3

#: demo.db 系列由冷启动重新生成，不在备份范围
_DEMO_FILES = {"demo.db", "demo.db-wal", "demo.db-shm"}

#: 绕过系统代理：否则代理会拦 127.0.0.1，把「服务未就绪」误判成「服务异常」
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    line = f"[{'OK  ' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  —— {detail}"
    print(line, flush=True)
    return ok


def request(base: str, method: str, path: str, body=None, token: str | None = None,
            timeout: float = 10.0):
    """发一个 JSON HTTP 请求，返回 (status, 解析后的 body)。4xx/5xx 也解析不抛。"""
    url = base + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read()), dict(e.headers)
    except (urllib.error.URLError, OSError) as e:
        return None, {"_unreachable": repr(e)}, {}


def _parse(data: bytes):
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


# ─────────────────────────── data/ 备份与恢复 ───────────────────────────

def backup_data_dir(dest: str) -> list[str]:
    """把 data/ 中需保留的文件（demo.db 系列除外）复制到 dest，返回文件名清单。"""
    kept: list[str] = []
    if not os.path.isdir(DATA_DIR):
        return kept
    for name in sorted(os.listdir(DATA_DIR)):
        if name in _DEMO_FILES:
            continue
        src = os.path.join(DATA_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, name))
            kept.append(name)
    return kept


def restore_data_dir(backup: str, names: list[str]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    for name in names:
        shutil.copy2(os.path.join(backup, name), os.path.join(DATA_DIR, name))


# ─────────────────────────── 起服务 / 收进程 ───────────────────────────

def _spawn(workdir: str) -> subprocess.Popen:
    """起「一条命令」。stdout 走管道以解析 READY 行，stderr 落文件便于报错。"""
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": open(os.path.join(workdir, "acceptance.err"), "w+b"),  # noqa: SIM115
        "cwd": REPO_ROOT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    args = [
        sys.executable, START,
        "--host", "127.0.0.1",
        "--port", "0",
        "--today", DEMO_TODAY,
    ]
    return subprocess.Popen(args, **kwargs)


def _read_ready(proc: subprocess.Popen, workdir: str, timeout: float) -> int | None:
    """逐行读到 READY 行拿实际端口（--port 0 时端口随机）。

    ⚠️ 必须循环读，不能只读第一行：start.py 先打两行 INFO（冷启动命令 /
    自动初始化说明），READY 由子进程 run_server.py 打出，不是第一行。
    """
    import threading

    result: dict = {}

    def _reader():
        if not proc.stdout:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                return  # 管道关闭：子进程已退出
            if line.decode("utf-8", "replace").startswith("READY"):
                for kv in line.decode("utf-8", "replace").split()[1:]:
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        result[k] = v
                return

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout)
    if "port" not in result:
        tail = _err_tail(workdir)
        print(f"ERROR 服务未就绪{('：' + tail) if tail else ''}", flush=True)
        return None
    return int(result["port"])


def _err_tail(workdir: str, limit: int = 2000) -> str:
    path = os.path.join(workdir, "acceptance.err")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _kill_tree(proc: subprocess.Popen) -> None:
    """一条命令会派生子进程（start.py → run_server.py），必须整棵树收掉。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def wait_health(base: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body, _ = request(base, "GET", "/api/health", timeout=3)
        if status == 200 and isinstance(body, dict) and body.get("ok") is True:
            return True
        time.sleep(0.2)
    return False


# ─────────────────────────────── 主流程 ───────────────────────────────

def _summary() -> int:
    failed = [c for c in CHECKS if not c[1]]
    print("-" * 62)
    if failed:
        print(f"验收结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过，🔴 {len(failed)} 项未过：")
        for name, _, detail in failed:
            print(f"  ✗ {name} {detail}")
        return 1
    print(f"验收结果：{len(CHECKS)}/{len(CHECKS)} 项全部通过 ✅")
    return 0


def _run(keep_data: bool) -> int:
    print("=" * 62)
    print("F6 验收：删 data/ → 一条命令 → 30 秒可演示")
    print("=" * 62)

    backup = tempfile.mkdtemp(prefix="scheduler-acceptance-")
    kept: list[str] = []

    if keep_data:
        print(f"INFO --keep-data：不动 {DATA_DIR}（仅验证一条命令可起来）", flush=True)
    elif os.path.isdir(DATA_DIR):
        print(f"INFO 备份需保留的文件 → {backup}", flush=True)
        kept = backup_data_dir(backup)
        print(f"INFO 保留 {len(kept)} 个：{kept or '（无，无需保留）'}", flush=True)
        shutil.rmtree(DATA_DIR)
        print(f"INFO 已删除 {DATA_DIR}", flush=True)
    else:
        print(f"INFO data/ 本就不存在：{DATA_DIR}", flush=True)

    proc = None
    try:
        if not check("data/ 已清空（或本不存在）", not os.path.exists(DATA_DIR), DATA_DIR):
            return _summary()

        workdir = tempfile.mkdtemp(prefix="scheduler-acceptance-log-")
        t0 = time.monotonic()
        proc = _spawn(workdir)

        port = _read_ready(proc, workdir, timeout=min(DEADLINE_SECONDS, 15.0))
        if port is None:
            check("一条命令启动并打印 READY", False, "见上方 stderr 尾部")
            return _summary()

        base = f"http://127.0.0.1:{port}"
        check("一条命令启动并打印 READY", True, f"port={port}")

        ready = wait_health(base, timeout=max(0.0, DEADLINE_SECONDS - (time.monotonic() - t0)))
        elapsed = time.monotonic() - t0
        check(
            f"{DEADLINE_SECONDS:.0f} 秒内 /api/health 200（实测 {elapsed:.1f}s）",
            ready and elapsed <= DEADLINE_SECONDS,
            f"elapsed={elapsed:.2f}s",
        )
        if not ready:
            return _summary()

        st, body, _ = request(base, "GET", "/api/health")
        check(
            "health 契约（200 / ok=true / db=ok / server_date=2026-09-08）",
            st == 200 and isinstance(body, dict) and body.get("ok") is True
            and body.get("db") == "ok" and body.get("server_date") == DEMO_TODAY,
            f"status={st}，body={ {k: body.get(k) for k in ('ok', 'status', 'server_date', 'db')} if isinstance(body, dict) else body }",
        )

        check("冷启动重建了演示库 data/demo.db", os.path.exists(DEMO_DB), DEMO_DB)

        # 页面可打开：/ 返回 HTML，app.js 也能取到（不只是 health 活着）
        st, _, headers = request(base, "GET", "/")
        ctype = (headers.get("Content-Type") or "").lower()
        check("页面可打开（GET / 200 且 Content-Type=text/html）",
              st == 200 and "text/html" in ctype, f"status={st}，ct={ctype or '-'}")
        st_js, _, headers_js = request(base, "GET", "/app.js")
        check("静态资源可达（GET /app.js 200）", st_js == 200,
              f"status={st_js}，ct={(headers_js.get('Content-Type') or '-')}")

        # 防「假绿」三连
        st, body, _ = request(base, "POST", "/api/login",
                              {"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
        token = body.get("token") if isinstance(body, dict) else None
        leaked = isinstance(body, dict) and ("password_hash" in body or "password" in body)
        check(f"演示账号可登录（POST /api/login 200 且不泄口令）",
              st == 200 and bool(token) and not leaked, f"status={st}")
        if not token:
            return _summary()

        st, body, _ = request(base, "GET", "/api/conflicts", token=token)
        raw = body.get("raw_count") if isinstance(body, dict) else None
        cnt = body.get("count") if isinstance(body, dict) else None
        check(f"冲突口径（raw_count={RAW_COUNT_EXPECT} / count={COUNT_EXPECT}）",
              st == 200 and raw == RAW_COUNT_EXPECT and cnt == COUNT_EXPECT,
              f"status={st}，raw_count={raw}，count={cnt}")
        if raw != RAW_COUNT_EXPECT:
            return _summary()

        st, body, _ = request(base, "POST", "/api/resolve", {}, token=token)
        rb = body.get("remaining_before") if isinstance(body, dict) else None
        ra = body.get("remaining_after") if isinstance(body, dict) else None
        check("业务路由真在（POST /api/resolve 200，23 → 0）",
              st == 200 and rb == RAW_COUNT_EXPECT and ra == 0,
              f"status={st}，{rb} → {ra}")

        return _summary()
    finally:
        if proc is not None:
            _kill_tree(proc)
        if kept:
            restore_data_dir(backup, kept)
            print(f"INFO 已恢复 data/ 中的 {len(kept)} 个文件：{kept}", flush=True)
        shutil.rmtree(backup, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="F6 验收：删 data/ → 一条命令 → 30 秒内 /api/health 200 且页面可打开",
    )
    parser.add_argument("--keep-data", action="store_true",
                        help="不删 data/（只验证一条命令能起来；不碰现场试用数据）")
    args = parser.parse_args(argv)
    return _run(args.keep_data)


if __name__ == "__main__":
    sys.exit(main())
