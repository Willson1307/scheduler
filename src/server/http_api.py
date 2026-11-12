"""S3 HTTP 适配层：ThreadingHTTPServer + 固定签名分发（B1）。

用途与设计意图（答辩口径）：
  1. 技术栈红线：HTTP 层用标准库 http.server（ThreadingHTTPServer），
     不用 FastAPI/Flask——零第三方依赖是**本队自定约束**（不是章程要求，
     2026-11-12 更正：`docs/24` 无此条）。核心功能不依赖外网（现场用本地
     服务演示）；可选的 AI 增强需要外网，断了降级、页面不崩。
  2. handle(method, path, query, body, headers, context=None) 签名写死
     （AGENTS.md 跨人约定第 2 条）：本层只做"解析请求 → 查路由 → 调用"，
     领域逻辑全部在 api_*.py / 领域层，将来换框架只替换本适配层。
  3. 统一出口：handler 返回 dict → 200；返回 (status, dict) → 指定状态码；
     抛 ApiError(status, msg) → 指定状态码；其他异常 → 500（绝不裸崩连接）。
  4. context 固定传 None（B1 无登录）；B2 的鉴权中间件将在此填充。

只用 Python 标准库。
"""

from __future__ import annotations

import json
import socket
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import routes, static
from .config import ServerConfig

#: 请求体上限（字节）。防御性上限，业务上远远用不到 1MB。
MAX_BODY_BYTES = 1 * 1024 * 1024


class ApiError(Exception):
    """业务层主动抛出的带状态码错误（B2/B3 沿用，如 401/403/409）。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def json_body(body: bytes) -> dict:
    """解析 JSON 请求体（api_*.py 统一用它，不各写一遍 try/except）。

    空体视为 {}（GET 转 POST 的容错场景）；非法 JSON 抛 ApiError(400)。
    """
    if not body or not body.strip():
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, f"JSON 请求体解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "JSON 请求体必须是对象（object）")
    return data


# ---------------------------------------------------------------------------
# 服务器与请求处理
# ---------------------------------------------------------------------------

class SchedulerHTTPServer(ThreadingHTTPServer):
    """多线程 HTTP 服务器：daemon 线程保证 Ctrl-C 后 3 秒内干净退出（CP-A）。"""

    daemon_threads = True
    allow_reuse_address = True
    #: 🔴 backlog 必须显式放大（2026-10-11 部署实测发现）。
    #: socketserver.TCPServer 默认 request_queue_size=5，公网试用时多个
    #: 探测/并发连接会把 accept 队列塞满，后续请求直接 Connection refused
    #: —— 服务端日志看不到这些请求，health 自检也照样全绿，属新一类静默失效。
    #: 12 位教师可能同时打开页面，128 足够。
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], cfg: ServerConfig,
                 family: int = socket.AF_INET):
        self.cfg = cfg
        self.address_family = family
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "SchedulerServer/1.0"
    protocol_version = "HTTP/1.1"

    # -- 分发 ---------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            url = urlsplit(self.path)
            path = self._normalize(url.path)
            query = parse_qs(url.query, keep_blank_values=True)
            body = self._read_body()

            if path.startswith("/api/"):
                self._dispatch_api(method, path, query, body)
            else:
                static.serve(self, self.server.cfg, path)  # type: ignore[attr-defined]
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # 客户端提前断开，无需响应
        except Exception:
            traceback.print_exc()
            try:
                self._send_json(500, {"error": "internal server error"})
            except Exception:
                pass

    def _dispatch_api(self, method: str, path: str, query: dict, body: bytes) -> None:
        table = routes.get_routes()
        handler = table.get((method, path))
        if handler is None:
            raise ApiError(404, f"接口不存在：{method} {path}")
        headers = {k.lower(): v for k, v in self.headers.items()}
        # 🔴 必须在字典推导**之后**注入——放前面会被客户端伪造的 X-Client-IP 覆盖。
        # 仅供 api_login 限流使用（内存态，不落库/不进日志，见隐私红线 docs/08）。
        headers["x-client-ip"] = self.client_address[0]
        result = handler(method, path, query, body, headers, context=None)
        status, payload, extra_headers = self._normalize_result(result)
        self._send_json(status, payload, extra_headers)

    @staticmethod
    def _normalize_result(result: Any) -> tuple[int, Any, dict | None]:
        """统一返回三元组 (status, payload, extra_headers)。

        handler 返回 dict → 200；返回 (status, payload) → 指定状态码；
        返回 (status, payload, headers_dict) → 额外写自定义响应头
        （如登录限流的 Retry-After）。extra_headers 仅本层消费。
        """
        if (isinstance(result, tuple) and len(result) == 3
                and isinstance(result[0], int) and isinstance(result[2], dict)):
            return result[0], result[1], result[2]
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            return result[0], result[1], None
        return 200, result, None

    @staticmethod
    def _normalize(path: str) -> str:
        """去掉尾部斜杠（'/api/health/' → '/api/health'）；根路径保持 '/'。"""
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        return path

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            raise ApiError(413, f"请求体过大（>{MAX_BODY_BYTES} 字节）")
        return self.rfile.read(length)

    # -- 响应 ---------------------------------------------------------------

    def _send_json(self, status: int, payload: Any,
                   extra_headers: dict | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(data)

    # -- 动词入口 ------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")
