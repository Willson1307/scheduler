"""B5（S17）：POST /api/demands 自然语言需求登记接口。

用途与设计意图（答辩口径）：
  1. 前端只传用户原话 raw_text，不做任何预处理/结构化（docs/07 §7.1）——
     解析规则全部在后端（b5_kernels），改规则不用动前端。
  2. 原话原样存储 + 响应体回显 raw_text（docs/07 §7.3）：
     落库保留原文，解析错了可追溯、可人工纠正。
  3. 🔴 绝不静默失败（docs/07 §7）：与现有日程冲突时，conflicts + message
     必须明确反馈（由 b5_kernels.detect_demand_conflicts 负责四种情形）。
  4. 鉴权（照 B3/B4 范式）：@require_auth 拦 401；group 走
     K.resolve_authorized_org 拦跨组织 403（group 从请求体取，缺省=所在最高组）。
  5. 数据合规（AGENTS §7）：本接口纯本地解析，不外发任何内容；将来若要接
     外部大模型，只允许发 raw_text 本身。
  6. S18 调整幅度档位（docs/19 §224）：请求可带 level（minimal/balanced/optimal，
     与 /api/resolve 的 mode 同源），缺省 minimal、行为与改动前完全一致；
     非法值 400，不静默兜底。词表见 b5_kernels.DEMAND_LEVELS；存储走 1:1
     旁表 demand_levels（schema.sql 末尾，读法见 b5_kernels.read_level）。

签名约定（写死，不得改）：
  handle(method, path, query, body, headers, context=None)

只用 Python 标准库。
"""

from __future__ import annotations

import json

from . import auth, config
from . import b3_kernels as B3
from . import b5_kernels as B5
from .http_api import json_body
from scheduler.db import new_id


@auth.require_auth
def demand(method, path, query, body, headers, context=None):
    """POST /api/demands。

    请求：{"raw_text":"下周三下午实验室A我要用，必须安排两节课","group":"...","hard":true,
           "level":"minimal"}
    成功 200：{"ok":true,"demand_id":"dem_x","level":"minimal","parsed":{...},"raw_text":"原话",
               "conflicts":[...],"message":"..."}
    未登录 401（require_auth）；跨组织 403（resolve_authorized_org）；
    缺 raw_text 400；level 非法 400。
    """
    data = json_body(body)
    raw_text = data.get("raw_text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        return 400, {"error": {"code": "bad_request", "message": "缺少 raw_text（用户原话）"}}
    hard = bool(data.get("hard", False))
    group = data.get("group")  # None 或缺省 → 所在最高组（docs/07 §1）

    # S18 调整幅度档位：缺省 minimal（docs/19 §224）；非法值显式 400，不静默兜底。
    # 与 api_resolve._parse_mode 同一风格（docs/07 §6 词汇）。
    level = data.get("level") or B5.DEFAULT_DEMAND_LEVEL
    if level not in B5.DEMAND_LEVELS:
        return 400, {"error": {
            "code": "bad_request",
            "message": f"level 必须是 {'/'.join(B5.DEMAND_LEVELS)}，收到 {level!r}",
        }}

    cfg = config.get_current_config()
    conn = config.open_db()
    try:
        # 🔴 旧库兜底：已存在的 data/*.db 不会自动补 demands 表
        # （ensure_database 只在库文件不存在时 seed）→ 缺表时本接口 500。
        # 与 telemetry.ensure_event_log 同一范式，见 b5_kernels.ensure_demands。
        B5.ensure_demands(conn)

        # 🔴 跨组织越权出口：group 不在 authorized_groups → 403（B3/B4 同口径）
        effective_group, org_id = B3.resolve_authorized_org(conn, context["user_id"], group)

        parsed = B5.parse_demand(raw_text, hard, cfg.today)
        conflicts, message = B5.detect_demand_conflicts(conn, org_id, parsed)

        # 🔴 原话原样落库（docs/07 §7.3）；parsed 仅作追溯，不替代 raw_text
        demand_id = new_id("dem")
        conn.execute(
            "INSERT INTO demands (id, org_id, group_id, user_id, raw_text, hard, parsed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                demand_id,
                org_id,
                effective_group,
                context["user_id"],
                raw_text,
                1 if hard else 0,
                json.dumps(parsed, ensure_ascii=False),
            ),
        )
        # S18 档位落 1:1 旁表 demand_levels（schema.sql 末尾；为什么不是
        # ALTER demands ADD COLUMN——见 b5_kernels._LEVEL_TABLES 注释：
        # ALTER 非幂等会炸 init_db 的二次调用）。与 demands 同事务提交。
        conn.execute(
            "INSERT INTO demand_levels (demand_id, level) VALUES (?, ?)",
            (demand_id, level),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "demand_id": demand_id,
        "level": level,
        "parsed": parsed,
        "raw_text": raw_text,
        "conflicts": conflicts,
        "message": message,
    }


ROUTES = {
    ("POST", "/api/demands"): demand,
}
