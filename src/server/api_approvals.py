"""B8（审批流）：/api/approvals 提议→批准→落库（docs/06 §3/§4）。

用途与设计意图（答辩口径）：
  1. 永久类改动（改周期日程的 valid_from）不直接生效，走「提出请求 →
     批准 → 落库」五态 + 审计日志。draft/pending 绝不碰业务数据；
     只有 approved 才落库，落库成功才转 applied。
  2. 🔴 批准时重跑硬约束（b8_kernels.validate_apply）：提议时合法 ≠
     批准时合法（期间别人可能改了课表），校验不过 409 且零写入。
  3. 鉴权（照 B3/B4/B5/B9 范式）：@require_auth 拦 401；提交/列表走
     B3.resolve_authorized_org 拦跨组织；批准/拒绝走
     b8_kernels.require_approve_in_org（approve 档以上）。
  4. 🔴 审批流的灵魂：自己批准自己的请求 → 403（requester_id ==
     context["user_id"]，与 level 无关）；teacher 天花板 propose ⇒ 老师批不了。
     rCMOFk 缺陷2：同级也批不了——批准人档位必须**严格高于**提出人
     （K.require_higher_level，两个 admin 互批 → 403）。
  5. 数据合规（AGENTS §7）：纯本地 SQLite 读写，不外发任何内容。

🔴 URL 形态：现有路由是**精确匹配**（无 path 参数），按 id 操作的端点
  一律用 **query 传 id**（与 B9 `DELETE /api/exceptions?id=` 同款约定），
  不解析 `/api/approvals/<id>/...` 这种 path 段。

签名约定（写死，不得改）：
  handle(method, path, query, body, headers, context=None)

只用 Python 标准库。
"""

from __future__ import annotations

import json
from datetime import date

from . import auth, config
from . import b3_kernels as B3
from . import b8_kernels as K
from . import b9_kernels as B9
from . import b_explain_kernels as X
from .http_api import ApiError, json_body


# ---------------------------------------------------------------------------
# 提交（draft 直接建为 pending）
# ---------------------------------------------------------------------------


@auth.require_auth
def submit_request(method, path, query, body, headers, context=None):
    """POST /api/approvals

    请求：{"scope":"permanent","op":"update","target_type":"schedule",
           "target_id":"sch_x","payload":{"after":{"valid_from":"2026-10-20"}},
           "reason":"该老师这周外出，物理从 10/13 顺延到 10/20"}
    成功 200：{"ok":true,"request_id":"cr_x","status":"pending"}

    🔴 提交只登记请求 + 抓 before 快照，**绝不改任何业务数据**。
    """
    data = json_body(body)
    scope = data.get("scope")
    op = data.get("op")
    target_type = data.get("target_type")
    target_id = data.get("target_id")
    payload = data.get("payload")
    reason = data.get("reason")

    # -- 本卡只做这一条写路径，其余组合 400（不是静默跳过）--
    if scope != "permanent":
        raise ApiError(400, f"scope 只支持 'permanent'，收到 {scope!r}")
    if op != "update":
        raise ApiError(400, f"op 只支持 'update'，收到 {op!r}")
    if target_type != "schedule":
        raise ApiError(400, f"target_type 只支持 'schedule'，收到 {target_type!r}")
    if not isinstance(target_id, str) or not target_id.strip():
        raise ApiError(400, "缺少 target_id（要改哪条周期日程的起始日）")
    if not isinstance(payload, dict) or not isinstance(payload.get("after"), dict):
        raise ApiError(400, "缺少 payload.after（{valid_from: 'YYYY-MM-DD'}）")
    new_valid_from = payload["after"].get("valid_from")
    if new_valid_from is None:
        raise ApiError(400, "缺少 payload.after.valid_from（新的起始日）")
    new_valid_from = B9.parse_date(new_valid_from)  # 严格 YYYY-MM-DD

    cfg = config.get_current_config()
    conn = config.open_db()
    try:
        K.ensure_change_tables(conn)

        _group, org_id = B3.resolve_authorized_org(
            conn, context["user_id"], data.get("group")
        )
        # 目标必须存在且属于本组织（404 不泄露存在性；非周期 → 400）。
        target = B9.load_target(conn, org_id, target_id)

        # 🔴 倒置区间拦截（rCMOFk 缺陷3）：after.valid_from 晚于目标 valid_to
        #    时不能放行——落库后区间反转（valid_from > valid_to）。原实现提交、
        #    批准都放行，坏数据静默入库。这里在提交时就拦（400 零写入）。
        if date.fromisoformat(new_valid_from) > date.fromisoformat(target["valid_to"]):
            raise ApiError(
                400,
                f"区间倒置：新起始日 {new_valid_from} 晚于目标结束日"
                f" {target['valid_to']}（须满足 valid_from <= valid_to）",
            )

        before = K.capture_before(conn, target)
        payload_json = json.dumps(
            {"before": before, "after": {"valid_from": new_valid_from}},
            ensure_ascii=False,
        )
        request_id = K.new_request_id()
        conn.execute(
            "INSERT INTO change_requests"
            " (id, org_id, requester_id, scope, op, target_type, target_id,"
            "  payload_json, reason, status)"
            " VALUES (?,?,?,'permanent','update','schedule',?,?,?,'pending')",
            (
                request_id,
                org_id,
                context["user_id"],
                target_id.strip(),
                payload_json,
                reason if isinstance(reason, str) else None,
            ),
        )
        K.record_event(conn, request_id, context["user_id"], "submit")
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "request_id": request_id, "status": "pending"}


# ---------------------------------------------------------------------------
# 列表（本组织，org 过滤）
# ---------------------------------------------------------------------------


@auth.require_auth
def list_requests(method, path, query, body, headers, context=None):
    """GET /api/approvals?status=&group=

    只返回调用者所在组织的请求（跨组织一律不可见），且只返回
    **本人 + 档位严格高于提出人**的请求（rRK0Q5 读取侧可见性，
    详见 b8_kernels.can_view_request 与 docs/07 §14）。
    """
    status = (query.get("status") or [""])[0]
    group = (query.get("group") or [""])[0] or None
    if status and status not in (
        "draft", "pending", "approved", "rejected", "withdrawn", "applied"
    ):
        raise ApiError(400, f"status 非法：{status!r}")

    cfg = config.get_current_config()
    conn = config.open_db()
    try:
        K.ensure_change_tables(conn)
        _group, org_id = B3.resolve_authorized_org(conn, context["user_id"], group)

        sql = "SELECT * FROM change_requests WHERE org_id = ?"
        params: list = [org_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at, id"
        rows = conn.execute(sql, params).fetchall()
        # 🔴 rRK0Q5 读取侧越权：SQL 只有 org 过滤是不够的——同组织内
        # 教师B 也能看到教师A 的申请（requester_id / reason / payload
        # 前后快照全泄露）。SQL 拿完行后按 can_view_request 过滤
        # （本人 或 档位严格高于提出人，与 require_higher_level 同一口径）；
        # count 是**过滤后**的数。两个 admin 互不可见是已知且可接受的
        # 副作用（own == own），见 b8_kernels.can_view_request 注释。
        rows = [r for r in rows if K.can_view_request(conn, context["user_id"], r)]
    finally:
        conn.close()

    items = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "org_id": r["org_id"],
                "requester_id": r["requester_id"],
                "scope": r["scope"],
                "op": r["op"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "payload": K.request_payload(r),
                "reason": r["reason"],
                "status": r["status"],
                "approver_id": r["approver_id"],
                "created_at": r["created_at"],
                "decided_at": r["decided_at"],
                "applied_at": r["applied_at"],
            }
        )
    return {"ok": True, "count": len(items), "requests": items}


# ---------------------------------------------------------------------------
# 批准（approve 档以上；自己不能批自己；批准时重跑校验）
# ---------------------------------------------------------------------------


@auth.require_auth
def approve_request(method, path, query, body, headers, context=None):
    """POST /api/approvals/approve?id=cr_x

    状态机：pending → approved →（重跑硬约束通过）→ applied，一个事务完成。
    409 撞车零写入；403 自己批自己 / 低档 / 跨组织；404 不存在 / target 已删。
    """
    request_id = (query.get("id") or [""])[0]

    cfg = config.get_current_config()
    conn = config.open_db()
    try:
        K.ensure_change_tables(conn)
        req = K.load_request(conn, request_id)

        if req["status"] != "pending":
            raise ApiError(409, f"请求当前状态是 {req['status']}，不可批准")

        # 🔴 审批流的灵魂：自己批准自己 → 403，无论 level 多高。
        if req["requester_id"] == context["user_id"]:
            raise ApiError(403, "不能批准自己提出的请求")

        K.require_approve_in_org(conn, context["user_id"], req["org_id"])

        # 🔴 只批下级（rCMOFk 缺陷2，docs/06 §1「approve = 批准/拒绝自己
        #    **下级**提出的请求」）：批准人在该组织的有效档必须**严格高于**
        #    提出人（view<propose<approve<own，沿用 authz.effective_level
        #    口径：admin 本组织短路 own，teacher 天花板 propose）。
        #    同级或更低 → 403（两个 admin 互批也拦：own == own 不严格高于）。
        K.require_higher_level(
            conn, context["user_id"], req["requester_id"], req["org_id"]
        )

        # 批准时重跑校验：按 after.valid_from 重新展开，撞车 → 409 零写入。
        target = B9.load_target(conn, req["org_id"], req["target_id"])  # 已删 → 404
        new_valid_from = K.request_payload(req)["after"]["valid_from"]

        # 🔴 竞态覆盖拦截（rCMOFk 缺陷4）：同目标两条 pending 先后批准时，
        #    第二条必须 409，不能静默覆盖第一条的落库结果。以提交时抓的
        #    before 快照为凭：目标当前 valid_from 已不是提议时记录的
        #    before.valid_from → 数据被他人改动 → 409「请重新提议」，零写入。
        before = K.request_payload(req)["before"]
        if target["valid_from"] != before.get("valid_from"):
            raise ApiError(409, "数据已被他人改动，请重新提议")

        slot_cfg = B9.load_slot_config(conn, req["org_id"])
        problems = K.validate_apply(conn, req["org_id"], target, new_valid_from, slot_cfg)
        if problems:
            # v1.1（卡 rHrY9D）出向 D3：把硬约束串翻成老师能读懂的拒绝理由。
            # 🔴 实测口径：`hard_violations()` 返回的**已经是中文、且已含对象名**
            #    （「高一(1)班 在该时段已被占用（第1节）」），不是英文枚举
            #    `teacher_overlap` —— 所以这里不是「英译中」，而是「术语 → 人话」。
            # 🔴 外发前先令牌化（真名 → 对象N），回来再还原；数字必须落在事实内，
            #    不接地就原样用模板句。任何失败都不影响 409 本身（拒绝照旧生效）。
            try:
                names = list(X.object_names(conn, context["user_id"]) or {})
            except Exception:  # noqa: BLE001
                names = []
            reason = X.explain_violations(problems, names=names)["explanation"]
            raise ApiError(409, "批准后与现有课表冲突：" + reason)

        user_id = context["user_id"]
        try:
            conn.execute(
                "UPDATE change_requests SET status='approved', approver_id = ?,"
                " decided_at = datetime('now') WHERE id = ?",
                (user_id, req["id"]),
            )
            after = K.apply_change(conn, req)
            conn.execute(
                "UPDATE change_requests SET status='applied',"
                " applied_at = datetime('now') WHERE id = ?",
                (req["id"],),
            )
            K.record_event(conn, req["id"], user_id, "approve")
            K.record_event(
                conn, req["id"], user_id, "apply", json.dumps(after, ensure_ascii=False)
            )
            conn.commit()
        except Exception:
            # 🔴 落库失败 → 回滚 + 状态回 pending（rCMOFk 缺陷1：原实现回滚成
            #    approved，请求就此僵死——approve/reject/withdraw 都只认
            #    pending，全部 409）。fail 审计事件照记。事务语义不变：
            #    校验→落库→转 applied→记事件仍是原子的，失败零半改
            #    （approved/applied 的 UPDATE 已随 rollback 一并撤销，
            #    approver_id/decided_at 也未落库，这里把状态显式钉回 pending）。
            conn.rollback()
            conn.execute(
                "UPDATE change_requests SET status='pending' WHERE id = ?",
                (req["id"],),
            )
            K.record_event(conn, req["id"], user_id, "fail")
            conn.commit()
            raise
    finally:
        conn.close()

    return {"ok": True, "request_id": req["id"], "status": "applied"}


# ---------------------------------------------------------------------------
# 拒绝（必须填 reason）
# ---------------------------------------------------------------------------


@auth.require_auth
def reject_request(method, path, query, body, headers, context=None):
    """POST /api/approvals/reject?id=cr_x   body {"reason":"..."}

    reason 必填非空；同样不能自己拒绝自己、需 approve 档以上。
    """
    request_id = (query.get("id") or [""])[0]
    data = json_body(body)
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ApiError(400, "拒绝必须填 reason（为什么不同意）")

    cfg = config.get_current_config()
    conn = config.open_db()
    try:
        K.ensure_change_tables(conn)
        req = K.load_request(conn, request_id)

        if req["status"] != "pending":
            raise ApiError(409, f"请求当前状态是 {req['status']}，不可拒绝")
        if req["requester_id"] == context["user_id"]:
            raise ApiError(403, "不能拒绝自己提出的请求")
        K.require_approve_in_org(conn, context["user_id"], req["org_id"])
        # 🔴 只拒下级（rCMOFk 缺陷2，与 approve 同口径）：同级或更低 → 403。
        K.require_higher_level(
            conn, context["user_id"], req["requester_id"], req["org_id"]
        )

        conn.execute(
            "UPDATE change_requests SET status='rejected', approver_id = ?,"
            " decided_at = datetime('now') WHERE id = ?",
            (context["user_id"], req["id"]),
        )
        K.record_event(
            conn, req["id"], context["user_id"], "reject",
            json.dumps({"reason": reason.strip()}, ensure_ascii=False),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "request_id": req["id"], "status": "rejected"}


# ---------------------------------------------------------------------------
# 撤回（仅提出人本人）
# ---------------------------------------------------------------------------


@auth.require_auth
def withdraw_request(method, path, query, body, headers, context=None):
    """POST /api/approvals/withdraw?id=cr_x   （仅 requester 本人）"""
    request_id = (query.get("id") or [""])[0]

    cfg = config.get_current_config()
    conn = config.open_db()
    try:
        K.ensure_change_tables(conn)
        req = K.load_request(conn, request_id)

        if req["requester_id"] != context["user_id"]:
            raise ApiError(403, "只能撤回自己提出的请求")
        if req["status"] != "pending":
            raise ApiError(409, f"请求当前状态是 {req['status']}，不可撤回")

        conn.execute(
            "UPDATE change_requests SET status='withdrawn',"
            " decided_at = datetime('now') WHERE id = ?",
            (req["id"],),
        )
        K.record_event(conn, req["id"], context["user_id"], "withdraw")
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "request_id": req["id"], "status": "withdrawn"}


# ---------------------------------------------------------------------------
# 审计轨迹（答辩演示用）
# ---------------------------------------------------------------------------


@auth.require_auth
def list_events(method, path, query, body, headers, context=None):
    """GET /api/approvals/events?id=cr_x

    查看某条请求的完整审计轨迹（submit→approve→apply…）。
    可见性与列表同口径（rRK0Q5）：本人 或 档位严格高于提出人
    （can_view_request）；不可见 → 403。原先只查 view 档组织隔离，
    任何本组织 view 档用户都能读他人请求的完整轨迹，同源越权。
    """
    request_id = (query.get("id") or [""])[0]

    cfg = config.get_current_config()
    conn = config.open_db()
    try:
        K.ensure_change_tables(conn)
        req = K.load_request(conn, request_id)
        K.require_org_access(conn, context["user_id"], req["org_id"])
        # 🔴 rRK0Q5：组织隔离之上再叠同一可见性判据——view 档不够，
        # 必须「本人 或 严格高于提出人」（教师B 读教师A 的轨迹 → 403）。
        # 错误体沿用 ApiError 扁平 {"error": msg}，不换风格。
        if not K.can_view_request(conn, context["user_id"], req):
            raise ApiError(403, "无权查看该请求（仅本人或档位高于提出人可见）")

        rows = conn.execute(
            "SELECT * FROM change_events WHERE request_id = ? ORDER BY rowid",
            (req["id"],),
        ).fetchall()
    finally:
        conn.close()

    events = []
    for r in rows:
        events.append(
            {
                "id": r["id"],
                "request_id": r["request_id"],
                "actor_id": r["actor_id"],
                "action": r["action"],
                "at": r["at"],
                "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
            }
        )
    return {"ok": True, "count": len(events), "events": events}


ROUTES = {
    ("POST", "/api/approvals"): submit_request,
    ("GET", "/api/approvals"): list_requests,
    ("POST", "/api/approvals/approve"): approve_request,
    ("POST", "/api/approvals/reject"): reject_request,
    ("POST", "/api/approvals/withdraw"): withdraw_request,
    ("GET", "/api/approvals/events"): list_events,
}
