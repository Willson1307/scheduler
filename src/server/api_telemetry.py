"""S14 聚合读取接口：GET /api/telemetry（feat/ops）。

只出聚合数（试用人数 / 使用时长 / 任务完成率 / 满意度），不含任何个人可识别字段。
权限：需登录且 role == 'admin'（仅管理员/队长可看试用进度，docs/11「怎么给老师看」）。

聚合逻辑唯一入口 = telemetry.aggregate()（与 scripts/telemetry_report.py 共用，单一口径源）。

只用 Python 标准库。
"""

from __future__ import annotations

from . import auth, config, telemetry


@auth.require_auth
def get_telemetry(method, path, query, body, headers, context=None):
    if context.get("role") != "admin":
        return 403, {"error": {"code": "forbidden", "message": "仅管理员可查看试用报告"}}
    conn = config.open_db()
    try:
        return telemetry.aggregate(conn)
    finally:
        conn.close()


ROUTES = {
    ("GET", "/api/telemetry"): get_telemetry,
}
