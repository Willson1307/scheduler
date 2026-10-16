"""F6 冷启动一条命令（S15 部署就绪包）。

运行：
    python deploy/start.py                          # 默认 127.0.0.1:8000，data/demo.db，演示日期 2026-09-08
    python deploy/start.py --port 8080              # 固定端口
    python deploy/start.py --port 0                 # 随机端口（验收脚本 / 联调用）
    python deploy/start.py --host 0.0.0.0           # 校园网内可达（务必配合安全组只放行校园网段）
    python deploy/start.py --open                   # 就绪后自动打开浏览器
    python deploy/start.py --no-seed                # 库不存在也不初始化（连已有库）

用途与设计意图（答辩口径）：
  1. 「删 data/ → 一条命令 → 30 秒可演示」——本脚本就是那条命令。
     库缺失时由服务端 ensure_database() 自动 seed（3 班 ×10 生 / 9 师 / 4 室 + 周末槽位），
     演示日期固定 2026-09-08 → 学期锚定 2026-09-07（第 1 周），冲突 23 处可复现。
     🔴 不带 --today 会落到「今天」所在周，只扫到 1 处冲突**且不报错**（docs/17 §1）。
  2. 本脚本不重复实现任何服务端逻辑，只补三件 run_server 不管的事：
     ① 启动前端口预检（Windows 下二次绑定会静默成功，见 docs/29）；
     ② 绑定 0.0.0.0 时显式提醒「安全组只放行校园网段」；
     ③ --open 时轮询 /api/health，就绪后打开浏览器。
  3. 断网可跑、零第三方依赖：只用标准库；服务端只 bind 本地 + 读写本地 SQLite。
  4. 跨平台：Windows 开发机与 Linux 服务器同一份脚本；systemd 直接 ExecStart 本脚本
     （纸面 unit 见 docs/05-部署与运维.md §4）。
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

#: 仓库根（deploy/ 的上一级），路径锚定不受当前工作目录影响
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SERVER = os.path.join(REPO_ROOT, "scripts", "run_server.py")

#: 演示日期：2026-09-08 所在周周一 = 2026-09-07（第 1 周），23 处冲突全在这一周。
#: 与 docs/17 §3.1 的现场命令逐字一致。
DEMO_TODAY = "2026-09-08"

#: 绕过系统代理：否则代理会拦 127.0.0.1，把「服务未就绪」误判成「服务异常」
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    """端口预检：能连上即认为已有服务在听。

    用 connect 探测而不是 bind —— Windows 下 SO_REUSEADDR 语义不同，二次 bind
    可能**静默成功**（docs/29：别指望端口占用会报错），「能否连上」才是可靠判据。
    port=0（随机端口）永远视为可用。
    """
    if port == 0:
        return False
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((probe_host, port)) == 0


def build_server_args(ns: argparse.Namespace) -> list[str]:
    """把本脚本的参数翻译成 scripts/run_server.py 的命令行。"""
    args = [
        sys.executable,
        RUN_SERVER,
        "--host",
        ns.host,
        "--port",
        str(ns.port),
        "--today",
        ns.today,
    ]
    if ns.db:
        args += ["--db", ns.db]
    if ns.no_seed:
        args.append("--no-seed")
    return args


def _probe_host(host: str) -> str:
    """0.0.0.0 不能直接当访问地址，探测/打开浏览器一律回落 127.0.0.1。"""
    return "127.0.0.1" if host in ("", "0.0.0.0") else host


def _open_browser_when_ready(host: str, port: int, timeout: float = 30.0) -> None:
    """后台轮询 /api/health，就绪后打开浏览器（best effort，失败静默）。

    刻意不解析子进程输出：用独立线程打健康检查，服务端 stdout 因此可以原样继承，
    READY 行使 doctor / acceptance 能照常解析。
    """
    base = f"http://{_probe_host(host)}:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _OPENER.open(base + "/api/health", timeout=2) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    else:
        return  # 超时未就绪：不打开浏览器，避免开出一个连不上的页面
    try:
        import webbrowser

        webbrowser.open(base + "/")
    except Exception:  # noqa: BLE001 —— 无图形界面（服务器）时静默跳过
        pass


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="课表活动调度辅助系统 · 冷启动（删掉 data/ 后一条命令回到可演示状态）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（仅本机）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000；0 = 随机分配")
    parser.add_argument(
        "--today",
        default=DEMO_TODAY,
        help=f"演示日期 YYYY-MM-DD，默认 {DEMO_TODAY}（第 1 周，23 处冲突可复现）",
    )
    parser.add_argument("--db", default=None, help="SQLite 库路径，默认 <仓库根>/data/demo.db")
    parser.add_argument(
        "--open", dest="open_browser", action="store_true", help="就绪后自动打开浏览器"
    )
    parser.add_argument(
        "--no-seed", dest="no_seed", action="store_true", help="库不存在时也不自动初始化"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv)

    if not os.path.exists(RUN_SERVER):
        print(f"ERROR 找不到服务端入口：{RUN_SERVER}", flush=True)
        return 2

    if port_in_use(ns.host, ns.port):
        print(
            f"ERROR 端口 {ns.port} 已被占用。\n"
            f"      （Windows 下服务端二次绑定会静默成功，所以这里先拦一道）\n"
            f"      换端口重试：python deploy/start.py --port 8001\n"
            f"      或随机端口：python deploy/start.py --port 0",
            flush=True,
        )
        return 2

    if ns.host in ("0.0.0.0", "::"):
        print(
            f"WARN 监听 {ns.host} = 校园网内可达。🔴 安全组 / 防火墙必须只放行校园网 IP 段，"
            f"不得对公网开放（见 docs/05-部署与运维.md §5）。",
            flush=True,
        )

    args = build_server_args(ns)
    print(f"INFO 冷启动命令：{' '.join(args)}", flush=True)
    print("INFO 库缺失时自动初始化（3 班 ×10 生 / 9 师 / 4 室 + 周末槽位）", flush=True)

    if ns.open_browser:
        if ns.port == 0:
            print("WARN --port 0 时端口随机、地址无法预知，跳过自动打开浏览器", flush=True)
        else:
            threading.Thread(
                target=_open_browser_when_ready, args=(ns.host, ns.port), daemon=True
            ).start()

    proc = subprocess.Popen(args)

    # POSIX（含 systemd）：SIGTERM 转给子进程，避免只杀外层壳、留下孤儿服务。
    # Windows 下控制台 Ctrl-C 会同时投递给本进程与子进程，无需额外处理。
    if os.name != "nt":

        def _forward(signum, frame):  # noqa: ANN001
            if proc.poll() is None:
                proc.terminate()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _forward)
            except (ValueError, OSError):
                pass

    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
