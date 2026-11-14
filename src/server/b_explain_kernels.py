"""v1.1（卡 rHrY9D）：出向翻译内核 —— 结构化事实 → 人话。

用途与设计意图（答辩口径）：
  1. **AI 只做翻译，第二层翻译就是这里**：把求解器 / 审批产出的**结构化事实**
     （改了几条、从哪节挪到哪节、被哪条硬约束挡住）翻成老师能读的一句话。
     ⛔ 事实由后端算，模型只负责措辞 —— 它**不参与计算、不产出数字**。
  2. 🔴 **数字一致性是硬闸**（`grounded`）：模型输出里的每一个阿拉伯数字，
     都必须出现在输入事实里，否则整条丢弃、回退模板句。
     防的就是那类最贵的错：「23 处」被写成「12 处」—— 看着像人话，数字是编的。
     另加一条反规避：中文数字 + 量词（「两节」「三处」）无法核验，同样判不接地。
  3. 🔴 **脱敏用可逆令牌，不用占位符**：出向要「说清是谁」，所以真名不能像入向
     那样打成 `[班级]`（打完就再也还原不回来）。做法是把库内真名换成
     `对象1 / 对象2`，模型只看到令牌，回来再逐个还原成真名 ——
     外部模型全程看不到真实班级 / 姓名，老师仍然看得懂。
  4. 🔴 **任何失败都回模板句**：`complete` 未配置 / 超时 / 返 None / 数字不接地 /
     字数超限 → 用本地拼的模板句（本身就是中文且信息完整），**不抛、不 500**。
     降级不是失败，假装成功才是。
  5. 不注册路由（文件名不带 `api_` 前缀，见 AGENTS §6）。只用标准库。

已知边界（如实）：
  - 「接地」只校验**数字与已知名字**，不校验措辞是否忠实（那要靠人工审查）。
    所以提示词把话说死：只能重述事实，不许补充方案、不许承诺操作步骤。
  - 令牌还原是**字符串替换**：若模型把「对象1」拆成「对象 1」就还原不回去，
    此时该名字在输出里会缺位 —— 宁可少一句，也不会张冠李戴。
"""

from __future__ import annotations

import re
import sqlite3

#: 字数上限（卡面写死）：D1 / D3 都是一句话，D2 是检索层答案。
MAX_D1_CHARS = 120
MAX_D3_CHARS = 120
MAX_D2_CHARS = 300

#: 出向超时：短事实 + 短回答，3 秒足够；回不来就用模板句，绝不拖住界面。
EXPLAIN_TIMEOUT_S = 3.0

#: 全角数字（０-９）→ 半角。用码点算，**不写字面量**：
#: 一串连续数字会被 `tests/test_no_hardcoded_passwords.py` 当成弱口令报警。
_FULLWIDTH = {0xFF10 + i: 0x30 + i for i in range(10)}

_DIGITS_RE = re.compile(r"\d+")

#: 中文数字 + 量词（「两节」「三处」「五个班」）—— 无法用数字集合核验，判不接地。
#: 🔴 必须排除「这一节 / 该节 / 同一节 / 第几节」这类**非数量**用法，
#:    否则模型正常说「这一节撞了」也会被当成编数字（实测踩过，导致整条被误杀）。
#:    做法：前面是 第|这|该|同|此|每|哪|另 时不匹配；「一」只在后接量词且不在
#:    上述前缀时才算数量（「一节」= 1 节，确实是个数量）。
_CN_NUM_QTY_RE = re.compile(
    r"(?<![第这该同此每哪另])[二三四五六七八九十两]\s*(?:处|节|组|条|人|间|次|位|个班)"
    r"|(?<![第这该同此每哪另])一\s*(?:处|组|条|人|间|次|位|个班)"
)

#: 对象占位令牌前缀（对外只出现这个）。
TOKEN_PREFIX = "对象"


# ---------------------------------------------------------------------------
# 库内真名
# ---------------------------------------------------------------------------

def object_names(conn: sqlite3.Connection, user_id: str) -> dict[str, str] | None:
    """当前用户所在组织的 `名称 → kind`（只取名字，不取课表、不取名单明细）。

    🔴 只用于本机替换：既不落日志、也不缓存，用完即丢。
    库不可用 / 用户无组织 → None（调用方退回正则兜底，**不阻断**问答）。
    """
    try:
        row = conn.execute(
            "SELECT o.org_id FROM objects o JOIN users u ON u.object_id = o.id"
            " WHERE u.id = ?",
            (user_id,),
        ).fetchone()
        if row is None or not row[0]:
            return None
        out: dict[str, str] = {}
        for name, kind in conn.execute(
            "SELECT name, kind FROM objects WHERE org_id = ?", (row[0],)
        ):
            if name and len(name) >= 2:
                out[name] = kind
        return out or None
    except Exception:  # noqa: BLE001 —— 库异常不得阻断问答
        return None


# ---------------------------------------------------------------------------
# 可逆令牌
# ---------------------------------------------------------------------------

def tokenize(text: str, names) -> tuple[str, dict[str, str]]:
    """真名 → 「对象N」，返回 (替换后文本, {令牌: 真名})。

    最长优先，避免「高一(1)班」被「一班」先切一刀。
    """
    mapping: dict[str, str] = {}
    if not text:
        return "", mapping
    uniq = sorted({n for n in (names or ()) if n and len(n) >= 2}, key=len, reverse=True)
    out = text
    for i, name in enumerate(uniq, 1):
        if name not in out:
            continue
        token = f"{TOKEN_PREFIX}{i}"
        out = out.replace(name, token)
        mapping[token] = name
    return out, mapping


def untokenize(text: str, mapping: dict[str, str] | None) -> str:
    """「对象N」→ 真名。模型没原样保留令牌时，该名字就缺位（不猜）。"""
    if not text or not mapping:
        return text or ""
    out = text
    for token, name in mapping.items():
        out = out.replace(token, name)
    return out


# ---------------------------------------------------------------------------
# 接地校验
# ---------------------------------------------------------------------------

def digits(text: str) -> set[str]:
    """抽出全部阿拉伯数字（全角先归一），用于「输出 ⊆ 事实」比对。"""
    if not text:
        return set()
    return set(_DIGITS_RE.findall(text.translate(_FULLWIDTH)))


def grounded(model_text: str, facts_text: str) -> bool:
    """模型输出是否「接地」：数字全在事实里、且没用中文数字绕过核验。"""
    if not model_text:
        return False
    if not digits(model_text) <= digits(facts_text):
        return False
    if _CN_NUM_QTY_RE.search(model_text.translate(_FULLWIDTH)):
        return False
    return True


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

_SYSTEM_COMMON = (
    "你是排课系统的「解释器」。把系统已经做完 / 已经判定的事实，翻成老师能看懂的一句中文。\n"
    "⛔ 你不做判断、不给方案、不承诺操作、不改任何东西。\n"
    "⛔ 不许新增事实里没有的数字、班级名、教师名、课程名、时间。\n"
    "⛔ 不许说「点这里」「点应用」这类按钮名，除非事实里原样出现过。\n"
    "数字一律用阿拉伯数字，不要用中文数字（不要写「两节」，写「2 节」）。\n"
    "只输出这一句话本身，不要加引号、不要解释。\n"
)

_D1_RULES = (
    "\n以下是这次「一键消解」的事实。用不超过 120 字说明：改了什么、解决了什么、"
    "代价是什么。只重述事实，不要建议下一步。\n"
)

_D3_RULES = (
    "\n以下是系统拒绝这次改课的原因。用不超过 120 字说明为什么不能批，"
    "让老师听懂是哪节课 / 哪个班 / 哪位老师撞上了。只重述事实，不要给替代方案。\n"
)


def _call(complete, system: str, user: str, timeout_s: float) -> str | None:
    """统一调用：注入优先，否则走 `b_llm_client.complete`。**永不抛**。"""
    if complete is None:
        try:
            from . import b_llm_client as llm
        except ImportError:
            return None
        complete = getattr(llm, "complete", None)
        if complete is None:
            return None
    try:
        out = complete(system, user, timeout_s)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(out, str) or not out.strip():
        return None
    return out.strip()


# ---------------------------------------------------------------------------
# D1：消解结果解释
# ---------------------------------------------------------------------------

def template_resolve(facts: dict) -> str:
    """本地模板句（模型不可用 / 不接地时的兜底）。信息完整、不含推测。"""
    applied = facts.get("applied", 0)
    before = facts.get("before", 0)
    after = facts.get("after", 0)
    head = f"这次自动调整应用了 {applied} 个方案，冲突从 {before} 处降到 {after} 处。"
    parts = [head]
    changes = facts.get("changes") or []
    if changes:
        first = changes[0]
        seg = f"例如 {first['object']}《{first['title']}》从{first['from']}挪到{first['to']}"
        if len(changes) > 1:
            seg += f"，共调整 {len(changes)} 条日程"
        parts.append(seg + "。")
    for extra in (facts.get("extra") or [])[:1]:
        parts.append(extra + "。")
    return _truncate("".join(parts), MAX_D1_CHARS)


def facts_text(facts: dict) -> str:
    """把事实拼成一段「唯一可信文本」：模型输出与它比对，模型提示词也只用它。"""
    lines = [
        f"应用方案数：{facts.get('applied', 0)}",
        f"消解前冲突：{facts.get('before', 0)} 处",
        f"消解后冲突：{facts.get('after', 0)} 处",
    ]
    for i, c in enumerate(facts.get("changes") or [], 1):
        lines.append(
            f"改动{i}：{c['object']}《{c['title']}》由{c['from']}改到{c['to']}"
        )
    for extra in facts.get("extra") or []:
        lines.append(f"说明：{extra}")
    return "\n".join(lines)


def explain_resolve(facts: dict, names=None, complete=None,
                    timeout_s: float = EXPLAIN_TIMEOUT_S) -> dict:
    """出向 D1：消解结果 → 一句人话。返回 `{explanation, source, degraded}`。

    `names` 是库内真名列表（用于令牌化）；不给就按事实里的名字自动取。
    """
    base = template_resolve(facts)
    try:
        raw_facts = facts_text(facts)
        if names is None:
            names = [c["object"] for c in (facts.get("changes") or []) if c.get("object")]
        masked_facts, mapping = tokenize(raw_facts, names)
        out = _call(complete, _SYSTEM_COMMON + _D1_RULES, masked_facts, timeout_s)
    except Exception:  # noqa: BLE001
        out = None

    if not out:
        return {"explanation": base, "source": "template", "degraded": True}
    # 接地校验用「未令牌化的事实」比对数字，再还原真名
    if not grounded(out, raw_facts):
        return {"explanation": base, "source": "template", "degraded": True}
    text = _truncate(untokenize(out, mapping), MAX_D1_CHARS)
    if not text:
        return {"explanation": base, "source": "template", "degraded": True}
    return {"explanation": text, "source": "llm", "degraded": False}


# ---------------------------------------------------------------------------
# D3：审批被拒的硬约束原因
# ---------------------------------------------------------------------------

def template_violations(problems: list[str]) -> str:
    """本地模板句：原样重述硬约束（`hard_violations()` 已是中文且含对象名）。"""
    parts = [p.strip("。 ") for p in (problems or []) if p and p.strip()]
    if not parts:
        return "这次调整会与现有课表撞车，所以不能批准。"
    return _truncate("这次调整不能批准：" + "；".join(parts) + "。", MAX_D3_CHARS)


def explain_violations(problems: list[str], names=None, complete=None,
                       timeout_s: float = EXPLAIN_TIMEOUT_S) -> dict:
    """出向 D3：硬约束列表 → 一句人话。返回 `{explanation, source, degraded}`。"""
    base = template_violations(problems)
    try:
        raw = "；".join(p.strip("。 ") for p in (problems or []) if p and p.strip())
        if not raw:
            return {"explanation": base, "source": "template", "degraded": True}
        masked, mapping = tokenize(raw, names or _names_in(raw, problems))
        out = _call(complete, _SYSTEM_COMMON + _D3_RULES, masked, timeout_s)
    except Exception:  # noqa: BLE001
        out = None

    if not out:
        return {"explanation": base, "source": "template", "degraded": True}
    if not grounded(out, raw):
        return {"explanation": base, "source": "template", "degraded": True}
    text = _truncate(untokenize(out, mapping), MAX_D3_CHARS)
    if not text:
        return {"explanation": base, "source": "template", "degraded": True}
    return {"explanation": text, "source": "llm", "degraded": False}


def _names_in(raw: str, problems) -> list[str]:
    """兜底：调用方没给名单时，从 problems 里挑出「后接空格/该时段」的对象名。

    `hard_violations()` 的文案形状固定（`<名字> 在该时段已被占用`、
    `<名字> 容量 N，容纳不下 M 人`），所以能稳定切出名字。
    """
    names: list[str] = []
    for p in problems or ():
        head = str(p).split(" 在该时段")[0].split(" 容量")[0].strip()
        if head and head != str(p).strip():
            names.append(head)
    return names
