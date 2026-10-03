"""S3 约定式自动注册（B1）。

用途与设计意图（答辩口径）：
  三人并行开发的最关键约定：**新增 API 只加文件，不改任何现有文件**。
  本模块扫描 src/server/ 下所有 api_*.py，把模块级 ROUTES 合并进路由表。

  🔴 禁止手工往本文件加注册行（AGENTS.md 跨人约定第 1 条）——
     谁手工加注册行，就破坏了三人并行的基础。

约定（B2/B3/B4/B5 沿用）：
  1. 每个 api_*.py 在模块级导出：
         ROUTES: dict[tuple[str, str], Handler]
     键是 ("METHOD", "/api/xxx")，值是处理函数。
  2. 处理函数签名统一写死（AGENTS.md 跨人约定第 2 条）：
         def handle(method, path, query, body, headers, context=None)
     - query  : dict[str, list[str]]（urllib.parse.parse_qs 的原生形态）
     - body   : bytes（原始载荷；需要 JSON 时用 http_api.json_body() 解析）
     - headers: dict[str, str]，键已小写
     - context=None 表示未登录；B2 后由鉴权中间件填充登录上下文
  3. 返回值：dict → 200 + JSON；(status:int, dict) → 指定状态码 + JSON。
     未登录应返回 401 的逻辑由 B2 的装饰器统一处理。
  4. 两个文件注册同一个 (METHOD, path) → 启动即报错（防静默覆盖）。

只用 Python 标准库。
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Callable

Handler = Callable[..., Any]

#: 路由键：("GET", "/api/health")
RouteKey = tuple[str, str]

_ROUTES: dict[RouteKey, Handler] | None = None


def scan() -> dict[RouteKey, Handler]:
    """扫描 api_*.py 并合并所有 ROUTES。每次调用重新扫描（测试友好）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    routes: dict[RouteKey, Handler] = {}
    for filename in sorted(os.listdir(here)):
        if not (filename.startswith("api_") and filename.endswith(".py")):
            continue
        module_name = f"server.{filename[:-3]}"
        module = importlib.import_module(module_name)
        module_routes = getattr(module, "ROUTES", None)
        if not isinstance(module_routes, dict) or not module_routes:
            raise RuntimeError(
                f"{module_name} 未导出非空 ROUTES（约定：ROUTES = {{('METHOD', '/api/…'): handler}}）"
            )
        for key, handler in module_routes.items():
            if not (isinstance(key, tuple) and len(key) == 2 and callable(handler)):
                raise RuntimeError(f"{module_name}.ROUTES 含非法条目：{key!r}")
            method, path = key
            if not (isinstance(method, str) and isinstance(path, str) and path.startswith("/api/")):
                raise RuntimeError(f"{module_name}.ROUTES 键必须形如 ('GET', '/api/…')，收到 {key!r}")
            if key in routes:
                raise RuntimeError(
                    f"路由冲突：{key!r} 被 {module_name} 重复注册（先注册于 {routes[key].__module__}）"
                )
            routes[key] = handler
    return routes


def get_routes() -> dict[RouteKey, Handler]:
    """获取路由表（进程内首次调用时扫描并缓存）。"""
    global _ROUTES
    if _ROUTES is None:
        _ROUTES = scan()
    return _ROUTES


def reset_cache() -> None:
    """清缓存（doctor / 单元测试用）。"""
    global _ROUTES
    _ROUTES = None
