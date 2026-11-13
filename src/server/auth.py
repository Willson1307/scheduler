"""B2 鉴权中间件：令牌签发/校验 + require_auth 装饰器（分支B）。

用途与设计意图（答辩口径）：
  1. 令牌用标准库 hmac+secrets 签名（无状态、断网可用、零第三方依赖）：
     payload=<base64(json)> . sig=<base64(hmac_sha256(payload, SECRET))>。
     无状态 = 重启不依赖外部存储；SECRET 持久化在 <仓库根>/data/.auth_secret
     （data/ 整目录已 gitignore，密钥不出本机），重启后旧令牌**仍然有效**，
     修复「重启即全员 401 重新登录」（rPm5gf 问题 9.1）。
     密钥来源优先级：环境变量 SCHEDULER_AUTH_SECRET（64 位 hex，测试/多实例
     场景）> data/.auth_secret 文件（首次自动生成，权限 0600）> 进程级随机
     （文件系统不可写时的兜底，此时退回旧行为：重启令牌失效）。
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
import os
import secrets
import time
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 签名密钥：环境变量 > data/.auth_secret 文件 > 进程级随机（兜底）
# ---------------------------------------------------------------------------


def _secret_file_path() -> str:
    """<仓库根>/data/.auth_secret（src/server/auth.py 上溯三级 = 仓库根）。

    data/ 整目录在 .gitignore（含真实库文件），密钥落这里不会入库、不出本机。
    🔴 路径锚定文件位置而非当前工作目录，与 config.repo_root() 同一手法。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, "data", ".auth_secret")


def _load_or_create_secret() -> bytes:
    """加载持久化密钥；不存在则生成并写盘（0600）；写不了盘则退回进程级随机。

    修复（rPm5gf 问题 9.1）：原实现每次重启重新随机 → 全量 token 失效，
    开发/演示中表现为「登录成功、过会儿 401 又让登录」。持久化后重启不失效。
    """
    env = os.environ.get("SCHEDULER_AUTH_SECRET", "")
    if env:
        try:
            return bytes.fromhex(env)
        except ValueError:
            pass  # 非法 hex 忽略，走文件/随机
    path = _secret_file_path()
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) >= 32:
            return data
    except OSError:
        pass  # 文件不存在/不可读 → 尝试生成
    secret = secrets.token_bytes(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secret)
        finally:
            os.close(fd)
        return secret
    except OSError:
        return secret  # 写盘失败（只读介质等）→ 本次进程内使用，退回旧行为


_SECRET = _load_or_create_secret()


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


def _account_disabled(user_id: str) -> bool:
    """账号是否已被停用（`users.status='disabled'`）。

    🔴 2026-11-13 补（外部审查发现）：令牌是**无状态**的，签发后不再回看数据库，
    于是「管理员停用账号」只挡住了新登录，**旧令牌仍能调接口、甚至写入数据**
    （实测：停用后新登录 401，旧令牌 `POST /api/import/csv` 返回 200 并新增 1 行）。

    判定刻意保守：**只有明确查到 status='disabled' 才拒绝**。
    用户行不存在（测试里直接签发的老令牌）或库暂时不可用 → 维持旧行为，
    不因为「查不到」把所有人踢下线（那会造成比原缺陷更大的可用性事故）。
    """
    if not user_id:
        return False
    try:
        from . import config  # 延迟导入：避免 server 包初始化期的循环依赖

        conn = config.open_db()
    except Exception:
        return False
    try:
        row = conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()
    except Exception:
        return False
    finally:
        conn.close()
    return row is not None and row[0] == "disabled"


def resolve_context(headers: dict) -> dict | None:
    """从请求头解析登录上下文。

    约定（docs/07 通用约定）：Authorization: Bearer <token>。
    无/格式错/签名无效/**账号已停用** → None（由 require_auth 转 401）。
    """
    auth_header = headers.get("authorization", "") or ""
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):].strip()
    payload = verify_token(token)
    if payload is None:
        return None
    # 🔴 令牌有效不等于账号有效：停用后旧令牌必须立刻失效（见 _account_disabled）
    if _account_disabled(payload.get("user_id")):
        return None
    return payload


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
