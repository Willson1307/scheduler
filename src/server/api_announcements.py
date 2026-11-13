"""组织公告接口（卡 rTVvb8）：GET | POST | DELETE /api/announcements。

路由（精确匹配、**无 path 参数**，id 一律走 query —— http_api.py:129）：
  GET    /api/announcements?group=
  POST   /api/announcements            body {group?, title, body_md}
  DELETE /api/announcements?group=&id=

🔴 本文件只是接口层：取参数 → 调 b_announcement_kernels → 拼响应。
   读写规则（upsert / 软删 / 组织过滤）只在核心里实现一份。

🔴 权限（不重复 B3 那次「硬编码 context=None」的事故）：
   · READ  = 登录 + 组织隔离（B3.resolve_authorized_org，view 档即可）
   · WRITE = `own` 档（「修改组」，docs/06）。教师天花板 = propose ⇒ 恒 403。
   · 顺序刻意是「解析组织 → 档位闸 → 参数校验 → 落库」：教师带非法参数
     也稳定拿 403，不会因为 400/404 提前暴露「这个 id 存不存在」。

🔴 错误体统一用**嵌套**格式 {"error":{"code","message"}}（契约 §7）。
   但 `auth.require_auth` 的 401 与 `B3.resolve_authorized_org` 抛的 ApiError
   都是**扁平**的 —— 后者必须在本文件捕获并转换，否则同一个接口两套错误体。

🔴 正文 body_md 是 Markdown 原文：本文件**不转义、不 strip**。防 XSS 靠前端
   （textContent / mdToDom，禁 innerHTML），后端只保证「原样存、原样还」。

只用 Python 标准库。
"""

from __future__ import annotations

from . import auth, config
from . import b3_kernels as B3
from . import b_announcement_kernels as K
from .http_api import ApiError, json_body

#: 标题长度上限。前端 input 也写 200，两处必须一致（不一致时后端为准）。
TITLE_MAX_LEN = 200

#: ApiError.status → 契约错误码。ApiError 只带 message，没有 code。
_CODE_BY_STATUS = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found"}


def _err(status: int, code: str, message: str):
    """契约 §7 的统一嵌套错误体（不用 ApiError 的扁平体，前端只需一套解析）。"""
    return status, {"error": {"code": code, "message": message}}


def _err_from_api_error(exc: ApiError):
    """把 ApiError（扁平体）转成本接口的嵌套体。"""
    return _err(exc.status, _CODE_BY_STATUS.get(exc.status, "error"), exc.message)


def _resolve_org(conn, user_id: str, group: str | None):
    """组织解析 + 越权闸，返回 `((effective_group, org_id), None)` 或 `(None, 错误响应)`。

    B3 抛 ApiError（403「无权访问该组」/「无授权行」），在分发层会变成扁平体，
    所以这里必须拦下来转成嵌套体。
    """
    try:
        return B3.resolve_authorized_org(conn, user_id, group), None
    except ApiError as exc:
        return None, _err_from_api_error(exc)


def _require_own(conn, user_id: str, effective_group: str):
    """写入口的档位闸：不满足 own 一律 403，返回错误响应否则 None。

    ⚠️ 判据是**当前组**的有效档位（authz.effective_level 已含继承与天花板）：
       给教师授 own 也只到 propose，所以教师必 403；管理员在自组织内短路为 own。
       前端「编辑公告」入口用的是同一口径（GET /api/groups 的 level），
       两边同源，不会出现「看得到按钮但一点就 403」。
    """
    from scheduler import authz

    if not authz.can(conn, user_id, effective_group, "own"):
        return _err(
            403,
            "forbidden",
            "需要 own 档权限（修改组织结构与规则）才能发布/停用公告；"
            "教师最高档为 propose。",
        )
    return None


def _public(row: dict | None) -> dict | None:
    """内核行 → 对外字段。

    ⛔ 不回传 org_id / active / updated_by / created_at：
       组织由请求的 group 决定，前端不该拿到 org_id 去跨组；其余是内部审计列。
    """
    if row is None:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "body_md": row["body_md"],
        "updated_at": row["updated_at"],
        "updated_by_name": row["updated_by_name"],
    }


@auth.require_auth
def get_announcement(method: str, path: str, query: dict, body: bytes, headers: dict, context=None):
    """GET /api/announcements?group=

    200 `{"ok":true,"announcement":{…}}`；无公告/已停用 → `announcement: null`
    （⛔ 不是 404、不是 {}：前端要能区分「没有公告」与「接口失败」）。
    """
    group = B3.parse_group(query)
    conn = config.open_db()
    try:
        K.ensure_announcements(conn)
        resolved, err = _resolve_org(conn, context["user_id"], group)
        if err is not None:
            return err
        _effective_group, org_id = resolved
        return {"ok": True, "announcement": _public(K.get_active(conn, org_id))}
    finally:
        conn.close()


@auth.require_auth
def save_announcement(method: str, path: str, query: dict, body: bytes, headers: dict, context=None):
    """POST /api/announcements —— 发布或覆盖（每组织恒一条）。

    group 从**请求体**取（与 api_period_sets 的 data.get("group") 一致）；
    缺省 None → 走用户默认组（同 §4 口径）。
    """
    try:
        data = json_body(body)
    except ApiError as exc:
        return _err_from_api_error(exc)

    group = data.get("group")
    if group is not None and not isinstance(group, str):
        # 防 int / list 传进 SQL 参数变 500
        return _err(400, "bad_request", "group 必须是字符串（组 id）")
    group = group or None

    conn = config.open_db()
    try:
        K.ensure_announcements(conn)
        resolved, err = _resolve_org(conn, context["user_id"], group)
        if err is not None:
            return err
        effective_group, org_id = resolved

        err = _require_own(conn, context["user_id"], effective_group)
        if err is not None:
            return err

        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            return _err(400, "bad_request", "缺少 title（公告标题，不能为空白）")
        title = title.strip()
        if len(title) > TITLE_MAX_LEN:
            return _err(
                400, "bad_request", f"title 过长（上限 {TITLE_MAX_LEN} 字，当前 {len(title)}）"
            )

        body_md = data.get("body_md", "")
        if body_md is None:
            body_md = ""
        if not isinstance(body_md, str):
            return _err(400, "bad_request", "body_md 必须是字符串（Markdown 原文）")

        # 顺序：先 strip 标题，正文原样入库（XSS 往返保真：不转义、不 strip）
        result = K.upsert(conn, org_id, context["user_id"], title, body_md)
        return {
            "ok": True,
            "created": result["created"],
            "announcement": _public(result["announcement"]),
        }
    finally:
        conn.close()


@auth.require_auth
def delete_announcement(method: str, path: str, query: dict, body: bytes, headers: dict, context=None):
    """DELETE /api/announcements?group=&id= —— 停用（软删，不删行）。

    重复停用返回 200 + `already_inactive: true`（幂等，双击不报错）；
    id 不存在**或属于别的组织** → 404（不泄漏「这个 id 在别处存在」）。
    """
    ann_id = (query.get("id") or [""])[0] or None
    if not ann_id:
        return _err(400, "bad_request", "缺少 id（DELETE /api/announcements?id=ann_x）")

    group = B3.parse_group(query)
    conn = config.open_db()
    try:
        K.ensure_announcements(conn)
        resolved, err = _resolve_org(conn, context["user_id"], group)
        if err is not None:
            return err
        effective_group, org_id = resolved

        err = _require_own(conn, context["user_id"], effective_group)
        if err is not None:
            return err

        outcome = K.deactivate(conn, org_id, ann_id, context["user_id"])
        if outcome is None:
            return _err(404, "not_found", f"公告不存在：{ann_id}（或不属于当前组织）")

        return {
            "ok": True,
            "id": ann_id,
            "active": False,
            "already_inactive": outcome == K.ALREADY_INACTIVE,
        }
    finally:
        conn.close()


ROUTES = {
    ("GET", "/api/announcements"): get_announcement,
    ("POST", "/api/announcements"): save_announcement,
    ("DELETE", "/api/announcements"): delete_announcement,
}
