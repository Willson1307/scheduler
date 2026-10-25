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
     🔴 **登录名不存在时同样计数**（对称计数）→ 不能用「会不会 429」判断账号是否存在；
     且对不存在用户补一次等时 PBKDF2 → **也不能用响应耗时判断**。见 §S3 修复注释。
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

#: 等时补偿用的哑 hash（格式合法、内容恒不匹配）。
#: 用于「登录名不存在」分支补齐一次 PBKDF2 计算，消除计时侧信道。
#: 🔴 不要改成 None 或空串——`verify_password` 解析失败会提前 return，耗时又变短了。
_DUMMY_HASH = "pbkdf2$" + "00" * 16 + "$" + "00" * 32


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
            # 🔴 2026-10-25 修复第 15 处静默失效（CP-H S3 / docs/21 §6.3 P1 返修项②）：
            #    **登录名枚举可被 HTTP 状态码 + 响应耗时双重区分**。
            #
            # 历史（别重蹈）：
            #   10-11 那版为修「拼错登录名把真实用户锁死」，改成「登录名不存在时完全不计数」。
            #   副作用是把枚举信号放大成两条硬区分：
            #     真实账号错密 5 次 → 第 6 次 429（锁定）
            #     不存在账号     → 永远 401（永不锁定）
            #   代码注释当时写「攻击者仍无法区分」——**实测为假**，误导了 14 天。
            #
            # 现修法（两条通道一起堵）：
            #   ① **对称计数**：不存在的登录名失败同样累加，同样在第 _LOCK_THRESHOLD+1 次返回 429。
            #      状态码序列完全一致 → 无法据此判断账号是否存在。
            #   ② **等时补偿**：`og.authenticate()` 对「用户不存在 / 非 active」**直接 return None、
            #      不跑 PBKDF2**，而正常用户要跑 10 万轮 → 两者响应耗时差约一倍，
            #      **计时侧信道比状态码更好利用**。
            #      故仅在这两种「压根没算过哈希」的情况下，对一个恒不匹配的哑 hash
            #      补跑一次 `verify_password`，把耗时拉平。
            #      ⛔ 域层 object_graph.py 只读，不能改 authenticate，只能在服务层补偿。
            #      ⚠️ 只能补「没跑过」的情况——对「跑过一次的」再补会变成两次，
            #         反而让不存在账号快一倍（实测 0.497 倍，本文件第一版就踩了这个坑）。
            #
            # ⚠️ 已知副作用（如实记录，不隐瞒）：
            #   攻击者现在可以「锁定任意登录名」——包括真实账号。但这条路**在修前同样存在**
            #   （直接打真实账号就能锁），本修复没有新增 DoS 面。
            #   拼错登录名只会锁住那个拼错的串，不会波及正确登录名（计数按 login 字符串隔离）。
            row = conn.execute(
                "SELECT status FROM users WHERE login = ? LIMIT 1", (login_name,)
            ).fetchone()
            hashed_already = row is not None and row["status"] == "active"
            if not hashed_already:
                og.verify_password(password, _DUMMY_HASH)  # 等时补偿，结果恒 False
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
