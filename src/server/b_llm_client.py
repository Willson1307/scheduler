"""L-17（rtFhoL）：问答层第 2 层 —— LLM 润色客户端（零路由、零落盘、零三方依赖）。

用途与设计意图（答辩口径）：
  0. 🔴 2026-11-15（卡 rHrY9D，v1.1）：本模块现在提供**两个**入口。

     ```python
     complete(system, user, timeout_s=8.0) -> str | None   # 纯底层，无业务
     polish(question, passages, timeout_s=8.0) -> str | None  # complete 的薄封装
     ```

     `complete` 是「AI 只做翻译」的**唯一出网通道**：入向把老师的话翻成结构化
     规则（intent_id + slots）、出向把求解器/审批的结构化事实翻成人话，都走它。
     它**不含任何业务**——不知道什么是意图、不知道什么是冲突，只负责
     「给 system + user，拿回一段文本」。这样两条翻译方向共用同一套失败语义、
     同一份脱敏前置约定，不会各自漂移。
     `polish` 的签名与行为**一字未变**（跨人契约，`tests/test_llm_client.py` 锁死）。
  1. **本模块是问答链路里唯一对外发请求的一层**，被 api_chat /
     b_ai_kernels 调用。**调用方负责**挑选与脱敏内容；本模块不做也不假装做脱敏
     （卡面红线）。
  2. 🔴 **失败一律返 None、不抛、不 retry**。上层（api_chat 三层降级）拿
     None 就降级到下一层——这正是「不接 AI 也能答」的前提：云模型挂了、
     超时了、配错 URL 了，问答功能整体仍然可用，只是少了 AI 润色那一层。
  3. 🔴 **未配置 = 不启用**。SCHEDULER_LLM_BASE / SCHEDULER_LLM_KEY 任一
     缺失（或空串）→ 立即返 None，**一次 socket 都不开**。这保证
     `smoke.py --offline` 外联为 0、断网部署不受影响：key/url 只由队长在
     服务器 `/home/ubuntu/scheduler/.env.local`（chmod 600）配置。
  4. 🔴 **安全红线**：key 只从 os.environ 读，本文件不出现任何 key 字面量
     （示例一律 sk-xxxx）；异常消息不外泄（urllib 的异常文本可能带 URL /
     响应头信息），因此**只捕获、只静默返 None**，连异常类型名都不往外带
     ——本模块不写日志、不落盘、不进埋点、不缓存回答（隐私口径 docs/08）。
  5. 不注册路由（文件名不带 api_ 前缀，routes.py 的 glob 扫不到，见
     AGENTS §3.1 文件命名铁律）。只用 Python 标准库。

已知边界（如实，不夸大）：
  - 代理行为跟随标准 urllib（读环境变量/注册表）。生产服务器无代理配置，
    直连出网；本机开发若被系统代理接管，连接失败同样按「失败返 None」
    处理，不会误判成「模型挂了」以外的任何东西。
"""

from __future__ import annotations

import json
import os
import urllib.request

# --- 环境变量名（唯一配置入口，与本文件 docstring 口径一致） -----------------
_ENV_BASE = "SCHEDULER_LLM_BASE"     # 例：https://open.bigmodel.cn/api/paas/v4
_ENV_KEY = "SCHEDULER_LLM_KEY"       # 例：sk-xxxx（真实 key 只存在于服务器 .env.local）
_ENV_MODEL = "SCHEDULER_LLM_MODEL"   # 缺省 glm-4-flash（免费层主通道，docs/23）
#: 🔴 2026-11-15（卡 rHrY9D）：总开关。`off/0/false/no` 任一（忽略大小写、
#: 前后空白）→ 两个方向都直接返回 None，与「未配 env」完全等价。
#: 现场只用一个变量就能把 AI 整个摘掉，不必去动 key/url（那会丢失配置）。
_ENV_SWITCH = "SCHEDULER_LLM"

_OFF_WORDS = frozenset({"off", "0", "false", "no", "disabled", "disable"})

# --- 系统提示 -----------------------------------------------------------------
# 🔴 2026-11-14 改写（原为卡面原文「资料里没有的，直接说『手册里没有这条，请找开发者确认』」）。
#    实测该提示词让模型在**资料其实有答案**时也拒答：老师点「调课」「永久改课」胶囊，
#    资料（docs/07 §11、docs/30 §5.5/§5.6）里明明写着步骤，模型却照抄那句兜底话。
#    根因：「没有」的判据被写成了「措辞不完全一致」。
#    现在明确：措辞不一致也算有 → 据资料作答；确实没有才说不知道。
#    ⛔ 仍然禁止推测 / 编步骤 / 编数字（红线未动）。
_SYSTEM_PROMPT = (
    "只根据下面提供的资料回答，不要推测、不要编造步骤或数字。"
    "资料里有相关内容就据此作答——问法措辞和资料不完全一致也算有，"
    "把资料里的步骤整理成清楚的中文，保留原文里的按钮名、数字与限制。"
    "确实找不到相关内容时，才说『这个问题手册里没有写明』，"
    "并建议换个说法再问；不要自己编步骤。"
    # 🔴 2026-11-14：资料里混着 API 契约（docs/07），模型会把接口概念拟人化成界面按钮
    #    （实测把 plan 说成「预览」、把 apply 说成「点『应用』」，而界面上没有这两步）。
    "只用界面上能看到的名字回答，不要提接口名、字段名、契约编号，"
    "也不要说「预览 / 应用」这类开发概念。"
)


def llm_off() -> bool:
    """总开关是否为「关」。🔴 只读 env，不发请求、不打日志。

    与「没配 key/url」等价地短路：现场执行 `SCHEDULER_LLM=off` 一处即可把
    入向解析与出向解释同时摘掉，问答退回纯手册检索（不报错）。
    """
    raw = os.environ.get(_ENV_SWITCH, "")
    return raw.strip().lower() in _OFF_WORDS


def configured() -> bool:
    """是否「该出网」：开关没关 + BASE 与 KEY 都在。⛔ 不读取其值、不拼 URL。"""
    if llm_off():
        return False
    return bool(os.environ.get(_ENV_BASE, "")) and bool(os.environ.get(_ENV_KEY, ""))


def complete(system: str, user: str, timeout_s: float = 8.0) -> str | None:
    """纯底层通道：给 system + user，拿回模型的一段文本。**无任何业务语义**。

    返回：
      None  —— 开关关闭 / 未配置 env / 超时 / 非 200 / 响应体非 JSON / 结构里
               取不出 content / content 是空串。任何失败都不抛、不 retry、不打日志。
      str   —— 模型回答（去首尾空白）。

    🔴 未配置 = 不启用：一个 socket 都不开（smoke --offline 外联为 0 的前提）。
    🔴 `timeout_s` 真传给 urllib 的 `timeout=`（不是文档里写着却实际不限时）。
    """
    base = os.environ.get(_ENV_BASE, "")
    key = os.environ.get(_ENV_KEY, "")
    if not base or not key or llm_off():
        return None  # 未配置 / 已关闭：不开 socket，直接短路

    model = os.environ.get(_ENV_MODEL, "glm-4-flash")
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )

    # 捕获必须用裸 Exception：socket.timeout / URLError / HTTPError(非200) /
    # 连接被拒 / DNS 失败全都要静默降级。刻意不读异常文本（可能含 URL/头），
    # 也不打日志——失败语义只有一种：返 None，让上层降级。
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if getattr(resp, "status", None) != 200:
                return None
            raw = resp.read()
    except Exception:
        return None

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        answer = data["choices"][0]["message"]["content"]
    except Exception:
        return None  # 响应体非 JSON / 结构缺字段 / 下标越界

    if not isinstance(answer, str):
        return None
    answer = answer.strip()
    return answer if answer else None  # 空串视为失败


def polish(question: str, passages: list[str], timeout_s: float = 8.0) -> str | None:
    """把问题 + 手册段落发给 LLM，拿回一条润色回答。

    🔴 2026-11-15 起本函数是 `complete()` 的**薄封装**，签名、请求体、失败语义
    与改前逐字节一致（`tests/test_llm_client.py` 锁死了 URL / body / temperature /
    系统提示片段），因此它只是「把 system 固定成手册口径的 complete」。
    既有调用方（b_ai_kernels 的两条路径）一行都不用改。
    """
    content = question + "\n\n【资料】\n" + "\n".join(passages)
    return complete(_SYSTEM_PROMPT, content, timeout_s=timeout_s)

