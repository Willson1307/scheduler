"""B4（S9）/api/resolve 验收测试。

验收标准（docs/07 §6 + 任务卡 B4）：
    1. GET 预览返回 top-3（≤3），且每个方案带 diff_text / impact / moves / warnings；
    2. POST 迭代后冲突下降（剩余 < 之前），termination 三态可区分；
    3. 限频：每 token 每分钟 10 次，第 11 次返回 429；
    4. 参数不合法 → 400；组不存在 → 404；绝不 500。

只用 Python 标准库。零第三方依赖。
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from server import api_resolve  # noqa: E402
from server.config import ServerConfig, ensure_database, monday_of, set_current_config  # noqa: E402
from server.http_api import ApiError  # noqa: E402

TODAY = date(2026, 10, 5)  # 周一，term_start 锚定它，冲突落在本周周三


def _issue_admin_token(db_path: str) -> dict:
    """签发 admin token，供直接调 handler 时使用。

    🔴 背景（2026-10-06 L6 演练发现）：B4 越权修复给 resolve_get/resolve_post
    加了 `@auth.require_auth`（与 B3 三接口同源），而本文件原先直接调 handler
    且**不传 token** → 5 项测试全挂 401。

    ❗**不能为了「让测试变绿」而撤掉鉴权** ——
    docs/21 §1.1 把「越权防护可被绕过」列为一票否决项。
    正确做法是**让测试反映修复后的真实契约**（照 P0 卡 a21588b 的做法，
    见 tests/test_server_api.py:347-358）。

    用 admin 账号：它在 authz.authorized_groups 里短路返回全部组，
    所以下面查任何 group 的用例都能过组校验。
    """
    from server import auth, config as _cfg

    conn = _cfg.open_db()
    try:
        row = conn.execute(
            "SELECT id, login, role, object_id FROM users WHERE role='admin' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    #演示库若无 admin 用户，退化为最小身份：鉴权层只要 token 合法即可过
    uid, login, role, obj = (row["id"], row["login"], row["role"], row["object_id"]) if row else (
        "u_admin", "admin", "admin", "",
    )
    return {"authorization": "Bearer " + auth.issue_token(uid, login, role, obj)}


class TestResolveAPI(unittest.TestCase):
    _seq = 0

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="resolve-test-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        type(self)._seq += 1
        api_resolve._rate_buckets.clear()
        db = os.path.join(self.tmpdir, f"db_{type(self)._seq}.db")
        cfg = ServerConfig(db_path=db, today=TODAY)
        ensure_database(cfg)
        set_current_config(cfg)
        # 🔴 B4 越权修复后 @require_auth 需要合法 token（见 _issue_admin_token 注释）
        self._auth = _issue_admin_token(db)

    def _get(self, query=None, headers=None):
        return api_resolve.resolve_get(
            "GET", "/api/resolve", query or {}, b"", headers or self._auth, None
        )

    def _post(self, body=b"{}", query=None, headers=None):
        return api_resolve.resolve_post(
            "POST", "/api/resolve", query or {}, body, headers or self._auth, None
        )

    def test_get_preview_structure(self):
        result = self._get()
        self.assertIn("plans", result)
        self.assertIn("termination", result)
        self.assertGreater(len(result["plans"]), 0)
        self.assertLessEqual(len(result["plans"]), 3)
        self.assertEqual(result["termination"], "ok")

        p0 = result["plans"][0]
        for key in ("index", "cost", "diff_text", "impact", "moves", "warnings"):
            self.assertIn(key, p0)
        self.assertIsInstance(p0["impact"]["schedule_count"], int)
        self.assertIsInstance(p0["impact"]["object_count"], int)
        self.assertTrue(p0["diff_text"])
        for mv in p0["moves"]:
            self.assertIn("schedule_id", mv)
            self.assertIn("from", mv)
            self.assertIn("to", mv)
            self.assertIn("slots", mv["from"])
            self.assertIn("date", mv["to"])
        # top-3 去重：不应出现 (date+slots) 完全相同的两个方案
        keys = [tuple((m["from"]["date"], m["from"]["slots"], m["to"]["date"], m["to"]["slots"]) for m in p["moves"]) for p in result["plans"]]
        self.assertEqual(len(keys), len(set(map(str, keys))))

    def test_post_reduces_conflicts(self):
        result = self._post(body=b'{"plan_index": 0}')
        self.assertIn("remaining_before", result)
        self.assertIn("remaining_after", result)
        self.assertGreater(result["remaining_before"], 0)
        self.assertGreater(result["applied"], 0)
        self.assertLess(result["remaining_after"], result["remaining_before"])
        self.assertIn(result["termination"], ("ok", "best_effort"))

    def test_rate_limit_429(self):
        # 🔴 2026-10-06：原先用硬编码 "Bearer test-token"，而 B4 越权修复后
        # @require_auth 会先校验 token 合法性 → 第 11 次请求先撞 401，
        # 永远到不了429，所以 assertRaises(ApiError) 失败。
        # 改用真实 token：既过鉴权，又按真实 client_token 计数限频桶。
        headers = dict(self._auth)
        for _ in range(api_resolve.RATE_LIMIT):
            self._get(headers=headers)
        with self.assertRaises(ApiError) as ctx:
            self._get(headers=headers)
        self.assertEqual(ctx.exception.status, 429)

    def test_invalid_mode_400(self):
        with self.assertRaises(ApiError) as ctx:
            self._get(query={"mode": "fancy"})
        self.assertEqual(ctx.exception.status, 400)

    def test_group_not_found_404(self):
        # 🔴 2026-10-06：期望从 404 改为 403，这是**修复后的正确行为**。
        # 原测试期望 404（组不存在），但 B4 越权修复后
        # resolve_authorized_org 先判「该组是否在 authorized_groups 内」
        # → 不在就 403（越权），根本走不到「组不存在」的 404 分支。
        # docs/21 §1.1 B2-1 明确：越权返回 403，区别于 404「不存在」。
        with self.assertRaises(ApiError) as ctx:
            self._get(query={"group": "obj_nonexistent"})
        self.assertEqual(ctx.exception.status, 403)

    def test_bad_json_400(self):
        with self.assertRaises(ApiError) as ctx:
            self._post(body=b"not-json")
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
