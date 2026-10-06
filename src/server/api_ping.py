"""S14 埋点写入接口：POST /api/ping（feat/ops）。

请求（docs/07 §10，口径以 docs/08-试用组织数据口径.md 为准）：
    {"event": "task_start", "session_id": "...", "payload": {...}, "group": "..."}
    - event / session_id 必填；payload 默认 {}；group 可选（教师正操作的班级）。
    - trial_code 由后端从登录名推导（trial_t01 → T01），前端不传、杜绝伪造。
    - org_kind 由后端按登录前缀判定（trial/demo）。

鉴权：@require_auth（未登录 401）；group 给定时走 b3_kernels.resolve_authorized_org
（跨组织越权 403，照 r13kE9 范式）。

只用 Python 标准库。
"""

from __future__ import annotations

from . import auth, b3_kernels, config, telemetry
from .http_api import json_body


@auth.require_auth
def ping(method, path, query, body, headers, context=None):
    data = json_body(body)
    event = data.get("event")
    session_id = data.get("session_id")
    payload = data.get("payload") or {}
    group = data.get("group")

    if not event or not session_id:
        return 400, {"error": {"code": "bad_request", "message": "缺少 event 或 session_id"}}
    if event not in telemetry.EVENTS:
        return 400, {"error": {"code": "bad_request", "message": f"未知事件：{event}"}}
    if not isinstance(payload, dict):
        return 400, {"error": {"code": "bad_request", "message": "payload 必须是对象"}}

    conn = config.open_db()
    try:
        telemetry.ensure_event_log(conn)
        if group is not None:
            # 跨组织越权出口：group 不在用户授权范围 → 403（r13kE9 同源）
            _, org_id = b3_kernels.resolve_authorized_org(conn, context["user_id"], group)
        else:
            org_id = telemetry.org_of_user(conn, context["object_id"])
        telemetry.write_event(
            conn,
            org_id=org_id,
            trial_code=telemetry.derive_trial_code(context["login"]),
            session_id=session_id,
            event=event,
            payload=payload,
            org_kind=telemetry.org_kind_of(conn, org_id),
        )
    finally:
        conn.close()
    return {"ok": True}


ROUTES = {
    ("POST", "/api/ping"): ping,
}
