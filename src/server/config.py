"""S2 运行配置与环境准备（B1）。

用途与设计意图（答辩口径）：
  1. 「演示日期可注入」是 B1 的红线验收之一：现场演示必须能指定固定日期
     复现同一结果（不能只认"今天"）。因此 Config 的 today 在构造时冻结，
     来源优先级 = 命令行参数 > 环境变量 SCHEDULER_TODAY > 系统当前日期。
     凡是需要"今天"的地方一律读 cfg.today，禁止直接调 date.today()。
  2. seed 的学期起点锚定 cfg.today 所在周的周一（领域层只读，锚定逻辑
     只能放在调用方）——指定任意日期重启服务，库的学期起点与冲突数
     逐字一致，这就是"可复现"的实现方式。
  3. 周六周日槽位：领域层 seed_demo 内部固定 days=5（周一~周五），
     周末缺行会让 mask_for_range() 对周末活动映射出空掩码——等于把
     本项目要解决的核心缺陷在周末场景复发（AGENTS §9b）。领域层不能改，
     因此 ensure_database() 在 seed 之后为每个组织补周末槽位行
     （复制周一作息，enabled=1），属数据行操作，不碰领域代码。
  4. 静态资源版本号由本模块统一导出（AGENTS §8：app.js?v=20261003，
     避免"改了 JS 但浏览器还在用旧版"的现场事故）。

只用 Python 标准库。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".."))

from scheduler.db import connect, new_id  # noqa: E402
from scheduler.seeds import seed_demo  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 静态资源版本号（AGENTS §8）：前端引用形如 app.js?v=<ASSET_VERSION>。
#: 发版时改这一处即可让所有浏览器拿到新文件（配合 no-cache + ETag）。
#: 🔴 2026-10-10修正：F3b 改了 app.js 后 index.html 侧已递增到 20261011，
#:    但本值漏改→两侧不一致。该不同步**不报错**，只会「改了前端偶尔不生效」，
#:    属静默失效，现场 10/23 演示时最难排查。
#:    改动此值时必须同步核对：grep -n 'app.js?v=' web/index.html
#:
#: 🔴 2026-11-10 第 10+ 个观测日：两侧仍不同（本值 20261011 / index.html 20261012）。
#:    现场前统一到演示准备日 20261110 —— 值变化本身即可强制所有浏览器丢弃旧缓存。
#:    自检命令（两处必须输出同一串）：
#:      grep -n 'ASSET_VERSION = ' src/server/config.py
#:      grep -n 'app.js?v=' web/index.html
#:
#: 2026-11-11 第 11 个观测日：本次接 logo（favicon 也吃同一个版本号），
#:    两侧一起递增到 20261113。
#: 2026-11-12：新增「查空位」面板（secSlots，消费 GET /api/slots）改了 app.js，
#:    两侧再一起递增到 20261114（卡面写的 20261112→20261113 已过期，
#:    基线当时已是 20261113；值一变即强制丢旧缓存，这才是卡面要的效果）。
#: 2026-11-11：新增「问一下」面板（secChat，消费 POST /api/chat）改了 app.js + index.html，
#:    两侧一起递增到 20261115。
#: 2026-11-12：界面上把开发期自证（接口名 / 契约编号）挤出去、技术说明收进底部折叠区，
#:    改了 app.js 与 index.html，两侧一起递增到 20261116。
#: 2026-11-12（卡 runQn7）：对外改名「时之交响 TimeSymphony」+ 首屏状态条 / 卡片对比 /
#:    折叠 / 预设问题芯片，改了 app.js 与 index.html，三处（本值 + app.js?v= + favicon?v=）
#:    一起递增到 20261117。
#: 2026-11-12（队评反馈）：页面改三栏布局 + 字体分层（衬线标题 / 黑体正文 / 等宽数字），
#:    又改了 index.html，三处一起递增到 20261118。
#: 2026-11-12：页头改用「时之交响」图标本体，新增静态资源 web/brand-icon.js，
#:    加了第四处引用（brand-icon.js?v=），四处一起递增到 20261119
#:    （基线已是 20261118，不能沿用同值 —— 值不变就丢不了缓存）。
ASSET_VERSION = "20261119"

#: 默认数据库：与队长 L8 沙盒演示库同路径，现场「删 data/ → 一条命令 →
#: 30 秒可演示」的闭环由本模块的自动初始化兜底（CP-G）。
DEFAULT_DB_NAME = "demo.db"

#: 演示种子规模：🔴 students_per_class 必须显式传 10（30 名学生 → 23 处冲突，
#: 该数字已写进 README / docs/01 / 报名材料；用默认值 8 会得到 19，对不上）。
SEED_STUDENTS_PER_CLASS = 10


def repo_root() -> str:
    """仓库根（src/server/ 的上两级），路径锚定不受当前工作目录影响。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def monday_of(d: date) -> date:
    """d 所在周的周一。"""
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServerConfig:
    """服务端运行配置。

    today 是冻结字段：进程内构造后不再变化，保证同一日期下
    任何输出（/api/health、学期锚点等）都可复现。
    """

    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = field(default_factory=lambda: os.path.join(repo_root(), "data", DEFAULT_DB_NAME))
    today: date = field(default_factory=date.today)
    web_root: str = field(default_factory=lambda: os.path.join(repo_root(), "web"))
    asset_version: str = ASSET_VERSION
    seed_if_missing: bool = True  # 库文件不存在时自动初始化演示数据
    #: 演示模式（docs/17 §1 事故一加固）：True 时 run_server 把 today 固定到
    #: 学期第 1 周（读库 terms.start_date），避免消解窗口随系统日期漂移、
    #: 现场扫不到冲突还不报错。
    demo: bool = False


def _parse_today(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"--today / SCHEDULER_TODAY 需要 YYYY-MM-DD，收到 {value!r}") from exc


def load_config(argv: list[str] | None = None) -> ServerConfig:
    """解析配置：命令行参数 > 环境变量 > 默认值。

    命令行：--host --port --db --today --web --no-seed
    环境变量：SCHEDULER_HOST / SCHEDULER_PORT / SCHEDULER_DB /
              SCHEDULER_TODAY / SCHEDULER_WEB / SCHEDULER_NO_SEED
    """
    import argparse

    parser = argparse.ArgumentParser(description="时之交响 TimeSymphony · 服务端（本地运行，断网可用）")
    parser.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认 8000；0 = 随机分配")
    parser.add_argument("--db", default=None, help="SQLite 数据库路径，默认 <仓库根>/data/demo.db")
    parser.add_argument(
        "--today",
        default=None,
        help="演示日期注入（YYYY-MM-DD）：固定服务端的『今天』，"
        "现场演示用固定日期复现同一结果；不传则用系统当前日期",
    )
    parser.add_argument("--web", default=None, help="前端静态资源目录，默认 <仓库根>/web")
    parser.add_argument(
        "--no-seed",
        action="store_true",
        default=None,
        help="库文件不存在时也不自动初始化（报错退出，用于连已有库）",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        default=None,
        help="演示模式：把『今天』固定到学期第 1 周（读库 terms.start_date，"
        "不硬编码日期），并打印当前窗口与本窗口冲突数——"
        "根治『不带 --today 窗口漂移、冲突扫不到还不报错』的静默事故（docs/17 §1）",
    )
    args = parser.parse_args(argv)

    env = os.environ
    host = args.host or env.get("SCHEDULER_HOST") or "127.0.0.1"

    port_raw = args.port if args.port is not None else env.get("SCHEDULER_PORT")
    port = int(port_raw) if port_raw not in (None, "") else 8000

    db_path = args.db or env.get("SCHEDULER_DB") or os.path.join(repo_root(), "data", DEFAULT_DB_NAME)

    today_raw = args.today or env.get("SCHEDULER_TODAY")
    today = _parse_today(today_raw) if today_raw else date.today()

    web_root = args.web or env.get("SCHEDULER_WEB") or os.path.join(repo_root(), "web")

    no_seed = args.no_seed if args.no_seed is not None else bool(env.get("SCHEDULER_NO_SEED"))
    demo = args.demo if args.demo is not None else bool(env.get("SCHEDULER_DEMO"))

    return ServerConfig(
        host=host,
        port=port,
        db_path=os.path.abspath(db_path),
        today=today,
        web_root=os.path.abspath(web_root),
        seed_if_missing=not no_seed,
        demo=demo,
    )


# ---------------------------------------------------------------------------
# 进程内当前配置（供 api_*.py 读取，避免把资源塞进 handle 的固定签名）
# ---------------------------------------------------------------------------

_current: ServerConfig | None = None


def set_current_config(cfg: ServerConfig) -> None:
    global _current
    _current = cfg


def get_current_config() -> ServerConfig:
    if _current is None:
        raise RuntimeError("服务端配置尚未初始化（run_server 启动时会调用 set_current_config）")
    return _current


def open_db() -> sqlite3.Connection:
    """按当前配置打开一个新连接（调用方负责 close）。

    多线程服务下每请求新建连接（SQLite 本地开销极小），
    规避 sqlite3 的同线程限制，不需要全局锁。
    """
    return connect(get_current_config().db_path)


# ---------------------------------------------------------------------------
# 数据库准备（含 B1 两条红线）
# ---------------------------------------------------------------------------

def ensure_weekend_slots(conn: sqlite3.Connection) -> int:
    """为每个组织的周六（6）、周日（7）补槽位行，返回补入行数。

    规则数据化（AGENTS §1b）：周末作息 = 复制该组织周一的行，enabled=1。
    周一改了作息，周末跟着变——改数据不改代码。
    已有周末行的组织不动（尊重既有配置）。
    """
    added = 0
    org_ids = [r["id"] for r in conn.execute("SELECT id FROM organizations").fetchall()]
    for org_id in org_ids:
        for dow in (6, 7):
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM period_slots WHERE org_id = ? AND day_of_week = ?",
                (org_id, dow),
            ).fetchone()["c"]
            if n:
                continue
            rows = conn.execute(
                "SELECT idx, name, start_time, end_time FROM period_slots "
                "WHERE org_id = ? AND day_of_week = 1 ORDER BY idx",
                (org_id,),
            ).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO period_slots "
                    "(id, org_id, day_of_week, idx, name, start_time, end_time, enabled) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (new_id("slot"), org_id, dow, r["idx"], r["name"], r["start_time"], r["end_time"]),
                )
                added += 1
    if added:
        conn.commit()
    return added


def ensure_database(cfg: ServerConfig) -> dict:
    """按配置准备数据库，返回初始化报告（doctor 亦断言该路径）。

    - 库文件不存在：init（seed_demo 内部建表建组织）→ 补周末槽位。
      seed 的 term_start 锚定 cfg.today 所在周周一 → 日期可注入、结果可复现。
    - 库文件已存在：直接使用；但无论新旧库都跑一遍 ensure_weekend_slots，
      覆盖「旧库只有 5 天槽位」的情况（幂等，已有行不动）。
    - seed_if_missing=False 且库不存在：抛 FileNotFoundError，明确报错。
    """
    if not os.path.exists(cfg.db_path):
        if not cfg.seed_if_missing:
            raise FileNotFoundError(
                f"数据库不存在且已禁用自动初始化：{cfg.db_path}\n"
                f"（先跑 python scripts/reset_demo.py 重建演示库，或去掉 --no-seed）"
            )
        os.makedirs(os.path.dirname(cfg.db_path) or ".", exist_ok=True)
        conn = connect(cfg.db_path)
        try:
            info = seed_demo(
                conn,
                classes=3,
                students_per_class=SEED_STUDENTS_PER_CLASS,
                teachers=9,
                rooms=4,
                term_start=monday_of(cfg.today).isoformat(),
            )
        finally:
            conn.close()
        created = True
    else:
        created = False

    conn = connect(cfg.db_path)
    try:
        weekend_added = ensure_weekend_slots(conn)
    finally:
        conn.close()

    return {
        "created": created,
        "weekend_slots_added": weekend_added,
        "db_path": cfg.db_path,
    }
