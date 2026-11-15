"""NL 入向：POST /api/nl —— 一句话 → 结构化规则（只解析，不执行）。

契约：`docs/07-API契约.md §8`（S19 新增）写的就是这个接口，但一直没实现。
本文件把它补上，并按项目红线收窄：

    POST /api/nl   body {"text":"高一(1)班周三第1节的课想换个时间", "group":"obj_x"}
    → 200 {"intent":"reschedule","display":"…","params":{"object_id","object_name",
            "date","period"},"source":"model|local","validated":true,"reply":"…"}

设计边界（答辩口径，别说过头）：

1. 🔴 **只解析，不执行**：本接口不写任何表、不返回任何可执行动作。
   解析出的位置是「源位置」（哪条课、哪天、第几节），**目标位置由求解器回答**
   （前端拿 params 去 `GET /api/slots` 要候选）。这条分工是刻意的：
   「改到周四」涉及「周四=几号、那天第几节有空」两件事，都不是语言问题。
2. 🔴 **模型可选**：有模型走 `b_nl_kernels.parse_rule`（三道关）；
   没模型 / 断网 / 超时一律走 `parse_local`（本地正则，纯标准库）。
   两条通道输出**同一结构**，前端不需要知道走的是哪条 —— 但 `source` 如实写。
   线上实测：未配模型时 `parse_rule` 恒返回 None，只有本地通道才可用。
3. 🔴 **抽不全就返回 intent=null**，绝不猜。猜错班比答不上更糟。
   `validated` 只在「对象真实存在 + 日期在扫描窗口 + 该日确有这一节」时为 true。
4. 鉴权与隔离同 B3/B4/B5：401 由 `@require_auth` 拦；给了 group 就校验授权，越权 403。
5. 不用任何第三方库。

已知边界（如实）：
  - 本地通道只认「对象名 + 周几 + 第几节」，**不认「下周」「明天」「下节课」**
    这类相对指代 —— 它们要读当前日期与课表状态，属于求解器的事。
  - 一次只解析一条规则；一句话里两个改动会被整条判为解析失败。
"""

from __future__ import annotations

from datetime import date, timedelta

from . import auth, config
from . import b3_kernels as B3
from . import b_nl_kernels as NL
from .http_api import ApiError, json_body
from scheduler import authz
from scheduler.slots import load_slot_config

#: text 长度上限（与 /api/chat 的 question 同口径）。
MAX_TEXT = 500

#: 解析窗口上限：只在「本周扫描窗口」里找日期，避免把「周三」解成三个月后的周三。
_DOW_LABEL = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _bad(message: str) -> ApiError:
    return ApiError(400, message)


def _facts(conn, org_id: str):
    """本次解析要用的事实：对象名单、窗口内每周几的日期、每日启用节次。

    ⛔ 只读。与 `api_chat._gate_ctx` 同源（同一套 `resolve_window` / `load_slot_config`），
    但这里额外需要 **对象 id** —— 前端要拿它去调 `/api/slots`。
    """
    slot_cfg = load_slot_config(conn, org_id)
    rows = conn.execute(
        "SELECT id, name, kind FROM objects WHERE org_id = ? AND kind IN ('group','user')",
        (org_id,),
    ).fetchall()
    objects = [{"id": r["id"], "name": r["name"], "kind": r["kind"]} for r in rows if r["name"]]

    date_from, date_to = B3.resolve_window(config.get_current_config())
    dates_by_dow: dict[int, str] = {}
    enabled_periods: dict[str, list[int]] = {}
    day = date_from
    while day <= date_to:
        iso = day.isoformat()
        # 🔴 enabled_slots() 返回的 idx 已是 1-based「第几节」，不要再 +1
        enabled_periods[iso] = [idx for _dow, idx in slot_cfg.enabled_slots(day.isoweekday())]
        # 同一个周几在窗口里可能出现两次（两周窗口）→ 取最早那天，口径固定、可预期
        dates_by_dow.setdefault(day.isoweekday(), iso)
        day += timedelta(days=1)

    return objects, dates_by_dow, enabled_periods, date_from, date_to


def _resolve_org(conn, user_id: str, group: str | None) -> str:
    """group → org_id（带授权校验）；不给 group 就用默认组。"""
    gid = (group or "").strip()
    if not gid:
        gid = authz.default_group_id(conn, user_id) or ""
        if not gid:
            raise ApiError(403, "当前账号还没有任何组织授权，无法解析")
    # 🔴 返回的是 (effective_group_id, org_id) 元组，别整条当 org_id 用
    _gid, org_id = B3.resolve_authorized_org(conn, user_id, gid)
    return org_id


def _display(object_name: str, iso: str, period: int) -> str:
    """人话描述：`高一(1)班 · 周三 2026-09-09 第 1 节`。

    ⛔ 只做展示，前端不要拿它反解析（要数据用 params）。
    """
    try:
        dow = date.fromisoformat(iso).isoweekday()
    except ValueError:
        return f"{object_name} · {iso} 第 {period} 节"
    return f"{object_name} · {_DOW_LABEL[dow - 1]} {iso} 第 {period} 节"


@auth.require_auth
def nl(method, path, query, body, headers, context=None):
    """POST /api/nl：一句话 → 规则。只解析、不执行。

    200：{"intent","display","params","source","validated","reply"}
       解析不出来 → 200 + intent=null + reason（正常业务态，不是 4xx）
    400 缺 text / 超长；401 未登录；403 无组织授权或跨组织。
    """
    data = json_body(body) or {}
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _bad("缺少 text（要说的话）")
    text = text.strip()
    if len(text) > MAX_TEXT:
        raise _bad(f"text 不能超过 {MAX_TEXT} 字")

    user_id = context["user_id"]
    group = data.get("group")

    conn = config.open_db()
    try:
        org_id = _resolve_org(conn, user_id, group)
        objects, dates_by_dow, enabled_periods, date_from, date_to = _facts(conn, org_id)
        names = [o["name"] for o in objects]

        # ① 先走模型（三道关在 parse_rule 内部）；没配模型时它返回 None，不报错
        rule = None
        source = "model"
        try:
            rule = NL.parse_rule(
                text,
                [i["id"] for i in _intent_ids()],
                gate_ctx={
                    "objects": {n: 1 for n in names},
                    "from": date_from.isoformat(),
                    "to": date_to.isoformat(),
                    "periods": enabled_periods,
                },
            )
        except Exception:
            rule = None  # 模型层任何异常都不影响本地通道

        # ② 模型没给 → 本地正则
        if not rule:
            rule = NL.parse_local(text, names, dates_by_dow, enabled_periods)
            source = "local"

        if not rule:
            return {
                "intent": None,
                "display": "",
                "params": {},
                "source": source,
                "validated": False,
                "reason": "没听出「哪个班 / 周几 / 第几节」，请说全一点",
            }

        slots = rule.get("slots") or {}
        obj_name = str(slots.get("object") or "")
        iso = str(slots.get("date") or "")
        try:
            period = int(slots.get("period"))
        except (TypeError, ValueError):
            period = 0

        obj = next((o for o in objects if o["name"] == obj_name), None)
        validated = bool(
            obj and iso and period and period in (enabled_periods.get(iso) or [])
        )

        return {
            "intent": rule.get("intent_id"),
            "display": _display(obj_name, iso, period) if (obj_name and iso and period) else "",
            "params": {
                "object_id": obj["id"] if obj else "",
                "object_name": obj_name,
                "date": iso,
                "period": period,
            },
            "source": source,
            "validated": validated,
            "reply": str(rule.get("reply") or ""),
        }
    finally:
        conn.close()


def _intent_ids():
    """意图白名单（唯一事实来源 = b_ai_kernels.INTENTS，避免两处漂移）。"""
    try:
        from . import b_ai_kernels as K
        return [{"id": i} for i in K.intent_ids()]
    except Exception:
        return []


ROUTES = {
    ("POST", "/api/nl"): nl,
}
