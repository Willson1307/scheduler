"""B-6（rn6JxF）：POST /api/chat —— AI 问答层接口（三层降级，不接 AI 也能答）。

契约（卡面 rn6JxF 写死）：
    POST /api/chat
    请求：{"question":"我想把周三下午的课调开，怎么弄？","group":"g_x"}
    返回 200：{"answer":"...","sources":[{"doc":"docs/30","section":"§3"}],
               "mode":"llm"|"retrieval"|"template","degraded":false}

用途与设计意图（答辩口径）：
  1. **本文件只做接口层**：参数校验 / 鉴权 / 组织隔离，然后把问题交给
     `b_ai_kernels.answer()`（三层降级内核）。业务逻辑一行不写在这里 ——
     内核是纯函数，可单测、可离线跑，接口层只负责「谁能问、问题长什么样」。
  2. 🔴 **`mode` 必填且必须如实**：前端要能显示「本次回答来自手册原文（未启用 AI）」。
     ⛔ 不能让老师以为所有回答都是 AI 生成的 —— 这是诚信要求，也是答辩要点
     （本项目 AI 只用于自然语言理解与结果解释，冲突检测与消解由自建求解器完成）。
  3. 🔴 **任何情况不返 500**：手册缺失、LLM 超时/挂掉/未配置，一律降级并在
     `mode` / `degraded` 里如实写。问答挂了不影响排课主链路（docs/20 冗余与降级）。
  4. 🔴 **AI 只能回答/解释，不能产出「改哪条日程」**：本接口不写任何表、
     不返回任何变更指令；要改课走 `/api/exceptions`（临时）或审批流（永久）。
  5. 鉴权照 B3/B4/B5 范式：`@require_auth` 拦 401；`group` 传了就校验
     `B3.resolve_authorized_org`（越权 → 403），与其它业务接口同口径 ——
     防止拿 `group` 探测别家组织是否存在。`group` 缺省不强制（本层不读组织数据，
     不传也能答手册问题；⛔ 不会因「没有授权组」把只想问手册的老师挡在门外）。

已知边界（如实）：
  - 无独立限流：本接口挂在登录之后，`/api/resolve` 那样的次数限制这里没做。
    开 LLM 时一次回答最多占用一个线程 8 秒（`b_llm_client` 的 timeout）。

签名约定（写死，不得改）：
  handle(method, path, query, body, headers, context=None)

只用 Python 标准库。
"""

from __future__ import annotations

from . import auth, config
from . import b3_kernels as B3
from . import b_ai_kernels as K
from .http_api import json_body

#: 问题长度上限（字符）。防超长输入拖慢检索 / 撑爆送模型的请求体；
#: 500 字已远超「老师问一句话」的实际场景。
MAX_QUESTION_CHARS = 500


def _bad_request(message: str) -> tuple[int, dict]:
    """400 用契约嵌套格式（与 api_demands 一致，docs/07 通用约定）。"""
    return 400, {"error": {"code": "bad_request", "message": message}}


@auth.require_auth
def chat(method, path, query, body, headers, context=None):
    """POST /api/chat。

    成功 200：{"answer","sources","mode","degraded"}
    400：缺 question / question 非字符串 / 超长 / group 类型不对
    401：未登录（@require_auth，嵌套格式）
    403：group 越权（B3.resolve_authorized_org，扁平格式，与 B3/B4 同口径）

    🔴 注意「答不出来」不是错误码：手册里没有这条时返回 **200 + mode="template"**
    + 「手册里没有这条」。⛔ 不许用 4xx/5xx 表达「不知道」—— 前端会把非 200
    当成故障弹窗，而「手册没写」是正常业务态。
    """
    data = json_body(body)
    question = data.get("question")
    if not isinstance(question, str) or not question.strip():
        return _bad_request("缺少 question（用户原话）")
    if len(question) > MAX_QUESTION_CHARS:
        return _bad_request(f"question 过长（>{MAX_QUESTION_CHARS} 字），请精简后再问")

    group = data.get("group")
    if group is not None:
        if not isinstance(group, str) or not group.strip():
            return _bad_request("group 必须是非空字符串（可省略）")
        conn = config.open_db()
        try:
            # 只要校验「该组在授权范围内」；返回的 org_id 本层不用 ——
            # 问答层不读任何组织数据（读了就不是「只解释手册」了）。
            B3.resolve_authorized_org(conn, context["user_id"], group)
        finally:
            conn.close()

    return K.answer(question)


ROUTES = {
    ("POST", "/api/chat"): chat,
}
