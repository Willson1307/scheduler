"""服务端（B1 骨架 · S1–S4）。

分层与设计意图（答辩口径）：
  1. config.py   —— 运行配置 + 数据库准备（含两条 B1 红线：
                    演示日期可注入、周六周日槽位 enabled=1）
  2. routes.py   —— 约定式自动注册：扫描 api_*.py，导出 ROUTES 即挂载。
                    🔴 本文件不含任何手工注册行，新增 API 只加 api_*.py 文件
  3. api_*.py    —— 每个文件一组 API；处理函数签名统一写死：
                    handle(method, path, query, body, headers, context=None)
                    context=None 表示未登录（B2 鉴权后由中间件填充）
  4. http_api.py —— ThreadingHTTPServer 适配层：解析请求 → 查路由 / 走静态
  5. static.py   —— web/ 静态文件服务（no-cache + ETag + 版本号查询参数）

只用 Python 标准库；核心功能不依赖外网（**本队自定约束**，不是章程要求——
2026-11-12 更正，`docs/24` 无此条）。AI 增强可联网，断了降级不崩。
"""
