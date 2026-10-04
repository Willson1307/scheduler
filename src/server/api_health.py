"""第一个约定式挂载的 API：GET /api/health（B1 · CP-A 载体）。

用途与设计意图（答辩口径）：
  1. 健康检查是 doctor.py（S1）的核心断言对象：/api/health 200 = 骨架活着。
  2. 响应携带 server_date（= 注入的演示日期）——「指定任意日期都能复现
     同一结果」的最小可验证出口：doctor 用不同 --today 启动，断言它逐字一致。
  3. db 状态用一次真实查询（SELECT 1）判定，不做假健康。
  4. 本文件同时是「约定式自动注册」的第一个用例：routes.scan() 发现它
     纯粹因为它叫 api_*.py 并导出 ROUTES——没有任何手工注册行。

签名约定（写死，不得改）：
  handle(method, path, query, body, headers, context=None)
  context=None 表示未登录；本接口公开，无需登录。
"""

from __future__ import annotations

from . import config
from .http_api import json_body  # noqa: F401  （统一入口，便于各 api_* 复用）


def health(method: str, path: str, query: dict, body: bytes, headers: dict, context=None) -> dict | tuple[int, dict]:
    cfg = config.get_current_config()

    db_state = "ok"
    try:
        conn = config.open_db()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception:
        db_state = "error"

    payload = {
        # 🔴 `ok` 是契约 docs/07 §9 冻结的**唯一必返字段**：
        #   契约原文「响应200 = {"ok":true}」，而 tests/test_contract.py:114 的
        #   就绪判定写的是 `body.get("ok") is True`——字段名不符就整体 SkipTest。
        #
        #   2026-10-04 实测后果：这个字段缺失导致 **40+ 项契约测试从上线起从未真正执行**
        #   （`Ran 0 tests` + `OK`，退出码还是 0，混在 110 项 discover 里极易被忽略）。
        #   加回这一行让契约测试真正跑起来，是本行存在的唯一理由。
        #
        #   ⚠️ 契约 §9 还有一句「只返回这一句，不带版本号/路径（公网下避免指纹识别）」，
        #   本实现多返了 status/server_date/asset_version/db。取舍见 docs/20 §5.2.2：
        #   先保留（doctor.py 依赖 server_date/db 做自检），**CP-H 上公网前必须收紧到
        #   只返 ok**，否则违背契约的安全意图。
        "ok": True,
        "status": "ok" if db_state == "ok" else "degraded",
        "server_date": cfg.today.isoformat(),
        "asset_version": cfg.asset_version,
        "db": db_state,
    }
    if db_state != "ok":
        payload["ok"] = False
        return 503, payload
    return payload


ROUTES = {
    ("GET", "/api/health"): health,
}
