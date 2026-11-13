"""B9（S21）：POST|DELETE|GET /api/exceptions 临时微调接口（docs/07 §11）。

用途与设计意图（答辩口径）：
  1. **「某条周期日程的某一天」临时取消 / 改点**。取消后相关冲突**立即**
     消失（领域层 build_occupancy 在展开之后逐日撤掉占用，冲突检测走同一
     份 entries），删掉例外**完全还原**（例外行一删，days 恢复 expand_*
     的原始结果）——这两条是教师敢不敢用的前提，也是本接口的验收主线。
  2. **只在写入口校验，不在读路径兜底**：取消/改点的目标槽位合不合法
     （Q1 四条校验链）在 POST 时一次判清，全部 400。读路径
     （build_occupancy / m2.schedule_masks）只负责**如实应用**，
     不做二次猜谜——猜出来的「合理值」正是 P0-3 那 5 处静默跳过的病根。
  3. 🔴 **不重复例外**：同 (日程, 日期) 已有 cancel/move 时返回 409 并带上
     已有例外的 id，让前端直接引导「先撤销旧的」。不拦的后果见
     b9_kernels.find_active 的 docstring（静默失效 / 全组织 500）。
  4. 鉴权（照 B3/B4/B5 范式）：@require_auth 拦 401；group 走
     K.resolve_authorized_org 拦跨组织 403。契约 §11 写的「需 edit 档」
     在代码里没有对应实现（docs/06 的四档是 view/propose/approve/own，
     teacher 天花板就是 propose），队长裁定按卡面执行：**登录 + 组织隔离**。
     真正卡「谁能批准」的是审批流（docs/06 已冻结、尚未进契约），
     不是本接口。
  5. 数据合规（AGENTS §7）：本接口纯本地 SQLite 读写，不外发任何内容。

签名约定（写死，不得改）：
  handle(method, path, query, body, headers, context=None)

只用 Python 标准库。
"""

from __future__ import annotations

from datetime import date

from . import auth, config
from . import b3_kernels as B3
from . import b9_kernels as K
from .http_api import ApiError, json_body


# ---------------------------------------------------------------------------
# POST /api/exceptions —— 新增一条例外
# ---------------------------------------------------------------------------


@auth.require_auth
def create_exception(method, path, query, body, headers, context=None):
    """POST /api/exceptions（docs/07 §11）。

    请求：{"schedule_id":"sch_x","date":"2026-09-23","mode":"cancel"}
          {"schedule_id":"sch_x","date":"2026-09-23","mode":"move","new_mask":"0x0008"}
    成功 200：{"ok":true,"exception_id":"exc_x","affected_dates":["2026-09-23"],
               "mode":"cancel","effective":true}

    状态码：
      401 未登录（@require_auth）
      403 跨组织越权（K.resolve_authorized_org）
      400 参数缺/格式错/mode 非法/new_mask 不过校验链/目标不是周期日程
      404 日程不存在或不属于当前组织
      409 同 (日程, 日期) 已有一条 cancel/move 例外

    🔴 `affected_dates` 恒为 [date]（契约 §11 定死）。它是「这条例外声称
    影响哪些天」，不是「实际改变了哪些天」——后者由 `effective` 如实回报，
    两个字段刻意分开，避免用一个字段表达两件事。
    """
    data = json_body(body)
    # 契约 §11 的字段名是 schedule_id；docs/12 的示例用过 target_id。
    # 🔴 两个都收，但**同时给且不一致时明确 400**——静默选一个会让
    # 前端以为自己传的那个生效了，是典型的静默失效。
    schedule_id = data.get("schedule_id")
    if schedule_id is None:
        schedule_id = data.get("target_id")
    elif data.get("target_id") is not None and data["target_id"] != schedule_id:
        raise ApiError(
            400,
            "schedule_id 与 target_id 不一致——请只传其中一个（契约口径：schedule_id）",
        )
    iso = data.get("date")
    mode = data.get("mode")
    new_mask_raw = data.get("new_mask")
    note = data.get("note")

    if not isinstance(iso, str) or not iso.strip():
        raise ApiError(400, "缺少 date（这条例外影响哪一天，YYYY-MM-DD）")
    iso = K.parse_date(iso)

    if not isinstance(mode, str) or mode not in K.EXCEPTION_MODES:
        raise ApiError(
            400,
            f"mode 必须是 {'/'.join(K.EXCEPTION_MODES)}，收到 {mode!r}"
            "（本接口不开放 special：没有对应规则支撑前不开放无语义写入路径）",
        )
    if mode == "move" and new_mask_raw is None:
        raise ApiError(400, "mode='move' 必须带 new_mask（改到哪些节次，如 \"0x0008\"）")
    if mode == "cancel" and new_mask_raw is not None:
        raise ApiError(400, "mode='cancel' 不接受 new_mask（取消是整节课不要，不是改节次）")
    if note is not None and not isinstance(note, str):
        raise ApiError(400, "note 必须是字符串")

    cfg = config.get_current_config()
    conn = config.open_db()
    try:
        # 🔴 旧库兜底：已存在的 data/*.db 不会自动补 schedule_exceptions 表
        # （ensure_database 只在库文件不存在时 seed）→ 缺表时本接口会 500。
        # 与 b5_kernels.ensure_demands 同一范式。
        K.ensure_schedule_exceptions(conn)

        _effective_group, org_id = B3.resolve_authorized_org(
            conn, context["user_id"], data.get("group")
        )

        target = K.load_target(conn, org_id, schedule_id)
        dow = date.fromisoformat(iso).isoweekday()

        # new_mask 校验链（Q1）：必须在拿到 cfg 与 dow 之后做——
        # 「当天有哪些启用槽位」只有 cfg 知道。
        slot_cfg = K.load_slot_config(conn, org_id)
        new_mask_week = None
        if mode == "move":
            new_mask_week = K.parse_api_mask(new_mask_raw, dow, slot_cfg)

        # 重复例外 → 409（不拦的后果见 find_active docstring）
        existing = K.find_active(conn, org_id, target["id"], iso)
        if existing is not None:
            # message 一字不改（前端已按它显示），另把 id / code **结构化**地
            # 平铺进响应体：原先只有文案里那句 exception_id=exc_xxx，
            # 前端只能正则抠字符串，message 一改就静默失配。
            raise ApiError(
                409,
                f"{iso} 已存在对该日程的 {existing['mode']} 例外"
                f"（exception_id={existing['id']}）——请先撤销它再新建",
                extra={"code": "conflict", "exception_id": existing["id"]},
            )

        effective = K.exception_effective(
            conn, org_id, target, iso, slot_cfg, mode, new_mask_week
        )
        exception_id = K.create(
            conn, org_id, target["id"], iso, mode, new_mask_week, note
        )
    finally:
        conn.close()

    return {
        "ok": True,
        "exception_id": exception_id,
        "affected_dates": [iso],
        "mode": mode,
        "schedule_id": target["id"],
        "effective": effective,
    }


# ---------------------------------------------------------------------------
# DELETE /api/exceptions —— 撤销一条例外（改错了必须能回退）
# ---------------------------------------------------------------------------


@auth.require_auth
def revoke_exception(method, path, query, body, headers, context=None):
    """DELETE /api/exceptions?id=exc_x（docs/07 §11「DELETE 即撤销」）。

    成功 200：{"ok":true,"exception_id":"exc_x","affected_dates":["2026-09-23"]}
    401 未登录；403 跨组织；400 缺 id；404 例外不存在或不属于当前组织。

    🔴 撤销后**完全还原**：例外行一删，load_exceptions 不再产出该 key，
    apply_schedule_exceptions 对该日无操作，days 恢复 expand_* 的原始结果。
    这条是「教师不敢用」的直接解药，tests/test_b9_exceptions.py 有对应用例。
    """
    raw_id = (query.get("id") or [""])[0]
    cfg = config.get_current_config()
    conn = config.open_db()
    try:
        K.ensure_schedule_exceptions(conn)
        _group, org_id = B3.resolve_authorized_org(conn, context["user_id"], None)
        info = K.revoke(conn, org_id, raw_id)
    finally:
        conn.close()

    return {
        "ok": True,
        "exception_id": info["exception_id"],
        "affected_dates": [info["date"]],
    }


# ---------------------------------------------------------------------------
# GET /api/exceptions —— 列出某条日程的未撤销例外
# ---------------------------------------------------------------------------


@auth.require_auth
def list_exceptions(method, path, query, body, headers, context=None):
    """GET /api/exceptions?schedule_id=sch_x → 200。

    成功 200：{"count":2,"items":[{"id","schedule_id","title","date","mode",
               "new_mask","readable","note","created_at"}]}
    401 未登录；403 跨组织；400 缺 schedule_id；404 日程不存在。

    🔴 为什么契约只写了 POST|DELETE 而本文件还加 GET：没有 GET，前端
    就无法在周视图上标出「这天有临时取消/改点」，教师既看不见自己改过
    什么，也不敢点撤销（§12.3 新增接口属非破坏性扩展，不动已有两个方法）。
    """
    raw_id = (query.get("schedule_id") or [""])[0]
    cfg = config.get_current_config()
    conn = config.open_db()
    try:
        K.ensure_schedule_exceptions(conn)
        _group, org_id = B3.resolve_authorized_org(conn, context["user_id"], None)
        # 复用 load_target：顺带拿到 404（不存在/跨组织）与「必须周期日程」校验
        target = K.load_target(conn, org_id, raw_id)
        items = K.list_for_schedule(conn, org_id, target["id"])
    finally:
        conn.close()

    return {"count": len(items), "items": items}


ROUTES = {
    ("POST", "/api/exceptions"): create_exception,
    ("DELETE", "/api/exceptions"): revoke_exception,
    ("GET", "/api/exceptions"): list_exceptions,
}
