"""L-17（rtFhoL）：问答层第 2 层 —— LLM 润色客户端（零路由、零落盘、零三方依赖）。

用途与设计意图（答辩口径）：
  1. **本模块是「问一下」问答链路里唯一对外发请求的一层**，被 api_chat /
     b_ai_kernels 调用（契约签名冻结，双方已按此写，不得改）：
         polish(question, passages, timeout_s=8.0) -> str | None
     语义：拿手册段落把用户问题润色成一条模型回答。**调用方负责**挑选与
     脱敏 passages；本模块不做也不假装做脱敏（卡面红线）。
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

# --- 系统提示（卡面原文，一个字都不能少） -------------------------------------
_SYSTEM_PROMPT = (
    "只根据下面提供的资料回答；资料里没有的，直接说『手册里没有这条，"
    "请找开发者确认』，不要推测、不要编步骤。"
)


def polish(question: str, passages: list[str], timeout_s: float = 8.0) -> str | None:
    """把问题 + 手册段落发给 LLM，拿回一条润色回答。

    返回：
      None  —— 未配置 env / 超时 / 非 200 / 响应体非 JSON / 结构里取不出
               content / content 是空串。任何失败都不抛、不 retry。
      str   —— 模型回答（去首尾空白）。

    🔴 timeout_s 真传给 urllib 的 timeout=（不是文档里写着 8 秒实际不限时）。
    """
    base = os.environ.get(_ENV_BASE, "")
    key = os.environ.get(_ENV_KEY, "")
    if not base or not key:
        return None  # 未配置 = 不启用：不开 socket，直接短路

    model = os.environ.get(_ENV_MODEL, "glm-4-flash")
    content = question + "\n\n【资料】\n" + "\n".join(passages)
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
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
