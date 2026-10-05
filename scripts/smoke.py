"""scripts/smoke.py —— 端到端自检（降级链 L1 层，对应 docs/20 §1）。

运行：
    python scripts/smoke.py                 # 常规端到端自检（8 项）
    python scripts/smoke.py --offline       # 断网演练：拦截一切非本机连接，断言 0 次外联
    python scripts/smoke.py --offline-probe # 仅跑拦截探针：证明守护真的会拦（不真发请求）

断网演练口径（如实，不夸大）：
  --offline 统计的是**本脚本自身进程**发起的对外连接（getaddrinfo / create_connection /
  socket.connect 三个 C 层入口全部拦截）。子进程 run_server.py 是独立进程、不受本进程
  monkeypatch 影响（它只 bind 127.0.0.1 + sqlite3，不主动外联），**不**计入本次统计，
  故本脚本不宣称「整机零外联」。对应 docs/21 §2 R2「断网可跑」。

用途与设计意图（答辩口径）：
  1. 网页层（L0）失败时的第一道兜底：一条命令跑完整流程——
     「建库 → 起服务 → 四个接口各调一次 → 一键消解 → 复扫归零」，
     每一步都有明确判据；全绿 exit 0，任一 FAIL exit 1。
  2. 只依赖后端 HTTP 接口，不依赖浏览器 / 前端 / 领域层直调；
     L1 跑通说明领域层 + 接口层都正常，问题在静态资源或前端。
  3. 固定 --today 2026-09-08（学期第 1 周周一 2026-09-07 所在周），
     保证 23 处冲突可复现（🔴 不带 --today 会落到第 5 周只扫到 1 处，
     且不报错——docs/17 §1 的坑）。
  4. 临时库落在 tempfile 目录，由服务端 ensure_database() 自动 seed，
     自包含、可复现、不污染 data/demo.db。
  5. 全程只用 Python 标准库（subprocess 起服务 + urllib 打请求），断网可跑。

复用：起服务 / 等 READY / 等 health 的模式来自 scripts/doctor.py；
      HTTP 客户端绕过系统代理的模式来自 tests/test_contract.py。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# 仓库根锚定（scripts/ 的上一级），不依赖 cwd
RUN_SERVER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "run_server.py"
)

# 演示日期：monday_of(2026-09-08) = 2026-09-07，23 处冲突全在第 1 周。
# 🔴 固定 TODAY 的理由是「输出逐字一致、可复现」（server_date / window / days
#    全由它决定），**不是**「避免第 5 周只有 1 处冲突」——那个坑只在复用
#    data/demo.db（term_start 固定 2026-09-07）时发生；本脚本每次用全新临时库，
#    ensure_database() 把 term_start 锚定 monday_of(today)，第 1 周永远 23 处，
#    天然规避 docs/17 §1 的坑。
TODAY = "2026-09-08"
# 消解前的冲突口径（演示库种子规模 students_per_class=10 决定，写进 README/报名材料）
RAW_COUNT_EXPECT = 23
COUNT_EXPECT = 3  # 聚合后的冲突组数（1 教师 + 两个班）

# 登录凭证（seed_demo 建的默认管理员，与 tests/test_contract.py 一致）
ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "admin123"

CHECKS: list[tuple[str, bool, str]] = []  # (名称, 是否通过, 说明)

# 绕过系统代理：否则代理会拦 127.0.0.1 返回 502，把「服务未启动」误判成「服务异常」
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# --offline 拦截到的「非本机连接」目标（自检进程口径，见文件头说明）
OFFLINE_ATTEMPTS: list[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    mark = "OK  " if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f"  —— {detail}"
    print(line, flush=True)
    return ok


def _parse(data: bytes):
    """把响应体解析成 JSON（dict / list / None）；空体或非 JSON 返回 None。"""
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def request(base: str, method: str, path: str, body=None, token: str | None = None,
            timeout: float = 10.0):
    """发一个 JSON HTTP 请求，返回 (status, 解析后的 body)。

    - body 为 dict 时以 JSON 编码；token 非空时加 Authorization 头。
    - HTTPError（4xx/5xx）也解析 body 返回，不抛异常。
    - 连接拒绝 / 超时（URLError / OSError）→ 返回 (None, {"_unreachable": ...})。
    """
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
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except (urllib.error.URLError, OSError) as e:
        return None, {"_unreachable": repr(e)}


class ServerProc:
    """run_server.py 子进程：读 READY 行拿到实际端口（--port 0 时端口随机）。"""

    def __init__(self, args: list[str], workdir: str):
        self.err_path = os.path.join(workdir, "server.err")
        kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": open(self.err_path, "w+b"),
            "cwd": workdir,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen([sys.executable, RUN_SERVER, *args], **kwargs)

    def wait_ready(self, timeout: float = 20.0) -> int:
        """读 READY 行解析端口；子进程异常退出时带出 stderr 尾部。"""
        result: dict = {}

        def _reader():
            if self.proc.stdout:
                line = self.proc.stdout.readline().decode("utf-8", "replace")
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

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


def wait_health_ok(base: str, timeout: float = 15.0) -> bool:
    """轮询 /api/health 直到 200（服务刚起时端口可能还在就绪中）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = request(base, "GET", "/api/health", timeout=3)
        if status == 200 and isinstance(body, dict) and body.get("ok") is True:
            return True
        time.sleep(0.2)
    return False


def _summary() -> int:
    failed = [c for c in CHECKS if not c[1]]
    print("-" * 62)
    if failed:
        print(f"自检结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过，"
              f"🔴 {len(failed)} 项未过：")
        for name, _, detail in failed:
            print(f"  ✗ {name} {detail}")
        return 1
    print(f"自检结果：{len(CHECKS)}/{len(CHECKS)} 项全部通过 ✅")
    return 0


# ─────────────────────── 断网演练（--offline）───────────────────────
#
# 拦截点必须三层（缺一不能拦住域名外发）：
#   urllib.request → http.client.HTTPConnection.connect → self._create_connection
#     → socket.create_connection → socket.getaddrinfo（DNS 就在这里）→ sock.connect(ip)
# 只拦 socket.connect 时 DNS 已真实发出；只拦 getaddrinfo 又拦不住字面 IP 直连。

_LOOPBACK_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost")


def _is_loopback(host: object) -> bool:
    """本机地址判定：None（AI_PASSIVE 绑定语义）、localhost、127.x、::1、[::1]。"""
    if host is None:
        return True
    if not isinstance(host, str):
        return False
    h = host.strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h.lower() in _LOOPBACK_NAMES or h == "::1":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False  # 域名 / 内网 IP / 0.0.0.0 → 按外联处理（安全默认）


def _block_offline(target: object) -> None:
    OFFLINE_ATTEMPTS.append(target if isinstance(target, tuple) else (target,))
    raise OSError(f"断网模式：已拦截对非本机地址 {target!r} 的连接")


def _install_offline_guard() -> None:
    """monkeypatch 三个 C 层入口，非本机一律拦下（抛 OSError）。

    注：socket.socket 是 socket.py 里的 Python 堆类，可安全赋 connect；
    不要 patch _socket.socket（C 类型，不可赋值）。
    """
    orig_gai = socket.getaddrinfo
    orig_cc = socket.create_connection
    orig_conn = socket.socket.connect

    def guard_gai(host, port, *args, **kwargs):
        if _is_loopback(host):
            return orig_gai(host, port, *args, **kwargs)
        _block_offline((host, port))

    def guard_cc(address, *args, **kwargs):
        if isinstance(address, tuple) and _is_loopback(address[0]):
            return orig_cc(address, *args, **kwargs)
        _block_offline(address)

    def guard_conn(self, address):
        if isinstance(address, tuple) and _is_loopback(address[0]):
            return orig_conn(self, address)
        _block_offline(address)

    socket.getaddrinfo = guard_gai
    socket.create_connection = guard_cc
    socket.socket.connect = guard_conn


def _offline_probe() -> int:
    """跑 3 条拦截自检，证明守护真的生效；均不真正发出请求（守护先于请求安装）。

    判定依据是 OFFLINE_ATTEMPTS 计数增量，而非「是否抛 OSError」——否则真实的
    网络不可达也会抛 OSError，会被误当成「已拦截」（假阳性）。
    """
    print("-" * 62)
    print("断网演练探针（--offline-probe）：验证拦截生效，均不真正发出请求")
    cases: list[tuple[str, object]] = [
        ("socket.create_connection(('example.com', 80))",
         lambda: socket.create_connection(("example.com", 80), timeout=5)),
        ("socket.connect(('192.0.2.1', 80))  # RFC5737 保留址，永不可路由",
         lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("192.0.2.1", 80))),
        ("socket.getaddrinfo('example.com', 80)",
         lambda: socket.getaddrinfo("example.com", 80)),
    ]
    ok = 0
    for label, call in cases:
        before = len(OFFLINE_ATTEMPTS)
        try:
            call()  # type: ignore[operator]
        except OSError as exc:
            if len(OFFLINE_ATTEMPTS) > before:      # 确由守护拦下，而非真实网络错误
                ok += 1
                print(f"[OK  ] {label} 已拦截 —— {exc}", flush=True)
            else:
                print(f"[FAIL] {label} 抛错但非守护所拦（疑似真实网络错误）🔴 {exc}", flush=True)
        else:
            print(f"[FAIL] {label} 未被拦截 🔴", flush=True)
    print(
        f"拦截探针：{ok}/{len(cases)} 通过 {'✅' if ok == len(cases) else '🔴'}"
        f"（共拦截 {len(OFFLINE_ATTEMPTS)} 次外部尝试，0 次真正发出）",
        flush=True,
    )
    return 0 if ok == len(cases) else 1


def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="smoke.py",
        description="端到端自检（L1）；--offline 为断网演练（docs/21 §2 R2）",
    )
    parser.add_argument("--offline", action="store_true",
                        help="断网演练：拦截一切非本机连接，断言本进程 0 次外联")
    parser.add_argument("--offline-probe", action="store_true",
                        help="仅运行拦截探针后退出（自动启用断网守护）")
    return parser.parse_args(argv)


def _run() -> int:
    workdir = tempfile.mkdtemp(prefix="scheduler-smoke-")
    db_path = os.path.join(workdir, "smoke.db")

    print("=" * 62)
    print("端到端自检（L1）")
    print("建库 → 起服务 → 四接口 → 一键消解 → 复扫归零")
    print("=" * 62)

    server = ServerProc(["--port", "0", "--db", db_path, "--today", TODAY], workdir)
    try:
        # ── 1. 起服务（建库 + seed 由服务端自动完成）───────────────────
        try:
            port = server.wait_ready()
        except RuntimeError as exc:
            check("服务启动就绪（READY 行 + /api/health 200）", False, str(exc))
            return _summary()

        base = f"http://127.0.0.1:{port}"
        ready = wait_health_ok(base)
        st, body = request(base, "GET", "/api/health")
        h_ok = ready and st == 200 and isinstance(body, dict) and body.get("ok") is True
        check(
            "服务启动就绪（/api/health 200 且 ok=true）",
            h_ok,
            f"port={port}，status={st}，server_date={body.get('server_date') if isinstance(body, dict) else None}",
        )
        if not h_ok:
            return _summary()

        # ── 2. 登录闭环（拿 token + default_group_id）───────────────────
        st, body = request(base, "POST", "/api/login",
                           {"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
        token = body.get("token") if isinstance(body, dict) else None
        default_gid = body.get("default_group_id") if isinstance(body, dict) else None
        leaked = isinstance(body, dict) and ("password_hash" in body
                                             or "password" in body)
        check(
            "登录闭环（POST /api/login 200 含 token，不泄 password_hash）",
            st == 200 and isinstance(body, dict) and bool(token) and not leaked,
            f"status={st}，default_group_id={default_gid}",
        )
        if not token:
            return _summary()

        # ── 3. 组列表（唯一要求登录的接口，验证 require_auth + token 生效）──
        st, body = request(base, "GET", "/api/groups", token=token)
        groups_ok = (
            st == 200
            and isinstance(body, list)
            and len(body) > 0
            and all(isinstance(g, dict) and g.get("group_id") for g in body)
        )
        check(
            "组列表（GET /api/groups 200，非空，每项含 group_id）",
            groups_ok,
            f"status={st}，组数={len(body) if isinstance(body, list) else '-'}",
        )

        # ── 4. 对象列表 ─────────────────────────────────────────────
        st, body = request(base, "GET", "/api/objects", token=token)
        kinds = {o.get("kind") for o in body} if isinstance(body, list) else set()
        objects_ok = (
            st == 200
            and isinstance(body, list)
            and len(body) > 0
            and kinds <= {"group", "user", "resource"}
        )
        check(
            "对象列表（GET /api/objects 200，非空，kind 三值内）",
            objects_ok,
            f"status={st}，对象数={len(body) if isinstance(body, list) else '-'}",
        )

        # ── 5. 周视图 ─────────────────────────────────────────────
        st, body = request(base, "GET", "/api/schedules", token=token)
        days = body.get("days") if isinstance(body, dict) else None
        grid = body.get("grid") if isinstance(body, dict) else None
        sched_ok = (
            st == 200
            and isinstance(body, dict)
            and isinstance(body.get("objects"), list)
            and isinstance(days, list)
            and len(days) == 5
            and isinstance(grid, dict)
            and len(grid) > 0
        )
        check(
            "周视图（GET /api/schedules 200，objects/days=5/grid 非空）",
            sched_ok,
            f"status={st}，days={days if days else '-'}，grid 格数={len(grid) if isinstance(grid, dict) else '-'}",
        )

        # ── 6. 冲突列表（消解前，断言 23 / 3）────────────────────────
        st, body = request(base, "GET", "/api/conflicts", token=token)
        raw = body.get("raw_count") if isinstance(body, dict) else None
        cnt = body.get("count") if isinstance(body, dict) else None
        win = body.get("window") if isinstance(body, dict) else {}
        conflicts_ok = (
            st == 200
            and raw == RAW_COUNT_EXPECT
            and cnt == COUNT_EXPECT
        )
        check(
            f"冲突列表（消解前 raw_count={RAW_COUNT_EXPECT} 且 count={COUNT_EXPECT}）",
            conflicts_ok,
            f"status={st}，raw_count={raw}，count={cnt}，window.from={win.get('from') if isinstance(win, dict) else '-'}",
        )
        if not conflicts_ok:
            return _summary()

        # ── 7. 一键消解（POST /api/resolve）────────────────────────
        st, body = request(base, "POST", "/api/resolve", {}, token=token)
        rb = body.get("remaining_before") if isinstance(body, dict) else None
        ra = body.get("remaining_after") if isinstance(body, dict) else None
        term = body.get("termination") if isinstance(body, dict) else None
        applied = body.get("applied") if isinstance(body, dict) else None
        resolve_ok = (
            st == 200
            and rb == RAW_COUNT_EXPECT
            and ra == 0
            and term == "ok"
            and applied is not None
            and applied > 0
        )
        check(
            f"一键消解（remaining_before={RAW_COUNT_EXPECT} → remaining_after=0，termination=ok）",
            resolve_ok,
            f"status={st}，{rb} → {ra}，applied={applied}，termination={term}",
        )
        if not resolve_ok:
            return _summary()

        # ── 8. 复扫归零 ───────────────────────────────────────────
        st, body = request(base, "GET", "/api/conflicts", token=token)
        raw2 = body.get("raw_count") if isinstance(body, dict) else None
        cnt2 = body.get("count") if isinstance(body, dict) else None
        rescan_ok = st == 200 and raw2 == 0 and cnt2 == 0
        check(
            "复扫归零（消解后再次 GET /api/conflicts 得 0/0）",
            rescan_ok,
            f"status={st}，raw_count={raw2}，count={cnt2}",
        )

        return _summary()
    finally:
        server.close()
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.offline or args.offline_probe:
        _install_offline_guard()
    if args.offline_probe:
        return _offline_probe()
    try:
        code = _run()
    finally:
        # 无论 _run() 从哪个 return 返回，断网行都是最后一行输出
        if args.offline:
            print(f"断网模式：本次运行 {len(OFFLINE_ATTEMPTS)} 次外部连接", flush=True)
    if args.offline and OFFLINE_ATTEMPTS:
        code = 1  # 断网断言：本进程不得有任何对外连接尝试
    return code


if __name__ == "__main__":
    sys.exit(main())
