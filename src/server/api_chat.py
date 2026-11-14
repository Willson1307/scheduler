"""B-6（rn6JxF）：POST /api/chat —— AI 问答层接口（三层降级，不接 AI 也能答）。

契约（卡面 rn6JxF 写死 + v1.1 卡 rHrY9D 只增字段）：
    POST /api/chat
    请求：{"question":"我想把周三下午的课调开，怎么弄？","group":"g_x"}
    返回 200：{"answer":"...","sources":[{"doc":"docs/30","section":"§3"}],
               "mode":"llm"|"retrieval"|"template","degraded":false,
               "grounded":true,
               "rule":{"intent_id":"...","slots":{},"source":"model"|"keyword"}}

用途与设计意图（答辩口径）：
  1. **本文件只做接口层**：参数校验 / 鉴权 / 组织隔离 / 取「库内事实」，
     然后把问题交给 `b_ai_kernels.answer()`（三层降级内核）。
     业务判断一行不写在这里 —— 内核是纯函数，可单测、可离线跑。
  2. 🔴 **v1.1 起本层多了一件事：为「入向翻译」提供两张事实表**（卡 rHrY9D）：
       · `gate_ctx`：库内真实对象名 / 本周扫描窗口 / 每日启用槽位
                     → 给内核做**取值关**（模型说的班名必须真实存在）
       · `facts`   ：`facts(intent_id, slots) -> {"text","sources"} | None`
                     → 模型解析出的规则由这里执行**只读查询**，数字全从库里来
     ⚠️ 这是对旧口径的**明确变更**（旧版写「本层不读任何组织数据」）：
     读是为了让 AI 说出的班级名与冲突数都是真的；**只读、不外发**——
     事实只在本机拼答案，送模型前一律令牌化 + 脱敏。
  3. 🔴 **`mode` 必填且必须如实**：前端要能显示「本次回答来自手册原文（未启用 AI）」。
     ⛔ 不能让老师以为所有回答都是 AI 生成的 —— 这是诚信要求，也是答辩要点
     （本项目 AI 只用于自然语言理解与结果解释，冲突检测与消解由自建求解器完成）。
  4. 🔴 **任何情况不返 500**：手册缺失、LLM 超时/挂掉/未配置、取事实失败，
     一律降级并在 `mode` / `degraded` 里如实写。问答挂了不影响排课主链路。
  5. 🔴 **AI 只能回答/解释，不能产出「改哪条日程」**：本接口不写任何表、
     不返回任何变更指令；要改课走 `/api/exceptions`（临时）或审批流（永久）。
  6. 鉴权照 B3/B4/B5 范式：`@require_auth` 拦 401；`group` 传了就校验
     `B3.resolve_authorized_org`（越权 → 403），与其它业务接口同口径。
     `group` 缺省不强制（不传也能答手册问题）。

已知边界（如实）：
  - 无独立限流：本接口挂在登录之后，`/api/resolve` 那样的次数限制这里没做。
    开 LLM 时一次回答最多占用一个线程 3 秒（入向）+ 15 秒（润色）。
  - `query=slots / schedules` 第一版**不执行**（枚举合法但会回退关键词）：
    查空位要完整区间参数、查课表要周视图网格，都不属于「一句话问一句答」。

签名约定（写死，不得改）：
  handle(method, path, query, body, headers, context=None)

只用 Python 标准库。
"""

from __future__ import annotations

from datetime import timedelta

from . import auth, config
from . import b3_kernels as B3
from . import b_ai_kernels as K
from . import b_explain_kernels as X
from .http_api import ApiError, json_body
from scheduler.slots import load_slot_config

#: 问题长度上限（字符）。防超长输入拖慢检索 / 撑爆送模型的请求体；
#: 500 字已远超「老师问一句话」的实际场景。
MAX_QUESTION_CHARS = 500

#: 一句话答案里最多列几条具体冲突（再多就成列表了，该去看冲突列表页）。
FACTS_MAX_ITEMS = 3

_DOW_LABEL = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _bad_request(message: str) -> tuple[int, dict]:
    """400 用契约嵌套格式（与 api_demands 一致，docs/07 通用约定）。"""
    return 400, {"error": {"code": "bad_request", "message": message}}


def _name_vocab(user_id: str) -> dict[str, str] | None:
    """当前用户所在组织里的**对象名 → 占位符**，交给问答层做精确脱敏。

    🔴 为什么需要它（2026-11-13 外部审查发现）：问答层的正则只能猜
    「姓氏+老师」「数字班名」这类**格式**；裸姓名（「王思勋负责的…」）和
    中文数字班名（「高一一班」）会原样送给外部模型。名单本来就在库里，
    取出来精确替换即可，不需要猜、也不会误伤普通词。

    ⛔ 只取名字，不取课表、不取名单明细；不落日志、不缓存、用完即丢。
    库不可用 / 用户无组织 → 返回 None（退回正则兜底，**不阻断问答**）。
    """
    try:
        conn = config.open_db()
    except Exception:
        return None
    try:
        names = X.object_names(conn, user_id)
        if not names:
            return None
        vocab = {
            name: K.VOCAB_PLACEHOLDER[kind]
            for name, kind in names.items()
            if K.VOCAB_PLACEHOLDER.get(kind)
        }
        return vocab or None
    except Exception:
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v1.1（卡 rHrY9D）：给内核的两张事实表
# ---------------------------------------------------------------------------

def _gate_ctx(conn, org_id: str, cfg) -> dict:
    """取值关的事实：库内对象名 / 扫描窗口 / 每日启用槽位。

    ⛔ 只读；⛔ 不含对象 id（内核不需要，少给一份就少一条泄漏面）。
    `periods` 用 `effective_slot_config`（`load_slot_config` 出来的同一份配置）
    推导，所以「模型说第 9 节」在只上 8 节的学校会被**当场挡掉**。
    """
    date_from, date_to = B3.resolve_window(cfg)
    slot_cfg = load_slot_config(conn, org_id)

    objects = {
        name: 1
        for (name,) in conn.execute(
            "SELECT name FROM objects WHERE org_id = ?", (org_id,)
        )
        if name and len(name) >= 2
    }
    periods: dict[str, list[int]] = {}
    day = date_from
    while day <= date_to:
        # 🔴 `enabled_slots()` 返回的 idx 已经是**1-based 的「第几节」**
        #    （`period_slots.idx` 从 1 开始，与 `slots.bit()` 同口径）。
        #    这里再 +1 会让「第 1 节」被判成非法 —— 直接导致合法规则被整条丢弃，
        #    且界面上只表现为「AI 好像没用上」，不报错。
        periods[day.isoformat()] = [idx for _dow, idx in slot_cfg.enabled_slots(day.isoweekday())]
        day += timedelta(days=1)
    return {
        "objects": objects,
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "periods": periods,
    }


def _slot_text(item: dict) -> str:
    """冲突项的节次人话：`周三第1节`（多节用顿号）。"""
    parts = []
    for r in item.get("readable") or []:
        try:
            dow = int(r.get("dow"))
            period = int(r.get("period"))
        except (TypeError, ValueError):
            continue
        label = _DOW_LABEL[dow - 1] if 1 <= dow <= 7 else f"周{dow}"
        parts.append(f"{label}第{period}节")
    return "、".join(parts)


def _object_schedule_ids(conn, org_id: str, name: str) -> set[str] | None:
    """对象名 → 它参与的日程 id 集合。名字不在库里 → None（判「查不到」）。

    🔴 为什么不能直接比 `item["object_id"]`：冲突项的 a/b 两侧给的是**代表对象**
    （`b3_kernels.ROLE_ORDER` 里教师优先），教师冲突时 object_name 会是「教师04」，
    班级名只在 title 里。按「参与者关系」反查才不依赖这个代表选谁。
    """
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM objects WHERE org_id = ? AND name = ?", (org_id, name)
        )
    ]
    if not ids:
        return None
    placeholders = ",".join("?" * len(ids))
    out = {
        r[0]
        for r in conn.execute(
            f"SELECT schedule_id FROM schedule_participants"
            f" WHERE object_id IN ({placeholders})",
            ids,
        )
    }
    out |= {
        r[0]
        for r in conn.execute(
            f"SELECT schedule_id FROM schedule_resources"
            f" WHERE resource_id IN ({placeholders})",
            ids,
        )
    }
    return out


def _conflict_facts(conn, org_id: str, cfg, slots: dict) -> dict | None:
    """`query=conflicts`：**复用 `/api/conflicts` 的同一份口径**（B3.conflict_payload）。

    ⛔ 不新造查询接口、不自己扫库：`count` / `raw_count` / 聚合规则全部同源，
    否则同一句话问出来的数会跟冲突列表页对不上（本项目最贵的老毛病）。
    """
    payload = B3.conflict_payload(conn, org_id, cfg)
    items = list(payload.get("items") or [])

    name = slots.get("object")
    day = slots.get("date")
    period = slots.get("period")

    involved: set[str] | None = None
    if name:
        involved = _object_schedule_ids(conn, org_id, name)
        if involved is None:
            return None  # 库里没这个对象 → 回退关键词，⛔ 不让模型编

    matched = []
    for it in items:
        if involved is not None:
            sides = {
                (it.get("a") or {}).get("schedule_id"),
                (it.get("b") or {}).get("schedule_id"),
            }
            if not (sides & involved):
                continue
        if day and it.get("date") != day:
            continue
        if period and not any(
            int(r.get("period") or 0) == int(period)
            for r in (it.get("readable") or [])
        ):
            continue
        matched.append(it)

    filtered = bool(name or day or period)
    if filtered and not matched:
        # 🔴 有筛选却查不到 → 返 None 让调用方回退关键词。
        #    ⛔ 不能由模型说「没有冲突」：它没查过库，那是编的。
        return None

    lines = []
    if not filtered:
        lines.append(
            f"本周共 {payload.get('count', 0)} 组冲突"
            f"（原始 {payload.get('raw_count', 0)} 处）。"
        )
    else:
        scope = []
        if name:
            scope.append(name)
        if day:
            scope.append(day)
        if period:
            scope.append(f"第{period}节")
        lines.append(f"{' '.join(scope)}：查到 {len(matched)} 组冲突。")
    for it in matched[:FACTS_MAX_ITEMS]:
        a = it.get("a") or {}
        b = it.get("b") or {}
        lines.append(
            f"· {it.get('date')} {_slot_text(it)}："
            f"{a.get('title') or '未命名'}（{a.get('object_name') or '—'}）"
            f" 撞上 {b.get('title') or '未命名'}（{b.get('object_name') or '—'}）"
        )
    if len(matched) > FACTS_MAX_ITEMS:
        lines.append(f"…另有 {len(matched) - FACTS_MAX_ITEMS} 组，见冲突列表。")
    return {
        "text": "\n".join(lines),
        "sources": [{"doc": "docs/07", "section": "§5"}],
    }


def _facts_fn(org_id: str, cfg):
    """构造 `facts(intent_id, slots)`。返回 None = 这条不执行查询（回退关键词）。

    🔴 **每次调用自己开关连接**：`chat()` 里那个连接在交给内核前就关了
    （内核是纯函数，不能持有句柄）。用闭包共享同一个连接的话，第一次查询会撞
    `Cannot operate on a closed database` —— 而它被 `except` 吞掉后只表现成
    「AI 好像没用上」，是典型的静默失效。
    """

    def facts(intent_id: str, slots: dict) -> dict | None:
        query = (slots or {}).get("query") or "howto"
        if query == "howto":
            for item in K.INTENTS:
                if item["id"] == intent_id:
                    return {
                        "text": item["answer"],
                        "sources": [dict(s) for s in item["sources"]],
                    }
            return None
        if query == "conflicts":
            conn = None
            try:
                conn = config.open_db()
                return _conflict_facts(conn, org_id, cfg, slots or {})
            finally:
                if conn is not None:
                    conn.close()
        # slots / schedules：第一版不执行（见文件头「已知边界」）
        return None

    return facts


@auth.require_auth
def chat(method, path, query, body, headers, context=None):
    """POST /api/chat。

    成功 200：{"answer","sources","mode","degraded","grounded","rule"}
    400：缺 question / question 非字符串 / 超长 / group 类型不对
    401：未登录（@require_auth，嵌套格式）
    403：group 越权（B3.resolve_authorized_org，扁平格式，与 B3/B4 同口径）

    🔴 注意「答不出来」不是错误码：手册里没有这条时返回 **200 + mode="template"**
    + 「暂时答不上来」。⛔ 不许用 4xx/5xx 表达「不知道」—— 前端会把非 200
    当成故障弹窗，而「手册没写」是正常业务态。
    """
    data = json_body(body)
    question = data.get("question")
    if not isinstance(question, str) or not question.strip():
        return _bad_request("缺少 question（用户原话）")
    if len(question) > MAX_QUESTION_CHARS:
        return _bad_request(f"question 过长（>{MAX_QUESTION_CHARS} 字），请精简后再问")

    group = data.get("group")
    if group is not None and (not isinstance(group, str) or not group.strip()):
        return _bad_request("group 必须是非空字符串（可省略）")

    vocab = _name_vocab(context["user_id"])

    # 🔴 v1.1：事实表要连库。库不可用 / 无组织 → facts=None，问答退回纯手册层
    #    （⛔ 不能因为查不到事实就把老师挡在门外）。
    #    唯一的例外是「显式传了 group 且越权」：403 必须原样抛，
    #    否则越权探测会被降级成 200 吞掉（与 B3/B4 同口径）。
    facts = None
    gate_ctx = None
    conn = None
    try:
        conn = config.open_db()
        try:
            _effective_group, org_id = B3.resolve_authorized_org(
                conn, context["user_id"], group
            )
        except ApiError:
            if group is not None:
                raise
            org_id = None  # 没传 group 且无默认组：不阻断手册问答
        if org_id:
            cfg = config.get_current_config()
            gate_ctx = _gate_ctx(conn, org_id, cfg)
            facts = _facts_fn(org_id, cfg)
    except ApiError:
        raise
    except Exception:  # noqa: BLE001 —— 取事实失败只降级，绝不 500
        facts = None
        gate_ctx = None
    finally:
        if conn is not None:
            conn.close()

    return K.answer(
        question, vocab=vocab, facts=facts, gate_ctx=gate_ctx
    )


ROUTES = {
    ("POST", "/api/chat"): chat,
}
