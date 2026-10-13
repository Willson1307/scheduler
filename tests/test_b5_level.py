"""S18 调整幅度档位（demand_levels 旁表）回归测试。

覆盖（docs/19 §224 + S18 任务卡验收四条）：
  1. 新库：init_db 后 demand_levels 表在位，level 默认 'minimal'；
  2. 🔴 旧库兜底：demands 表在（S17 建的库）、demand_levels 不在时，
     ensure_demands 补表，且旧数据（无档位行）读出 'minimal'；
  3. 接口层：POST /api/demands 缺省 level → 200 且落库 minimal；
     显式 balanced/optimal → 落库；非法值 → 400；未登录 → 401；
  4. 幂等：ensure_demands 重复调用不炸（init_db 全量 IF NOT EXISTS）。

🔴 设计口径（2026-10-13 实测结论）：level 存 1:1 旁表而不是
ALTER demands ADD COLUMN——ALTER 非幂等，而 init_db 契约是「可重复调用」，
seeds.seed_demo 内部会二次 init_db，schema.sql 放 ALTER 会让既有
「init_db + seed_demo」调用链全部 duplicate column name（实测 5 个既有
测试 ERROR）。旁表幂等，新库旧库同一套 DDL。

🔴 本文件钉的是「第 7 处静默失效」防线：doctor / 契约测试都走新建库，
发现不了「已存在 data/*.db 缺 demand_levels」——只有这里手工造旧库。

只用 Python 标准库。零第三方依赖。

运行：python -m unittest discover -s tests -p "test_b5_level.py" -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.slots import setup_default_slots  # noqa: E402
from server import api_demands, auth, config, routes  # noqa: E402
from server import b5_kernels as B5  # noqa: E402
from server.config import ServerConfig, set_current_config  # noqa: E402

TODAY = date(2026, 10, 13)


def _build_org(db_path: str) -> dict:
    """建一个可控的小组织（不用 demo seed，保证测试确定性）。"""
    conn = connect(db_path)
    init_db(conn)
    org_id, root_id = og.create_organization(conn, "测试校")
    cls = og.create_object(conn, org_id, "group", "高一1班")
    og.add_edge(conn, root_id, cls)
    admin_obj = og.create_object(conn, org_id, "user", "管理员")
    og.create_user(conn, admin_obj, "admin", "admin123", role="admin")
    setup_default_slots(conn, org_id)
    conn.commit()
    conn.close()
    return {"org": org_id, "root": root_id, "cls": cls, "admin_obj": admin_obj}


def _drop_demand_levels(db_path: str) -> None:
    """把库改造成「S18 之前的旧库」：demands 在、demand_levels 不在，
    并留一条旧需求行（S17 时登记的，没有档位行）。"""
    conn = connect(db_path)
    conn.execute("DROP TABLE demand_levels")
    org_id = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
    user_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO demands (id, org_id, user_id, raw_text, hard, parsed) "
        "VALUES ('dem_legacy', ?, ?, '下周三下午实验室A我要用，必须安排两节课', 1, '{}')",
        (org_id, user_id),
    )
    conn.commit()
    conn.close()


def _admin_token(db_path: str) -> dict:
    """签发 admin token，供直接调 handler 时使用（照 test_server_resolve 范式）。"""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, login, role, object_id FROM users WHERE role='admin' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    uid, login, role, obj = row["id"], row["login"], row["role"], row["object_id"]
    return {"authorization": "Bearer " + auth.issue_token(uid, login, role, obj)}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="b5-level-")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.fix = _build_org(self.db_path)
        self.cfg = ServerConfig(
            host="127.0.0.1", port=0, db_path=self.db_path, today=TODAY,
            seed_if_missing=False,
        )
        set_current_config(self.cfg)
        routes.reset_cache()
        self.auth_headers = _admin_token(self.db_path)

    def tearDown(self):
        set_current_config(None)
        routes.reset_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _post(self, body_obj, headers=None):
        body = json.dumps(body_obj).encode("utf-8")
        result = api_demands.demand(
            "POST", "/api/demands", {}, body, headers if headers is not None else self.auth_headers
        )
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            return result[0], result[1]
        return 200, result

    def _read_level(self, demand_id):
        conn = connect(self.db_path)
        try:
            return B5.read_level(conn, demand_id)
        finally:
            conn.close()


class TestSchemaNewDb(unittest.TestCase):
    """新库：init_db 后 demand_levels 在位、默认 minimal、CHECK 生效。"""

    def test_fresh_db_has_demand_levels_table(self):
        conn = connect(":memory:")
        try:
            init_db(conn)
            cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(demand_levels)")}
            self.assertIn("level", cols)
            self.assertEqual(cols["level"]["dflt_value"], "'minimal'")
            self.assertEqual(cols["level"]["notnull"], 1)
        finally:
            conn.close()

    def test_check_rejects_bogus_level(self):
        conn = connect(":memory:")
        try:
            init_db(conn)
            org_id, _ = og.create_organization(conn, "测试校")
            conn.execute(
                "INSERT INTO demands (id, org_id, raw_text) VALUES ('dem_x', ?, 't')",
                (org_id,),
            )
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "INSERT INTO demand_levels (demand_id, level) VALUES ('dem_x', 'bogus')"
                )
            self.assertIn("CHECK", str(ctx.exception))
        finally:
            conn.close()


class TestEnsureBackfill(_Base):
    """🔴 旧库兜底：demands 在、demand_levels 缺 → 补表 + 旧数据读 minimal。"""

    def setUp(self):
        super().setUp()
        _drop_demand_levels(self.db_path)

    def test_old_db_gets_demand_levels(self):
        conn = connect(self.db_path)
        try:
            tables = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("demand_levels", tables)  # 前提确认：真的是旧库
            B5.ensure_demands(conn)
            tables = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("demand_levels", tables)
        finally:
            conn.close()

    def test_legacy_row_reads_minimal(self):
        """验收第 4 条：旧数据（S18 前登记、无档位行）读出来是 minimal。"""
        conn = connect(self.db_path)
        try:
            B5.ensure_demands(conn)
        finally:
            conn.close()
        self.assertEqual(self._read_level("dem_legacy"), "minimal")

    def test_ensure_idempotent(self):
        """重复调用不得炸（init_db 全量 IF NOT EXISTS，schema.sql 无 ALTER）。"""
        conn = connect(self.db_path)
        try:
            B5.ensure_demands(conn)
            B5.ensure_demands(conn)
        finally:
            conn.close()

    def test_post_on_old_db_not_500(self):
        """🔴 核心回归：旧库上真打接口，必须 200 而不是 500（no such table）。"""
        status, payload = self._post({"raw_text": "下周三下午实验室A我要用，必须安排两节课"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["level"], "minimal")
        self.assertEqual(self._read_level(payload["demand_id"]), "minimal")
        # 旧数据仍在且可读
        self.assertEqual(self._read_level("dem_legacy"), "minimal")


class TestDemandLevel(_Base):
    """新库接口层：缺省 minimal / 显式档位 / 非法 400 / 401。"""

    def test_default_level_is_minimal(self):
        status, payload = self._post({"raw_text": "明天下午想用会议室1", "hard": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["level"], "minimal")
        self.assertEqual(self._read_level(payload["demand_id"]), "minimal")

    def test_explicit_balanced_and_optimal(self):
        for lv in ("balanced", "optimal"):
            status, payload = self._post({"raw_text": "周五上午音乐教室排练", "level": lv})
            self.assertEqual(status, 200)
            self.assertEqual(payload["level"], lv)
            self.assertEqual(self._read_level(payload["demand_id"]), lv)

    def test_bogus_level_400(self):
        status, payload = self._post({"raw_text": "测试原话", "level": "bogus"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "bad_request")
        self.assertIn("minimal/balanced/optimal", payload["error"]["message"])

    def test_null_level_falls_back_to_minimal(self):
        status, payload = self._post({"raw_text": "测试原话", "level": None})
        self.assertEqual(status, 200)
        self.assertEqual(payload["level"], "minimal")

    def test_missing_token_401(self):
        status, payload = self._post({"raw_text": "测试原话"}, headers={})
        self.assertEqual(status, 401)
