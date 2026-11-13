"""ApiError.extra（结构化错误字段）的验收测试（卡 rrr4vM 追加，2026-11-13）。

设计意图（为什么必须走**真实 HTTP**）：
    `ApiError` → 响应体的序列化发生在 `http_api.py` 的 `do_*` 捕获块里
    （`payload = dict(exc.extra or {}); payload["error"] = exc.message`）。
    直接调 handler 只能看到异常对象上的 `extra`，**看不到真正发出去的 JSON**——
    而本卡要钉的恰恰是「响应体同时有 error 原文与结构化字段」。

四条必须守住的形状（`http_api.py` 是全站共享层，改它影响每一个接口）：

| 状态 | 形状 | 谁在依赖 |
|---|---|---|
| 401 | `{"error": {"code", "message"}}`（**嵌套**，来自 auth 中间件） | 前端 `extractMessage()` |
| 400/403/404 | `{"error": "原文"}`（**扁平，且只有这一个键**） | 全站 `httpErrText()` |
| 409 | 扁平 `error` 原文 **+** `code` / `exception_id` | 前端「一键撤销」 |

    ⛔ 任何一条改成嵌套、或让 extra 覆盖 `error`，全站错误提示会一起变空。
    所以这里的断言是**精确键集**（`set(body) == {...}`），不是 `assertIn`。

运行：python -m unittest tests.test_api_error_extra -v
只用 Python 标准库。
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler.db import connect  # noqa: E402
from server import config  # noqa: E402
from server.config import ServerConfig, ensure_database  # noqa: E402
from server.http_api import SchedulerHTTPServer  # noqa: E402

#: seed_demo 的 term 锚定 2026-09-07；周三 2026-09-09 是「冲突演示」那天。
TODAY = date(2026, 9, 8)
WED = "2026-09-09"


class ApiErrorShapeTest(unittest.TestCase):
    """真实 HTTP：起服务 + http.client 发请求，断言**响应体原文**。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="api_error_extra_")
        cls.db_path = os.path.join(cls.tmp, "demo.db")
        cfg = ServerConfig(db_path=cls.db_path, today=TODAY)
        ensure_database(cfg)
        cls._prev_cfg = config._current
        config.set_current_config(cfg)

        conn = connect(cls.db_path)
        try:
            cls.class_id = conn.execute(
                "SELECT id FROM objects WHERE kind='group' AND name='高一(1)班'"
            ).fetchone()["id"]
            # 🔴 日程 id 是 uuid（新建库每次不同），按标题定位。
            cls.physics_id = conn.execute(
                "SELECT id FROM schedules WHERE title = '（冲突演示）高一(1)班 物理'"
            ).fetchone()["id"]
        finally:
            conn.close()

        cls.server = SchedulerHTTPServer(("127.0.0.1", 0), cfg)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = cls._login("admin", "admin123")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        if cls._prev_cfg is not None:
            config.set_current_config(cls._prev_cfg)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    @classmethod
    def _request(cls, method, path, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
            conn.request(method, path, body=body, headers=headers)
        else:
            conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        status = resp.status
        conn.close()
        return status, data

    @classmethod
    def _login(cls, login, password):
        payload = json.dumps({"login": login, "password": password}).encode("utf-8")
        status, data = cls._request("POST", "/api/login", body=payload)
        assert status == 200, f"登录失败 {login}: {status} {data}"
        return json.loads(data)["token"]

    def _post_exception(self, **body):
        raw = json.dumps(body).encode("utf-8")
        return self._request("POST", "/api/exceptions", token=self.token, body=raw)

    # -- 409：结构化字段（本卡新增能力的落点）-------------------------------
    def test_409_has_flat_error_plus_structured_exception_id(self):
        """409 必须**同时**给：error 原文 + code + exception_id。

        🔴 破坏法：把 `api_exceptions.py` 那处 `extra={...}` 删掉 → 本用例必须红
        （`code` / `exception_id` 消失，只剩那句文案）。
        这也是「不要再从 message 里正则抠 id」的依据：结构化字段存在时它们是同源的。
        """
        first_status, first_data = self._post_exception(
            schedule_id=self.physics_id, date=WED, mode="cancel"
        )
        self.assertEqual(first_status, 200, first_data)
        first = json.loads(first_data)
        self.assertTrue(first["exception_id"])

        status, data = self._post_exception(
            schedule_id=self.physics_id, date=WED, mode="move", new_mask="0x000008"
        )
        self.assertEqual(status, 409, data)
        body = json.loads(data)

        # 1) 扁平的 error 字符串，且是**原文**（含 exception_id= 那句，一字未改）
        self.assertIn("error", body)
        self.assertIsInstance(body["error"], str, "error 必须是字符串，嵌套会让全站 httpErrText 挂")
        self.assertIn("已存在对该日程的 cancel 例外", body["error"])
        self.assertIn("请先撤销它再新建", body["error"])

        # 2) 结构化字段
        self.assertEqual(body.get("code"), "conflict")
        self.assertEqual(body.get("exception_id"), first["exception_id"])

        # 3) 两者同源：文案里的 id 与结构化字段一致（口径漂移会当场暴露）
        self.assertIn(f"exception_id={first['exception_id']}", body["error"])

        # 4) 键集精确：只有这三个，不多不少（多出来的键会被前端当未知字段）
        self.assertEqual(set(body), {"error", "code", "exception_id"})

    # -- 回归：其余状态码的形状必须与改前逐字一致 ---------------------------
    def test_400_body_is_flat_error_only(self):
        """400 只能有 error 一个键（extra 不能泄漏到其它错误上）。"""
        status, data = self._post_exception(mode="cancel")   # 缺 date / schedule_id
        self.assertEqual(status, 400, data)
        body = json.loads(data)
        self.assertEqual(set(body), {"error"}, f"400 响应体多出字段：{sorted(body)}")
        self.assertIsInstance(body["error"], str)
        self.assertIn("date", body["error"])

    def test_403_body_is_flat_error_only(self):
        """403（组越权）保持扁平，且不因 extra 改造变成嵌套。"""
        status, data = self._request(
            "GET",
            f"/api/schedules/detail?object_id={self.class_id}&date={WED}&group=grp_not_mine",
            token=self.token,
        )
        self.assertEqual(status, 403, data)
        body = json.loads(data)
        self.assertEqual(set(body), {"error"}, f"403 响应体多出字段：{sorted(body)}")
        self.assertIsInstance(body["error"], str)
        self.assertEqual(body["error"], "无权访问该组")

    def test_404_body_is_flat_error_only(self):
        """404（对象不属于该组织）保持扁平。"""
        status, data = self._request(
            "GET",
            f"/api/schedules/detail?object_id=obj_does_not_exist&date={WED}",
            token=self.token,
        )
        self.assertEqual(status, 404, data)
        body = json.loads(data)
        self.assertEqual(set(body), {"error"}, f"404 响应体多出字段：{sorted(body)}")
        self.assertIsInstance(body["error"], str)
        self.assertIn("对象不属于该组织", body["error"])

    def test_401_body_stays_nested(self):
        """401 必须仍是嵌套 {error:{code,message}}——它来自 auth，不是 ApiError。

        这条单独钉一次，是因为 `ApiError` 的序列化改动很容易被顺手「统一」到 401 上，
        而前端 `extractMessage()` 两种都吃得下、**不会报错**，只会静默换掉提示形状。
        """
        status, data = self._request(
            "GET", f"/api/schedules/detail?object_id={self.class_id}&date={WED}"
        )
        self.assertEqual(status, 401, data)
        body = json.loads(data)
        self.assertEqual(set(body), {"error"})
        self.assertIsInstance(body["error"], dict, "401 必须是嵌套错误体")
        self.assertEqual(set(body["error"]), {"code", "message"})
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertEqual(body["error"]["message"], "未登录或令牌无效")


if __name__ == "__main__":
    unittest.main()
