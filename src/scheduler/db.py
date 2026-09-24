"""数据库连接与初始化。

设计意图：
    B0 批次刻意**不引入任何 Web 框架**，只依赖 Python 标准库 sqlite3。
    原因是技术栈（FastAPI / Flask / 纯前端）尚未最终拍板，数据层与占用计算
    是最稳定的部分——把它写成纯 Python 包，后面无论用什么框架都能直接复用，
    不会因为框架选型变化而返工。

    SQLite 的选择理由：单文件、零部署、断网可跑（现场演示硬要求）。
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def new_id(prefix: str) -> str:
    """生成带前缀的可读 id，例如 obj_3f2a...（便于调试与日志排查）。"""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def default_db_path() -> str:
    """默认数据库文件位置，可用环境变量 SCHEDULER_DB 覆盖。"""
    return os.environ.get("SCHEDULER_DB", "data/scheduler.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    """打开数据库连接。path 为 ':memory:' 时用于测试。"""
    if path is None:
        path = default_db_path()
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # SQLite 默认不开启外键约束，必须显式打开，否则 ON DELETE CASCADE 不生效
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """执行建表脚本。可重复调用（全部使用 IF NOT EXISTS）。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
