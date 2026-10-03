"""S4 静态文件服务：web/ 目录（B1）。

用途与设计意图（答辩口径）：
  1. 前端产物是纯静态文件（原生 JS、无构建步骤），由本模块直接服务——
     「本地部署 + 无外链」满足断网要求，不需要任何静态资源服务器。
  2. 🔴 Cache-Control: no-cache + ETag（AGENTS §8）：现场改样式时，
     避免出现「改了 JS 但老师浏览器还在用旧版」且极难排查的事故。
     前端引用形如 app.js?v=20261003（版本号由 config.py 统一导出）。
  3. 目录穿越防护：归一化后必须仍在 web_root 内。
  4. web/ 目录尚不存在（F1 前端骨架未建）时返回 404 JSON 而非崩溃——
     B1 阶段后端先起来，前端目录后到。

只用 Python 标准库。
"""

from __future__ import annotations

import hashlib
import os

from .config import ServerConfig

#: 扩展名 → Content-Type（默认 octet-stream 下载）
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


def serve(handler, cfg: ServerConfig, path: str) -> None:
    """向 BaseHTTPRequestHandler 发送静态文件响应。

    - '/' → index.html（单页应用唯一入口）
    - 命中且带匹配 ETag → 304（省流量；改版后内容变 → ETag 变 → 重新拉取）
    - web_root 不存在 / 文件不存在 / 越界 → 404 JSON（与 API 的错误形态一致）
    """
    root = cfg.web_root

    if not os.path.isdir(root):
        _send(handler, 404, {"error": f"静态资源目录不存在：{root}（前端尚未构建）"})
        return

    rel = "index.html" if path in ("", "/") else path.lstrip("/")
    full = os.path.abspath(os.path.join(root, rel))

    # 目录穿越防护：归一化后必须仍在 web_root 内
    if os.path.commonpath([full, os.path.abspath(root)]) != os.path.abspath(root):
        _send(handler, 404, {"error": "not found"})
        return

    if not os.path.isfile(full):
        _send(handler, 404, {"error": f"not found: {path}"})
        return

    with open(full, "rb") as f:
        data = f.read()

    etag = '"' + hashlib.md5(data).hexdigest() + '"'
    if handler.headers.get("If-None-Match") == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        return

    ext = os.path.splitext(full)[1].lower()
    handler.send_response(200)
    handler.send_header("Content-Type", MIME_TYPES.get(ext, "application/octet-stream"))
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("ETag", etag)
    handler.end_headers()
    handler.wfile.write(data)


def _send(handler, status: int, payload) -> None:
    import json

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
