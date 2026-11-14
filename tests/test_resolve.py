"""卡 rHrY9D · v1.1 出向 D1：消解结果「一句人话解释」。

运行：python -m unittest tests.test_resolve -v

⚠️ 文件命名说明：仓库里已有 `test_server_resolve.py`（B4 消解接口）与
`test_resolve_snapshot.py`（撤销/固化）。本文件按卡面要点命名，只放
**出向解释**相关用例，不重复覆盖消解本身。

---------------------------------------------------------------------------
🔴 本文件钉死的口径（每条对应卡面一条验收 + 一个静默失效风险）
---------------------------------------------------------------------------

  1. **数字一致性是硬闸**：模型输出里的每个阿拉伯数字必须出现在事实里，
     否则整条丢弃、回退模板句。卡面原话防的是「23 处被写成 12 处」。
  2. **主流程不被模型阻塞**：`POST /api/resolve` 一行都不许调模型 ——
     解释走独立路由 `POST /api/resolve/explain`，前端异步补。
  3. **失败不 5xx**：模型返 None / 撒谎 / 异常 → 200 + 模板句 + degraded。
  4. **只读**：explain 前后库内容逐字节一致（比对全部 schedule_periodic 行）。

只用 Python 标准库，全部注入 `complete`，不打真网。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from server import api_resolve, config  # noqa: E402
from server import b_explain_kernels as X  # noqa: E402
from server.config import ServerConfig, ensure_database, set_current_config  # noqa: E402

TODAY = date(2026, 10, 5)  # 周一；演示库冲突落在本周周三


def _issue_admin_token() -> dict:
    """签发 admin token（与 test_server_resolve 同手法：不撤鉴权、只提供合法身份）。"""
    from server import auth

    conn = config.open_db()
    try:
        row = conn.execute(
            "SELECT id, login, role, object_id FROM users WHERE role='admin' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"authorization": "Bearer " + auth.issue_token("u_admin", "admin", "admin", "")}
    return {
        "authorization": "Bearer "
        + auth.issue_token(row["id"], row["login"], row["role"], row["object_id"])
    }


class _DbCase(unittest.TestCase):
    _seq = 0

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="resolve-explain-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        type(self)._seq += 1
        api_resolve._rate_buckets.clear()
        self.db = os.path.join(self.tmpdir, f"db_{type(self)._seq}.db")
        cfg = ServerConfig(db_path=self.db, today=TODAY)
        ensure_database(cfg)
        set_current_config(cfg)
        self.auth = _issue_admin_token()

    # -- helpers ----------------------------------------------------------
    def _resolve(self) -> dict:
        """真跑一次一键消解，拿到 snapshot_id（写的是临时库，不是 data/demo.db）。"""
        return api_resolve.resolve_post(
            "POST", "/api/resolve", {}, b"{}", self.auth, None
        )

    def _explain(self, sid: str) -> dict:
        return api_resolve.resolve_explain(
            "POST", "/api/resolve/explain", {"snapshot_id": [sid]}, b"{}", self.auth, None
        )


# ---------------------------------------------------------------------------
# 一、内核：数字一致性（卡面验收 8）
# ---------------------------------------------------------------------------

class TestGroundingGate(unittest.TestCase):
    """`b_explain_kernels.grounded()` 与 `explain_resolve()` 的硬闸。"""

    FACTS = {
        "applied": 3,
        "before": 23,
        "after": 0,
        "changes": [
            {"object": "高一(1)班", "title": "物理", "from": "周三第1节", "to": "周三第4节"},
        ],
        "extra": [],
    }

    def test_truthful_numbers_pass(self):
        """数字都在事实里 → 采用模型输出，并把令牌还原成真名。"""
        fake = lambda system, user, timeout_s: "把对象1这节物理挪到周三第4节，冲突从 23 处降到 0 处。"
        got = X.explain_resolve(self.FACTS, names=["高一(1)班"], complete=fake)
        self.assertEqual(got["source"], "llm")
        self.assertFalse(got["degraded"])
        self.assertIn("高一(1)班", got["explanation"])
        self.assertNotIn("对象1", got["explanation"])

    def test_tampered_number_is_dropped(self):
        """🔴 验收 8：23 被写成 12 → 整条丢弃，回退模板句。"""
        fake = lambda system, user, timeout_s: "冲突从 12 处降到 0 处，主要挪了对象1的物理。"
        got = X.explain_resolve(self.FACTS, names=["高一(1)班"], complete=fake)
        self.assertEqual(got["source"], "template", "数字不接地必须整条丢弃")
        self.assertTrue(got["degraded"])
        self.assertNotIn("12", got["explanation"])
        self.assertIn("23", got["explanation"], "模板句要给真实数字")

    def test_chinese_numeral_evasion_is_dropped(self):
        """用中文数字绕过核验（「连上两节」）→ 同样判不接地。"""
        fake = lambda system, user, timeout_s: "调整后当天上午连上两节，冲突降到 0 处。"
        got = X.explain_resolve(self.FACTS, names=["高一(1)班"], complete=fake)
        self.assertEqual(got["source"], "template")

    def test_complete_none_falls_back_to_template(self):
        got = X.explain_resolve(self.FACTS, names=["高一(1)班"], complete=lambda *a, **k: None)
        self.assertEqual(got["source"], "template")
        self.assertTrue(got["degraded"])
        self.assertIn("应用了 3 个方案", got["explanation"])

    def test_model_exception_is_swallowed(self):
        def boom(*a, **k):
            raise TimeoutError("模拟超时")

        got = X.explain_resolve(self.FACTS, names=["高一(1)班"], complete=boom)
        self.assertEqual(got["source"], "template")

    def test_explanation_is_hard_truncated(self):
        """字数上限 120 是服务端硬截断，不靠提示词自觉。"""
        long_text = "这次调整把对象1的物理挪到周三第4节，冲突从 23 处降到 0 处，" + "很长的补充" * 40
        fake = lambda system, user, timeout_s: long_text
        got = X.explain_resolve(self.FACTS, names=["高一(1)班"], complete=fake)
        self.assertLessEqual(len(got["explanation"]), X.MAX_D1_CHARS + 1)


# ---------------------------------------------------------------------------
# 二、路由：真打 /api/resolve/explain
# ---------------------------------------------------------------------------

class TestTemplateFormat(unittest.TestCase):
    """模板句可读性：线上实测暴露的两处（对象名占位符、原始时间戳）。

    这两条都是「不报错、只是难看」的类型 —— 单测不钉，改回去没人发现，
    而现场演示时老师第一眼就看这句。
    """

    def test_placeholder_object_is_not_printed(self):
        """没有 target 参与者的日程查不到对象名（占位「—」）→ 不能输出「例如 —《…》」。"""
        facts = {
            "applied": 1,
            "before": 14,
            "after": 0,
            "changes": [{"object": "—", "title": "示例活动",
                         "from": "11月11日 10:00", "to": "11月10日 09:50"}],
            "extra": [],
        }
        text = X.template_resolve(facts)
        self.assertNotIn("—", text)
        self.assertIn("例如《示例活动》", text)

    def test_named_object_is_still_printed(self):
        """有对象名时不能把它吞掉 —— 上面那条修的是占位符，不是删除主语。"""
        facts = {
            "applied": 1,
            "before": 23,
            "after": 0,
            "changes": [{"object": "高一(1)班", "title": "物理",
                         "from": "周三第1节", "to": "周三第4节"}],
            "extra": [],
        }
        self.assertIn("例如 高一(1)班《物理》", X.template_resolve(facts))

    def test_iso_timestamp_is_formatted(self):
        """一次性活动的 start_at 是 ISO 串，模板句里必须是「11月11日 10:00」。"""
        self.assertEqual(api_resolve._fmt_when("2026-11-11T10:00"), "11月11日 10:00")
        self.assertEqual(api_resolve._fmt_when("2026-11-10 09:50"), "11月10日 09:50")
        self.assertEqual(api_resolve._fmt_when(None), "未设置")
        self.assertEqual(api_resolve._fmt_when("未排定"), "未排定")


class TestExplainRoute(_DbCase):
    def _snapshot(self) -> tuple[str, dict]:
        body = self._resolve()
        return body["snapshot_id"], body

    def test_explain_reads_facts_and_never_writes(self):
        """解释只读：前后 schedule_periodic 逐行一致。"""
        sid, _ = self._snapshot()
        conn = config.open_db()
        try:
            before = conn.execute(
                "SELECT schedule_id, slot_bitmap FROM schedule_periodic ORDER BY schedule_id"
            ).fetchall()
        finally:
            conn.close()

        got = self._explain(sid)

        conn = config.open_db()
        try:
            after = conn.execute(
                "SELECT schedule_id, slot_bitmap FROM schedule_periodic ORDER BY schedule_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [tuple(r) for r in before], [tuple(r) for r in after],
            "explain 是只读路由，不许改动任何日程",
        )
        self.assertIn("explanation", got)
        self.assertIn(got["source"], ("llm", "template"))
        self.assertTrue(got["explanation"].strip())

    def test_lying_model_falls_back_to_template(self):
        """🔴 验收 8 的路由版：模型撒谎 → 200 + 模板句（不是 500、不是假数字）。"""
        sid, body = self._snapshot()
        from server import b_llm_client

        orig = b_llm_client.complete
        try:
            b_llm_client.complete = lambda system, user, timeout_s: (
                "这次把 12 处冲突全解决了。"
            )
            got = self._explain(sid)
        finally:
            b_llm_client.complete = orig

        self.assertEqual(got["source"], "template")
        self.assertNotIn("12", got["explanation"])
        self.assertIn(str(body["remaining_before"]), got["explanation"])

    def test_model_slow_does_not_block_resolve(self):
        """🔴 验收 9：`POST /api/resolve` 一行都不许调模型。

        用一个「一调就睡 5 秒」的 complete 顶着，然后计时跑 resolve：
        若哪天有人把解释塞回主流程，这里会立刻变成 5 秒级并红。
        """
        from server import b_llm_client

        calls = []

        def slow(system, user, timeout_s):
            calls.append(1)
            time.sleep(5)
            return "慢回答"

        orig = b_llm_client.complete
        try:
            b_llm_client.complete = slow
            t0 = time.monotonic()
            body = self._resolve()
            elapsed = time.monotonic() - t0
        finally:
            b_llm_client.complete = orig

        self.assertEqual(calls, [], "消解主流程不许同步等模型")
        self.assertLess(elapsed, 3.0, f"主流程被拖慢了：{elapsed:.2f}s")
        self.assertIn("snapshot_id", body)

    def test_missing_snapshot_id_is_400(self):
        from server.http_api import ApiError

        with self.assertRaises(ApiError) as ctx:
            api_resolve.resolve_explain(
                "POST", "/api/resolve/explain", {}, b"{}", self.auth, None
            )
        self.assertEqual(ctx.exception.status, 400)

    def test_unknown_snapshot_is_404(self):
        from server.http_api import ApiError

        with self.assertRaises(ApiError) as ctx:
            self._explain("snap_doesnotexist000")
        self.assertEqual(ctx.exception.status, 404)

    def test_explain_requires_auth(self):
        """未登录 → 401 元组（`@require_auth` 的口径是**返回** 401，不是抛）。"""
        sid, _ = self._snapshot()
        status, body = api_resolve.resolve_explain(
            "POST", "/api/resolve/explain", {"snapshot_id": [sid]}, b"{}", {}, None
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")


if __name__ == "__main__":
    unittest.main(verbosity=2)
