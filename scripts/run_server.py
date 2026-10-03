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
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from server import routes  # noqa: E402
from server.config import ensure_database, load_config, set_current_config  # noqa: E402
from server.http_api import SchedulerHTTPServer  # noqa: E402


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


def main(argv: list[str] | None = None) -> int:
    cfg = load_config(argv)

    report = ensure_database(cfg)
    set_current_config(cfg)

    routes.get_routes()  # fail fast：扫描 + 路由冲突检查
    server = SchedulerHTTPServer((cfg.host, cfg.port), cfg)
    port = server.server_address[1]  # --port 0 时是实际分配端口

    print(
        f"READY host={cfg.host} port={port} db={cfg.db_path} today={cfg.today.isoformat()}",
        flush=True,
    )
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
