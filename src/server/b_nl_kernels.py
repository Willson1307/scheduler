"""v1.1（卡 rHrY9D）：入向翻译内核 —— 老师的话 → 结构化规则 IR。

用途与设计意图（答辩口径）：
  1. **AI 只做翻译，第一层翻译就是这里**：把一句口语（「高一(1)班周三第一节
     是不是撞了」）翻成 `{"intent_id","slots","reply"}` 这个**封闭结构的中间表示**，
     之后由后端按 `intent_id` / `query` 去执行**只读查询**，再把真实数字填进去。
     ⛔ 模型不参与计算、不产出可执行动作、不输出内部 ID。
  2. 🔴 **封闭集合就是 schema**：`intent_id` 只能取自现有意图表（29 条），
     `slots` 只能是 4 个键，`query` 只能是 4 个只读枚举。
     模型想「自由发挥」时，第一步就会被 schema 关挡掉 —— 这是比提示词更硬的约束。
  3. 🔴 **三关缺一不可**（任一不过 = 整条丢弃，调用方回退关键词匹配）：
       关 1 schema  ：白名单 / 键集合 / 枚举 / 类型
       关 2 取值    ：object 必须对上库内真实对象；date 落在扫描窗口内；
                      period 落在当日启用槽位内
       关 3 红线    ：不得出现写动作、不得出现 sch_/obj_ 之类的内部 ID
     ⛔ 丢弃比放行安全：放行一条编造的规则，等于让老师看到一条编造的答案。
  4. 🔴 **本模块不读库、不读文件、不发请求**：`complete` 与「库内事实」都由调用方
     注入（`gate_ctx`），所以它可以被单测完全离线覆盖，也不会和后端存储耦合。
     不 import `b_ai_kernels`（意图白名单由调用方传进来），避免循环 import。
  5. 不注册路由（文件名不带 `api_` 前缀，见 AGENTS §6 命名铁律）。只用标准库。

已知边界（如实，不夸大）：
  - **不支持模糊指代**：「我们班」「那个年级」「昨天那节」一律过不了取值关。
    第一版要求说全名（如「高一(1)班」）。这是刻意的 —— 猜指的代价是答错班。
  - 缓存键含**原话**而非仅 `(intent_id, slots)`：因为 `intent_id`/`slots` 要等
    模型返回后才知道，无法当前置键用；含原话才能真正省掉第二次模型调用。
  - 进程内缓存无 TTL，上限 64 条（FIFO 淘汰）。改 env、换库后需重启进程才清空。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date

# ---------------------------------------------------------------------------
# 常量：封闭集合
# ---------------------------------------------------------------------------

#: slots 允许的键（多一个键都算 schema 不过 —— 防止模型夹带 `action` 之类）。
SLOT_KEYS: tuple[str, ...] = ("object", "date", "period", "query")

#: query 允许的取值，**全部只读**。⛔ 不在这里的（write/apply/resolve/approve）
#: 一律按红线处理，回退关键词。
QUERY_ENUM: tuple[str, ...] = ("conflicts", "slots", "schedules", "howto")

#: reply 上限（字）。卡面写死 60 —— 它只是一句口语开场，正式内容由后端填。
MAX_REPLY_CHARS = 60

#: 入向超时（秒）。短 prompt（约 1.5k 字），线上实测 0.5s；3 秒足够，
#: 超了就退回关键词匹配 —— 宁可答得朴素，也不让老师干等。
IR_TIMEOUT_S = 3.0

#: 进程内缓存条数上限（演示时同一句会点很多次）。
_CACHE_MAX = 64
_CACHE: dict[str, dict] = {}

#: 内部 ID 形态（红线 3）：出现即丢弃，且**绝不**拿去调写接口。
_ID_RE = re.compile(r"\b(?:obj|grp|g|sch|usr|org|exc|dem|snap|cr)_[0-9a-zA-Z]{6,}\b")

#: 写动作词（红线 1）：出现在 reply 里说明模型想「指挥系统做事」。
_WRITE_WORDS = (
    "action", "op=", "write", "apply", "approve", "delete",
    "已改为", "已撤销", "已批准", "已删除",
)

#: 键名白名单（红线 1 的补充）：模型若自己加了这些键，说明它在拼一个动作。
_FORBIDDEN_KEYS = frozenset({"action", "op", "write", "move", "cancel", "approve", "cmd"})


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

_PROMPT_HEAD = (
    "你是排课系统的「意图识别器」。你的唯一工作是把老师的一句话翻成结构化规则。\n"
    "⛔ 你不做计算、不查数据、不改任何东西、不判断对错。\n"
    "只输出**一个 JSON 对象**，不要解释、不要 Markdown 代码块、不要多余文字。\n"
    "\n"
    "输出格式（三个键，一个都不能少）：\n"
    '{"intent_id":"...","slots":{},"reply":"..."}\n'
)

_PROMPT_TAIL = (
    "\n"
    "slots 只允许这 4 个键，没把握就整个留空 {}：\n"
    "- object：问题里提到的班级 / 教师 / 教室，**必须原样照抄原文里的写法**。\n"
    "  原文里出现「对象1」「对象2」这类令牌时，照抄令牌，不要猜它是什么。\n"
    "- date：形如 2026-09-09 的日期。\n"
    "- period：第几节，写整数。\n"
    "- query：只能是 conflicts（查冲突）/ slots（查空位）/ "
    "schedules（查课表）/ howto（问怎么操作）。\n"
    "\n"
    "reply：一句口语，不超过 60 字。\n"
    "  ⛔ 不许写任何数字（处数、节次、人数都不行）—— 正式数字由系统查完再填。\n"
    "  ⛔ 不许写任何班级名、教师名、教室名 —— 系统会自己补。\n"
    "  ⛔ 不知道就写空串，不要编。\n"
    "\n"
    "绝对禁止：\n"
    "1) 输出 sch_ / obj_ / usr_ / org_ 这类内部 ID；\n"
    "2) 输出「改成 / 挪到 / 取消 / 批准 / 已处理」这类执行指令；\n"
    "3) 编造权限规定、时间限制、人数、制度 —— 没依据就写空串。\n"
)

_SYSTEM_CACHE: dict[tuple[str, ...], str] = {}


def build_system_prompt(intent_ids) -> str:
    """把意图白名单拼进提示词（白名单是唯一事实来源 = `b_ai_kernels.INTENTS`）。

    缓存按 id 元组，进程内只拼一次；换意图表（测试）会自动重建。
    """
    key = tuple(intent_ids)
    cached = _SYSTEM_CACHE.get(key)
    if cached is not None:
        return cached
    listing = "、".join(key)
    prompt = (
        _PROMPT_HEAD
        + "\nintent_id 只能取下面这些之一（选不出就写 null）：\n"
        + listing
        + "\n"
        + _PROMPT_TAIL
    )
    _SYSTEM_CACHE[key] = prompt
    return prompt


def build_user_prompt(question: str) -> str:
    return "老师原话：" + question.strip() + "\n\n只输出那个 JSON 对象。"


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def cache_key(question: str, intent_ids) -> str:
    """缓存键 = 脱敏后原话 + 白名单指纹。

    🔴 卡面写的是 `hash(intent_id + slots)`，但这两个值要等模型返回后才知道，
    无法当前置键用 —— 那样缓存永远命中不了。改用原话做键，效果是
    「同一句问第二次不再调模型」，这才是演示时要的那 0.5 秒。
    """
    blob = question.strip() + "\x00" + "|".join(intent_ids)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def cache_get(key: str) -> dict | None:
    hit = _CACHE.get(key)
    return dict(hit) if hit is not None else None


def cache_put(key: str, rule: dict) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)), None)  # FIFO：淘汰最早那条
    _CACHE[key] = dict(rule)


def reset_cache() -> None:
    """清缓存（测试用：注入不同模型回答时不串味）。"""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# 关 1：schema
# ---------------------------------------------------------------------------

def check_schema(rule: dict, intent_ids) -> tuple[bool, str]:
    """判据：白名单 / 键集合 / 枚举 / 类型。任一不符 → 整条丢弃。"""
    if not isinstance(rule, dict):
        return False, "不是 JSON 对象"

    extra = set(rule) - {"intent_id", "slots", "reply"}
    if extra:
        return False, f"多余字段 {sorted(extra)}"

    intent_id = rule.get("intent_id")
    # None 是合法的「认不出」（模型允许弃权），但弃权等于没答案 → 也丢弃
    if intent_id is None:
        return False, "intent_id 为 null（模型弃权）"
    if not isinstance(intent_id, str) or intent_id not in intent_ids:
        return False, f"intent_id 不在白名单：{intent_id!r}"

    slots = rule.get("slots")
    if slots is None:
        slots = {}
    if not isinstance(slots, dict):
        return False, "slots 不是对象"
    if set(slots) - set(SLOT_KEYS):
        return False, f"slots 出现非法键 {sorted(set(slots) - set(SLOT_KEYS))}"

    query = slots.get("query")
    if query is not None and query not in QUERY_ENUM:
        return False, f"query 不是只读枚举：{query!r}"

    period = slots.get("period")
    if period is not None and (
        isinstance(period, bool) or not isinstance(period, int) or period < 1
    ):
        return False, f"period 必须是正整数：{period!r}"

    obj = slots.get("object")
    if obj is not None and not isinstance(obj, str):
        return False, "object 必须是字符串"
    d = slots.get("date")
    if d is not None and not isinstance(d, str):
        return False, "date 必须是字符串"

    reply = rule.get("reply", "")
    if reply is None:
        reply = ""
    if not isinstance(reply, str):
        return False, "reply 必须是字符串"

    return True, ""


# ---------------------------------------------------------------------------
# 关 3：红线
# ---------------------------------------------------------------------------

def check_redline(rule: dict) -> tuple[bool, str]:
    """判据：不出现内部 ID、不自带动作键、reply 里不出现执行动作词。

    🔴 动作词**只查 reply 与槽位取值**，不查整个 JSON —— `intent_id` 本身
    就在白名单里，而白名单里真有 `propose_approve` 这种含 approve 的 id。
    早先查整个 blob 的做法会把合法规则整条误杀，表现为「AI 好像没用上」，不报错。
    """
    slots = rule.get("slots") or {}
    if isinstance(slots, dict) and set(slots) & _FORBIDDEN_KEYS:
        return False, f"slots 夹带动作键 {sorted(set(slots) & _FORBIDDEN_KEYS)}"

    # 内部 ID：整条 JSON 都不许出现（含 intent_id / reply / 槽位）
    blob = json.dumps(rule, ensure_ascii=False)
    if _ID_RE.search(blob):
        return False, "输出里出现内部 ID（sch_/obj_ 等）"

    # 执行动作词：只看「模型自己写的那句话」和槽位取值
    parts = [str(rule.get("reply") or "")]
    if isinstance(slots, dict):
        parts.extend(str(v) for v in slots.values())
    text = "\n".join(parts).lower()
    for word in _WRITE_WORDS:
        if word in text:
            return False, f"输出里出现执行动作词：{word}"
    return True, ""


# ---------------------------------------------------------------------------
# 关 2：取值
# ---------------------------------------------------------------------------

def check_values(slots: dict, gate_ctx: dict | None) -> tuple[bool, str]:
    """判据：object 对上库内真实对象、date 在窗口内、period 在当日启用槽位内。

    `gate_ctx` 由调用方从库里取（本模块不读库）：
        {"objects": {名称: id}, "from": "YYYY-MM-DD", "to": "YYYY-MM-DD",
         "periods": {"YYYY-MM-DD": [1, 2, ...]}}
    `gate_ctx is None`（取不到库 / 无组织）→ **一律判不过**：
    宁可退回关键词，也不在拿不到事实的情况下放行一条未经核对的规则。
    """
    if gate_ctx is None:
        return False, "拿不到库内事实，无法核对取值"

    slots = slots or {}
    obj = slots.get("object")
    if obj:
        names = gate_ctx.get("objects") or {}
        tokens = gate_ctx.get("tokens") or {}
        # 入向对原话做了可逆令牌化（真名不出本机），模型看到的是「对象1」。
        # 所以取值关先把令牌还原成真名再核对 —— 否则一条合法规则会被令牌挡掉。
        real = tokens.get(obj, obj)
        if real not in names:
            return False, f"object 对不上库内任何对象：{obj!r}"

    raw_date = slots.get("date")
    d: date | None = None
    if raw_date:
        try:
            d = date.fromisoformat(raw_date)
        except (TypeError, ValueError):
            return False, f"date 不是合法日期：{raw_date!r}"
        lo = gate_ctx.get("from")
        hi = gate_ctx.get("to")
        if lo and d < date.fromisoformat(lo):
            return False, f"date 早于扫描窗口：{raw_date}"
        if hi and d > date.fromisoformat(hi):
            return False, f"date 晚于扫描窗口：{raw_date}"

    period = slots.get("period")
    if period is not None:
        periods = gate_ctx.get("periods") or {}
        if d is not None:
            enabled = periods.get(d.isoformat())
            if enabled is None:
                return False, f"当天没有启用槽位：{d.isoformat()}"
        else:
            # 没给日期：只要在本周任意一天启用过就算合法（口径见 docstring）
            enabled = sorted({p for ps in periods.values() for p in ps})
        if period not in enabled:
            return False, f"period {period} 不在启用槽位内：{enabled}"

    return True, ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _loads(raw: str) -> dict | None:
    """解析模型输出：容错去掉代码块围栏与首尾空白，非法就 None（不抛）。"""
    text = _FENCE_RE.sub("", (raw or "").strip())
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _trim_reply(text: str) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS] + "…"
    return text


def parse_rule(
    question: str,
    intent_ids,
    complete=None,
    timeout_s: float = IR_TIMEOUT_S,
    gate_ctx: dict | None = None,
) -> dict | None:
    """一句话 → 校验通过的规则。**任何环节不过都返 None**（调用方回退关键词）。

    参数：
      complete —— 注入的「纯底层通道」`complete(system, user, timeout_s)`；
                  None 时用 `b_llm_client.complete`（跨人契约，失败一律 None）。
      gate_ctx —— 取值关需要的事实（见 `check_values`）。**必传**才可能通过。

    返回：`{"intent_id","slots","reply"}`；`reply` 已硬截断到 60 字。
    """
    text = (question or "").strip()
    if not text:
        return None

    ids = tuple(intent_ids)
    key = cache_key(text, ids)
    cached = cache_get(key)
    if cached is not None:
        return cached

    if complete is None:
        try:
            from . import b_llm_client as llm
        except ImportError:
            return None
        complete = getattr(llm, "complete", None)
        if complete is None:
            return None

    try:
        raw = complete(build_system_prompt(ids), build_user_prompt(text), timeout_s)
    except Exception:  # noqa: BLE001 —— 注入实现抛异常也必须降级，不能 500
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None

    rule = _loads(raw)
    if rule is None:
        return None

    ok, _why = check_schema(rule, ids)
    if not ok:
        return None
    ok, _why = check_values(rule.get("slots") or {}, gate_ctx)
    if not ok:
        return None
    ok, _why = check_redline(rule)
    if not ok:
        return None

    slots = {k: v for k, v in (rule.get("slots") or {}).items() if v is not None}
    out = {
        "intent_id": rule["intent_id"],
        "slots": slots,
        "reply": _trim_reply(rule.get("reply") or ""),
    }
    cache_put(key, out)
    return out


def reset_system_cache() -> None:
    """清提示词缓存（测试换意图表时用）。"""
    _SYSTEM_CACHE.clear()
