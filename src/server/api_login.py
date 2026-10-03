"""B2 登录接口：POST /api/login（分支B）。

用途与设计意图（答辩口径）：
  1. 鉴权用领域层 authenticate()（object_graph.py），它返回含 password_hash
     的完整 users 行——🔴 服务层必须剔除，响应绝不回传 password_hash
     （docs/07 §1 安全铁律）。user 字段只回 {id, login, global_role}。
  2. global_role = users.role（admin/teacher/terminal/student），改名映射到契约字段。
  3. default_group_id 按勘误3：沿祖先链层级最高（depth-to-root 最小），
     并列才比 level / group_id。admin→根组，年级组长→年级，教师→其最高层级组。
  4. 限频防爆破：同一 login 连续失败 5 次 → 锁定 15 分钟返回 429
     （docs/07 §1）。成功登录清零。进程内 dict 计数（本地单进程足够）。
  5. 错误体用契约嵌套格式 {"error":{"code","message"}}，返回 (status, body) 元组。

签名约定（写死）：handle(method, path, query, body, headers, context=None)。
只用 Python 标准库。
"""

from __future__ import annotations

import time

from . import auth, config
from .http_api import json_body
from scheduler import authz, object_graph as og

# ---------------------------------------------------------------------------
# 失败计数与锁定（进程内，本地单进程足够；重启清零可接受）
# ---------------------------------------------------------------------------
_LOCK_THRESHOLD = 5  # 连续失败 5 次锁定
_LOCK_SECONDS = 15 * 60  # 锁 15 分钟
_fail_tracker: dict[str, dict] = {}  # login -> {"count": int, "lock_until": float}


def _is_locked(login_name: str, now: float) -> bool:
    rec = _fail_tracker.get(login_name)
    return bool(rec and rec.get("lock_until", 0) > now)


def _record_failure(login_name: str, now: float) -> None:
    rec = _fail_tracker.setdefault(login_name, {"count": 0, "lock_until": 0.0})
    rec["count"] += 1
    if rec["count"] >= _LOCK_THRESHOLD:
        rec["lock_until"] = now + _LOCK_SECONDS


def _clear_failures(login_name: str) -> None:
    _fail_tracker.pop(login_name, None)


def reset_locks() -> None:
    """清空失败计数与锁定（测试隔离用；生产不需调）。"""
    _fail_tracker.clear()


def login(method: str, path: str, query: dict, body: bytes, headers: dict, context=None):
    """POST /api/login。

    请求：{"login":"admin","password":"admin123"}
    成功 200：{"token","user":{id,login,global_role},"default_group_id"}
    坏凭证 401：{"error":{"code":"bad_credentials","message":"登录名或密码错误"}}
    锁定 429：{"error":{"code":"locked","message":"连续失败过多，请 15 分钟后再试"}}
    缺字段 400。
    """
    data = json_body(body)
    login_name = data.get("login")
    password = data.get("password")
    if not login_name or not password:
        return 400, {"error": {"code": "bad_request", "message": "缺少 login 或 password"}}

    now = time.time()
    if _is_locked(login_name, now):
        return 429, {"error": {"code": "locked", "message": "连续失败过多，请 15 分钟后再试"}}

    conn = config.open_db()
    try:
        user = og.authenticate(conn, login_name, password)
        if user is None:
            _record_failure(login_name, now)
            return 401, {"error": {"code": "bad_credentials", "message": "登录名或密码错误"}}

        # 成功：清失败计数
        _clear_failures(login_name)

        # 🔴 剔除 password_hash：令牌 payload 与响应 user 都不带
        token = auth.issue_token(
            user_id=user["id"],
            login=user["login"],
            role=user["role"],
            object_id=user["object_id"],
        )
        # default_group_id（勘误3）
        default_gid = authz.default_group_id(conn, user["id"])
    finally:
        conn.close()

    return {
        "token": token,
        "user": {
            "id": user["id"],
            "login": user["login"],
            "global_role": user["role"],  # users.role → 契约 global_role
        },
        "default_group_id": default_gid,
    }


ROUTES = {
    ("POST", "/api/login"): login,
}
