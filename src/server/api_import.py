"""r18onN CSV 批量导入接口：POST /api/import/csv（卡面写死，不改口径）。

请求体（契约形态）：
    {"csv": "<CSV 全文>", "dry_run": true|false}
    dry_run 缺省 true（不显式要求写库就只预检）。

    兜底形态：请求体不是 JSON 而是 CSV 文件原文（真实场景：老师把 Excel
    导出的 .csv 直接 POST）。编码先试 utf-8-sig、失败退 gb18030；
    dry_run 从 query 读（?dry_run=false 才写库），缺省同为 true。
    详见 b_import_kernels.extract_request 的设计说明。

响应 200：{"ok":true,"dry_run":bool,"imported":N,"errors":[],
           "encoding":"utf-8-sig"|"gb18030",
           "conflicts":{"count":N,"from":"...","to":"..."}}（区间>62天附 advisory）
响应 400：(400, {..., "errors":[{"line":12,"message":"..."}]}) —— 任一行错，
    错误列**全部**行号，一条也不放过；库内 0 条新增。
401：未登录（@require_auth）。
403：跨组织（K.resolve_authorized_org）/ 权限不够（仅 own 档可导入）。

权限设计（卡面安全核心）：
    仅 own 档可导入（实管员）。teacher 的天花板是 propose（authz.py:144
    的 min(max, ceiling)），授 own 也被封顶 → 永远 403。
    理由：导入是**批量直接写库**，允许 propose 档走这条路等于给
    B8 审批流开后门。⛔ 不得以「方便老师试用」为理由放宽。

落库走 b_import_kernels.write_rows → 领域层 create_periodic，
与手工录入同一条路径（逐字段一致）。

签名约定（写死，不得改）：handle(method, path, query, body, headers, context=None)。
只用 Python 标准库。
"""

from __future__ import annotations

from . import auth
from . import b3_kernels as K
from . import b_import_kernels as B
from . import config

from .http_api import ApiError


@auth.require_auth
def import_csv(method, path, query, body, headers, context=None):
    """POST /api/import/csv → 200 / 400 / 401 / 403（见模块 docstring）。"""
    csv_text, encoding, dry_run = B.extract_request(body)
    if dry_run is None:
        # 原生 CSV 兜底形态：dry_run 从 query 解析，缺省 true（写库必须显式要求）
        dry_run = B.parse_dry_run_query(query)

    conn = config.open_db()
    try:
        # 越权校验 + group → org_id：group 可缺省（= 用户默认组），
        # 跨组织 / 无授权 → 403（K.resolve_authorized_org，与 B3/B4 同一出口）。
        effective_group, org_id = K.resolve_authorized_org(
            conn, context["user_id"], K.parse_group(query)
        )
        # 档位校验：仅 own 档（admin 本组织短路 own；teacher 封顶 propose → 403）
        B.require_import_level(conn, context["user_id"], effective_group)

        cfg = config.get_current_config()
        return B.run_import(
            conn,
            org_id,
            created_by=context.get("object_id"),
            csv_text=csv_text,
            encoding=encoding,
            dry_run=dry_run,
            today=cfg.today,
        )
    finally:
        conn.close()


ROUTES = {
    ("POST", "/api/import/csv"): import_csv,
}
