"""S4 服务启动入口（B1）。

运行：
    python scripts/run_server.py                     # 默认 127.0.0.1:8000，库=data/demo.db
    python scripts/run_server.py --today 2026-10-10  # 演示日期注入（现场复现用）
    python scripts/run_server.py --port 0            # 随机端口（doctor / 联调用）

用途与设计意图（答辩口径）：
  1. 「一条命令可演示」：库不存在时自动初始化（seed + 周末槽位），
     删掉 data/ 后直接启动即可进入可演示状态（CP-G 闭环的起点）。
  2. 演示日期注入：--today / SCHEDULER_TODAY 固定服务端的"今天"，
     学期起点锚定该日期所在周周一 → 指定任意日期重启，结果逐字一致。
  3. Ctrl-C 优雅退出：Windows 下 CTRL_BREAK 信号同样映射为
     KeyboardInterrupt，保证 doctor 的「3 秒退出」断言（CP-A）可测。
  4. 启动即扫路由（fail fast）：api_*.py 有路由冲突在启动当场报错，
     而不是等到某个请求进来才发现。

只用 Python 标准库。
"""

from __future__ import annotations

import os
import signal
import socket
import sys
from dataclasses import replace
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from server import routes  # noqa: E402
from server.config import ensure_database, load_config, set_current_config  # noqa: E402
from server.http_api import SchedulerHTTPServer  # noqa: E402


def _bind_family(host: str) -> int:
    """按 host 决定 socket 地址族，交给 SchedulerHTTPServer 使用。

    🔴 双栈监听（2026-10-11 云部署实测发现）：
    云主机通常同时有 IPv4 与 IPv6。`--host 0.0.0.0` 只绑 IPv4，
    纯 IPv6 网络的用户（部分校园网 / 移动网络）会连不上。
    绑 `::`（AF_INET6）时，只要系统 `net.ipv6.bindv6only=0`（Linux 默认），
    同一个套接字就同时接受 IPv4 映射地址 —— 一条监听覆盖两族，最省事。
    """
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def _map_ctrl_break_to_interrupt() -> None:
    """Windows：CTRL_BREAK_EVENT → KeyboardInterrupt。

    信号处理器在主线程执行；serve_forever 被信号打断后异常从主循环冒出，
    与 Ctrl-C（SIGINT）机制一致。POSIX 上 SIGINT 本身就映射 KI，无需处理。
    """

    if not hasattr(signal, "SIGBREAK"):
        return

    def _raise_keyboard_interrupt(signum, frame):  # noqa: ANN001
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGBREAK, _raise_keyboard_interrupt)
    except (ValueError, OSError):
        pass  # 非主线程或平台不支持时静默跳过，SIGINT 路径仍然可用


def _explicit_today(argv: list[str] | None) -> bool:
    """用户是否显式指定了 --today / SCHEDULER_TODAY（显式值优先于 --demo 覆盖）。"""
    if os.environ.get("SCHEDULER_TODAY"):
        return True
    args = argv if argv is not None else sys.argv[1:]
    return any(a == "--today" or a.startswith("--today=") for a in args)


def _demo_window(db_path: str) -> tuple[date, date, int] | None:
    """读库算演示窗口与真实冲突数（不硬编码日期，不硬编码 23）。

    - 第 1 周口径：terms.start_date（reset_demo 改 TERM_START 时自动同步）。
    - 冲突数：scheduler.occupancy.scan 逐组织真查求和（与 reset_demo 同算法）。
    - terms 表不存在 / 无行（异常库）：返回 None，调用方降级提示。
    """
    from scheduler.db import connect
    from scheduler.occupancy import scan
    from scheduler.slots import load_slot_config

    conn = connect(db_path)
    try:
        try:
            row = conn.execute("SELECT start_date FROM terms ORDER BY start_date LIMIT 1").fetchone()
        except Exception:  # noqa: BLE001  # 无 terms 表的旧/异常库
            return None
        if not row or not row["start_date"]:
            return None
        d0 = date.fromisoformat(row["start_date"])
        d6 = d0 + timedelta(days=6)
        total = 0
        for org in conn.execute("SELECT id FROM organizations").fetchall():
            cfg = load_slot_config(conn, org["id"])
            total += scan(conn, org["id"], d0, d6, cfg=cfg).count
        return d0, d6, total
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    cfg = load_config(argv)

    report = ensure_database(cfg)

    # ── 演示模式（docs/17 §1 事故一加固）─────────────────────────
    # --demo 且未显式传 --today：把 today 固定到学期第 1 周（start+1 天，
    # 复现实测 09-08）。日期读库不硬编码；新库 term_start 锚定真实今天所在
    # 周周一，天然落在第 1 周，自洽。显式 --today 优先，不被覆盖。
    demo_note = ""
    if cfg.demo:
        if _explicit_today(argv):
            demo_note = "INFO --demo：已显式指定 --today，未覆盖（如需固定第 1 周请去掉 --today）"
        else:
            win = _demo_window(cfg.db_path)
            if win is None:
                demo_note = "WARN --demo：库中无 terms 行，无法定位学期第 1 周，today 保持系统日期（建议先跑 scripts/reset_demo.py）"
            else:
                d0, _, n_conflicts = win
                cfg = replace(cfg, today=d0 + timedelta(days=1))
                demo_note = (
                    f"INFO --demo：today 已固定到学期第 1 周（{cfg.today.isoformat()}），"
                    f"当前窗口 = {win[0].isoformat()} ~ {win[1].isoformat()}，本窗口冲突 {n_conflicts} 处"
                )

    set_current_config(cfg)

    routes.get_routes()  # fail fast：扫描 + 路由冲突检查
    server = SchedulerHTTPServer((cfg.host, cfg.port), cfg, family=_bind_family(cfg.host))
    port = server.server_address[1]  # --port 0 时是实际分配端口

    print(
        f"READY host={cfg.host} port={port} db={cfg.db_path} today={cfg.today.isoformat()}",
        flush=True,
    )
    if demo_note:
        print(demo_note, flush=True)
    if report["created"]:
        print(
            f"INFO 已初始化新库：seed 规模 3 班 ×10 生 / 9 师 / 4 室，"
            f"term_start 锚定 {cfg.today.isoformat()} 所在周周一",
            flush=True,
        )
    if report["weekend_slots_added"]:
        print(
            f"INFO 已补周末槽位 {report['weekend_slots_added']} 行（enabled=1，AGENTS §9b）",
            flush=True,
        )
    print(
        f"INFO 页面 http://{cfg.host}:{port}/ ｜ 健康检查 http://{cfg.host}:{port}/api/health"
        f" ｜ Ctrl-C 退出",
        flush=True,
    )

    _map_ctrl_break_to_interrupt()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("INFO 服务已退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
