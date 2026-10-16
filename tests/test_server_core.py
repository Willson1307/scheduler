"""F6 冷启动用例（deploy 卡）：删 data/ → 一条命令 → 30 秒可演示。

运行：python -m unittest discover -s tests -p "test_server_core.py" -v

口径（与团队测试约定一致，见 docs/23「测试用 :memory: + port=0，不起真服务」）：
  - **不污染真实 data/**：全程在 tempfile 临时目录建库（docs/25：不要动演示库）；
  - **port=0**：进程内起 SchedulerHTTPServer 绑 127.0.0.1:0，随机端口，测完即关；
  - 「health 200」「页面可打开」在进程内**真打一次 HTTP**，而不是直调 handler ——
    静态页与路由分发只有真正过一遍 http_api 才算验过。

为什么额外验 /api/resolve：docs/17 §7 的事故里 health / doctor / demo_cli 全绿，
但 main 上 /api/resolve 是 404（代码只在 feature 分支）。**只验 health 会漏掉
「接口不在这个分支上」**，所以冷启动用例必须打一次业务路由。

外部依赖：无。不起真服务、不联网、不写 data/。
"""

import argparse
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler.db import connect  # noqa: E402
from server import routes  # noqa: E402
from server.config import ServerConfig, ensure_database, set_current_config  # noqa: E402
from server.http_api import SchedulerHTTPServer  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 演示日期：2026-09-08 所在周周一 = 2026-09-07（第 1 周），23 处冲突全在这一周。
#: 🔴 不带 --today 会落到「今天」所在周，只扫到 1 处冲突且不报错（docs/17 §1）。
DEMO_TODAY = date(2026, 9, 8)

#: seed_demo 建的默认管理员（权威出处 src/scheduler/seeds.py:38，勿凭记忆写）
ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "admin123"

#: 演示库口径：students_per_class=10 → 30 名学生 → 23 处原始冲突 / 3 个冲突组
RAW_COUNT_EXPECT = 23
COUNT_EXPECT = 3

#: 绕过系统代理：否则代理会拦 127.0.0.1，把「服务未就绪」误判成「服务异常」
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(base, method, path, body=None, token=None, timeout=10.0):
    """发一个 JSON HTTP 请求，返回 (status, 解析后的 body)。4xx/5xx 也解析不抛。"""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except (urllib.error.URLError, OSError) as e:
        return None, {"_unreachable": repr(e)}


def _parse(data):
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _load_start_module():
    """按路径加载 deploy/start.py（F6 的「一条命令」，作为被测模块）。"""
    path = os.path.join(REPO_ROOT, "deploy", "start.py")
    spec = importlib.util.spec_from_file_location("deploy_start_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestColdStartSeed(unittest.TestCase):
    """库缺失 → ensure_database 自动建库 + seed 到 23 处口径（CP-G 的数据侧）。"""

    def test_missing_db_is_seeded_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "data", "demo.db")
            cfg = ServerConfig(db_path=db_path, today=DEMO_TODAY)
            self.assertFalse(os.path.exists(db_path), "前提：库不存在")
            report = ensure_database(cfg)
            self.assertTrue(report["created"], "库缺失必须自动创建，否则「一条命令」不成立")
            self.assertTrue(os.path.exists(db_path))
            # AGENTS §9b：周末槽位必须补，否则周末活动映射出空掩码
            self.assertGreater(report["weekend_slots_added"], 0)

    def test_existing_db_is_reused_not_reseeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "data", "demo.db")
            cfg = ServerConfig(db_path=db_path, today=DEMO_TODAY)
            ensure_database(cfg)
            report = ensure_database(cfg)  # 第二次：库已存在
            self.assertFalse(report["created"], "已存在的库不得被重建")
            conn = connect(db_path)
            try:
                orgs = conn.execute("SELECT COUNT(*) AS c FROM organizations").fetchone()["c"]
            finally:
                conn.close()
            self.assertEqual(orgs, 1, "重新 seed 会多出一份组织")

    def test_no_seed_without_db_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ServerConfig(
                db_path=os.path.join(tmp, "data", "demo.db"),
                today=DEMO_TODAY,
                seed_if_missing=False,
            )
            with self.assertRaises(FileNotFoundError):
                ensure_database(cfg)


class TestColdStartServer(unittest.TestCase):
    """进程内起服务（127.0.0.1:0），验「一条命令之后」的可演示面。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        # 库刻意放在临时目录：本类不得碰仓库真实 data/
        cls.cfg = ServerConfig(
            db_path=os.path.join(cls._tmp.name, "data", "demo.db"),
            port=0,
            today=DEMO_TODAY,
        )
        cls.report = ensure_database(cls.cfg)
        set_current_config(cls.cfg)
        cls.server = SchedulerHTTPServer(("127.0.0.1", 0), cls.cfg)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls._tmp.cleanup()

    def test_db_was_created_from_nothing(self):
        self.assertTrue(self.report["created"])
        self.assertTrue(os.path.exists(self.cfg.db_path))

    def test_routes_scanned(self):
        # CP-B 判据：约定式自动注册 scan() 出 count > 0
        table = routes.get_routes()
        self.assertGreater(len(table), 0)
        self.assertIn(("GET", "/api/health"), table)

    def test_health_ok(self):
        status, body = _request(self.base, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("db"), "ok")
        self.assertEqual(body.get("server_date"), DEMO_TODAY.isoformat())

    def test_page_opens(self):
        status, _ = _request(self.base, "GET", "/")
        self.assertEqual(status, 200)
        req = urllib.request.Request(self.base + "/", headers={"Accept": "text/html"})
        with _OPENER.open(req, timeout=10) as resp:
            self.assertIn("text/html", (resp.headers.get("Content-Type") or "").lower())
            self.assertIn(b"<!DOCTYPE html", resp.read()[:1024])

    def test_static_asset_served(self):
        status, _ = _request(self.base, "GET", "/app.js")
        self.assertEqual(status, 200)

    def test_login_issues_token_without_leaking_password(self):
        status, body = _request(self.base, "POST", "/api/login",
                                {"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)
        self.assertTrue(body.get("token"))
        self.assertNotIn("password_hash", body)
        self.assertNotIn("password", body)

    def test_wrong_password_is_rejected(self):
        status, _ = _request(self.base, "POST", "/api/login",
                             {"login": ADMIN_LOGIN, "password": "wrong-on-purpose"})
        self.assertEqual(status, 401)

    def _admin_token(self) -> str:
        status, body = _request(self.base, "POST", "/api/login",
                                {"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
        self.assertEqual(status, 200, "拿不到 admin token，后续用例全部无意义")
        return body["token"]

    def test_conflicts_demo_caliber(self):
        token = self._admin_token()
        status, body = _request(self.base, "GET", "/api/conflicts", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body.get("raw_count"), RAW_COUNT_EXPECT)
        self.assertEqual(body.get("count"), COUNT_EXPECT)

    def test_resolve_route_present_and_works(self):
        # 🔴 守住 docs/17 §7：health 绿但 /api/resolve 404（B4 只在 feature 分支）
        token = self._admin_token()
        status, body = _request(self.base, "POST", "/api/resolve", {}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body.get("remaining_before"), RAW_COUNT_EXPECT)
        self.assertEqual(body.get("remaining_after"), 0)
        self.assertEqual(body.get("termination"), "ok")

    def test_unauthenticated_business_api_is_401(self):
        status, _ = _request(self.base, "GET", "/api/conflicts")
        self.assertEqual(status, 401)


class TestStartScript(unittest.TestCase):
    """deploy/start.py 的可测片段：参数拼装与端口预检（不起服务）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_start_module()

    def test_build_server_args_injects_today_and_port(self):
        ns = argparse.Namespace(host="127.0.0.1", port=8000, today="2026-09-08",
                                db=None, no_seed=False)
        args = self.mod.build_server_args(ns)
        self.assertEqual(args[0], sys.executable)
        self.assertIn("--today", args)
        self.assertIn("2026-09-08", args)
        self.assertIn("--port", args)
        self.assertIn("8000", args)
        self.assertNotIn("--no-seed", args)

    def test_build_server_args_passes_db_and_no_seed(self):
        ns = argparse.Namespace(host="127.0.0.1", port=0, today="2026-09-08",
                                db="data/trial.db", no_seed=True)
        args = self.mod.build_server_args(ns)
        self.assertIn("--db", args)
        self.assertIn("data/trial.db", args)
        self.assertIn("--no-seed", args)

    def test_port_in_use_detects_listening_socket(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(self.mod.port_in_use("127.0.0.1", port))
        finally:
            srv.close()

    def test_port_zero_is_never_treated_as_busy(self):
        self.assertFalse(self.mod.port_in_use("127.0.0.1", 0))


if __name__ == "__main__":
    unittest.main()
