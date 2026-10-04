"""B2 鉴权中间件：令牌签发/校验 + require_auth 装饰器（分支B）。

用途与设计意图（答辩口径）：
  1. 令牌用标准库 hmac+secrets 签名（无状态、断网可用、零第三方依赖）：
     payload=<base64(json)> . sig=<base64(hmac_sha256(payload, SECRET))>。
     无状态 = 重启不依赖外部存储；SECRET 进程级随机，重启后旧令牌失效
     （本地工具可接受，用户重新登录即可）。
  2. require_auth 装饰器在 handler 层拦截（不改 http_api.py 的固定分发）：
     读 Authorization: Bearer <token> → 校验 → 填充 context；未登录返回 401。
     这样 B2 只碰自己的独占文件，http_api 的 context=None 硬编码不动。
  3. 错误体用契约格式 {"error":{"code","message"}}（docs/07 通用约定），
     通过返回 (status, body) 元组产出——不抛 ApiError（B1 的 ApiError 产扁平
     {"error":msg}，与契约嵌套格式不一致；B2 不碰 http_api，故用元组）。
  4. context 形态：{"user_id","login","role","object_id"}，供 api_*.py 取当前用户。

只用 Python 标准库。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 进程级签名密钥（重启失效，本地工具可接受）
# ---------------------------------------------------------------------------
_SECRET = secrets.token_bytes(32)


def issue_token(user_id: str, login: str, role: str, object_id: str) -> str:
    """签发无状态令牌。返回 payload.sig 串。

    payload 含 user_id / login / role / object_id / iat（签发时间戳）。
    🔴 不含 password_hash（令牌里绝不带敏感字段）。
    """
    payload = {
        "user_id": user_id,
        "login": login,
        "role": role,
        "object_id": object_id,
        "iat": int(time.time()),
    }
    pb = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(_SECRET, pb.encode("ascii"), hashlib.sha256).digest()
    sb = base64.urlsafe_b64encode(sig).decode("ascii")
    return f"{pb}.{sb}"


def verify_token(token: str) -> dict | None:
    """校验令牌签名，返回 payload dict 或 None（无效/篡改）。"""
    if not token or "." not in token:
        return None
    pb, _, sb = token.partition(".")
    try:
        sig = base64.urlsafe_b64decode(sb.encode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(_SECRET, pb.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):  # 常量时间比较，防时序攻击
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(pb.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or "user_id" not in payload:
        return None
    return payload


def resolve_context(headers: dict) -> dict | None:
    """从请求头解析登录上下文。

    约定（docs/07 通用约定）：Authorization: Bearer <token>。
    无/格式错/签名无效 → None（由 require_auth 转 401）。
    """
    auth_header = headers.get("authorization", "") or ""
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):].strip()
    return verify_token(token)


def require_auth(fn: Callable[..., Any]) -> Callable[..., Any]:
    """装饰器：要求登录。未登录返回 (401, {"error":{"code","message"}})。

    用法（api_*.py）：
        @auth.require_auth
        def handle(method, path, query, body, headers, context=None):
            user_id = context["user_id"]
            ...
    """
    def wrapper(method: str, path: str, query: dict, body: bytes, headers: dict, context=None):
        ctx = resolve_context(headers)
        if ctx is None:
            return 401, {"error": {"code": "unauthorized", "message": "未登录或令牌无效"}}
        return fn(method, path, query, body, headers, context=ctx)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper
