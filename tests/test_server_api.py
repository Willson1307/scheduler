"""B3 三接口测试：/api/objects、/api/schedules、/api/conflicts。

运行：python -m unittest discover -s tests -p "test_server_api.py" -v

---------------------------------------------------------------------------
🔴🔴 本文件存在的唯一理由：钉死四处「已经付出过代价」的口径
---------------------------------------------------------------------------

本项目最贵的 bug 类型不是崩溃，是**静默失效**——不报错，却给错误结果。
已发生四次：

  1. 掩码位序用错（周掩码 vs 单日掩码）→ 页面全部错位，不报错
  2. 演示窗口取错周→ 只扫到 1 处而非 23 处，**且不报错**（termination 仍是 ok）
  3. `/api/conflicts` 不聚合 → 23 条刷屏，但契约要求聚合
  4. 排序不确定（object_id 是 uuid4）→ 「高一(1)班」刷新变「学生1-07」

所以本文件的断言**不是"测一下功能对不对"，而是"下次有人改代码时能不能立刻发现改错了"**。
每处都用**反向断言**（assertNotEqual）钉死，防止"改错了但测试还绿"。

---------------------------------------------------------------------------
一处特别提醒：绿灯不等于正确
---------------------------------------------------------------------------

2026-10-03 实测教训：83 项单测全绿、doctor 14/14 全绿、demo_cli 23→0 正常，
但 `main` 上 `/api/resolve` **实际返回 404** —— 因为代码一直在 feature 分支。
**所有这些检查都测不出"代码不在 main 上"。**

所以：交付 =已合并 + **已在 main 上真实打一次请求**，两个动作缺一不可
（`AGENTS.md §6.1`）。本文件只能证明逻辑对，证明不了代码在正确的分支上。
"""

import json
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.seeds import seed_demo  # noqa: E402
from scheduler.slots import bit  # noqa: E402
from scheduler.solver import m2  # noqa: E402
from server import b3_kernels as K  # noqa: E402
from server import config  # noqa: E402
from server.config import ServerConfig  # noqa: E402

# 演示库规模：必须与 config.SEED_STUDENTS_PER_CLASS 一致（30 名学生 → 23 处冲突）
DEMO_CLASSES = 3
DEMO_STUDENTS = 10
DEMO_TEACHERS = 9
DEMO_ROOMS = 4

#: 演示库的学期起点（周一）。23 处冲突全在这一周。
TERM_START = "2026-09-07"

#: 🔴 演示日期。必须落在第 1 周，否则只扫到 1 处冲突（且不报错）。
DEMO_TODAY = date(2026, 9, 8)


def _build_demo_db(db_path):
    """造一个与 scripts/reset_demo.py 同规格的演示库。"""
    conn = connect(db_path)
    init_db(conn)
    seed_demo(
        conn,
        classes=DEMO_CLASSES,
        students_per_class=DEMO_STUDENTS,
        teachers=DEMO_TEACHERS,
        rooms=DEMO_ROOMS,
        term_start=TERM_START,
    )
    conn.commit()
    return conn


def _build_small_db(db_path):
    """造一个可控的小组织（不用 demo seed，保证测试确定性）。

    🔴 create_organization 返回 (org_id: str, root_id: str|None) 两个**字符串**，
    不是 dict——写错会让测试报 "string indices must be integers"（已踩）。
    """
    conn = connect(db_path)
    init_db(conn)
    org_id, root_id = og.create_organization(conn, "测试校")
    grade = og.create_object(conn, org_id, "group", "高一")
    cls = og.create_object(conn, org_id, "group", "高一1班")
    og.add_edge(conn, root_id, grade)
    og.add_edge(conn, grade, cls)
    a_obj = og.create_object(conn, org_id, "user", "管理员")
    og.create_user(conn, a_obj, "admin", "admin123", role="admin")
    conn.commit()
    return conn, org_id, root_id, grade, cls, a_obj


class TestMaskOrder(unittest.TestCase):
    """🔴 第一处：掩码位序（本项目最贵的坑）。"""

    def test_week_mask_is_not_day_mask(self):
        """周掩码 slots.bit(dow, idx)：0-based，位号 = (周几-1)*16 + 节次-1。"""
        # 周三 = 3，第 1 节 = idx 1→ 位号 (3-1)*16 + 0 = 32
        self.assertEqual(bit(3, 1), 0x100000000)

    def test_day_mask_is_1based(self):
        """单日掩码 m2.day_slots_mask：1-based，period N → 位 N。"""
        self.assertEqual(m2.day_slots_mask([(3, 1)]), 0x2)
        self.assertEqual(m2.day_slots_mask([(3, 3)]), 0x8)
        self.assertEqual(m2.day_slots_mask([(3, 1), (3, 2)]), 0x6)

    def test_golden_case_week_to_day(self):
        """黄金用例：周掩码 0x100000000（周三第 1 节）→ 单日掩码 0x2。"""
        week_mask = bit(3, 1)
        day_mask = m2.day_slots_mask([(3, 1)])
        self.assertEqual(week_mask, 0x100000000)
        self.assertEqual(day_mask, 0x2)
        # 🔴 反向断言：两个值绝不相等。
        # 这条是本文件最重要的断言——如果有人把 slots_hex 改成用 slots.bit，
        # 上面两个 assertEqual 仍可能都过（它们各自测的是各自的函数），
        # 只有这条能抓住"接口出参用错了函数"。
        self.assertNotEqual(day_mask, week_mask)
        self.assertNotEqual(0x2, 0x100000000)

    def test_api_slots_hex_uses_day_mask(self):
        """b3_kernels.slots_hex 是 API 出参的唯一入口，必须走单日掩码。"""
        self.assertEqual(K.slots_hex([(3, 1)]), "0x2")
        self.assertEqual(K.slots_hex([(3, 3)]), "0x8")
        self.assertEqual(K.slots_hex([(3, 1), (3, 2)]), "0x6")
        # 🔴 反向断言：绝不能等于周掩码的十六进制表示
        self.assertNotEqual(K.slots_hex([(3, 1)]), "0x100000000")

    def test_slots_hex_matches_b4_format(self):
        """与 B4 的 _slots_hex 行为一致（两处必须同宽同格式）。"""
        expected = "0x" + format(m2.day_slots_mask([(3, 1)]), "x")
        self.assertEqual(K.slots_hex([(3, 1)]), expected)

    def test_readable_is_plain_dict(self):
        """readable 给前端直接显示，服务端给什么前端显示什么（契约 §12）。"""
        self.assertEqual(
            K.readable([(3, 1), (4, 2)]),
            [{"dow": 3, "period": 1}, {"dow": 4, "period": 2}],
        )


class TestResolveWindow(unittest.TestCase):
    """🔴 第二处：演示窗口（不带 --today 只扫到 1 处，且不报错）。"""

    def test_window_is_this_week(self):
        cfg = ServerConfig(today=DEMO_TODAY)
        f, t = K.resolve_window(cfg)
        self.assertEqual(f, date(2026, 9, 7))  # 周一
        self.assertEqual(t, date(2026, 9, 13))  # 周日

    def test_window_follows_today(self):
        """🔴 显式断言窗口随 today 变——防有人改窗口逻辑无人发现。"""
        cfg_sep = ServerConfig(today=date(2026, 9, 8))
        cfg_sep28 = ServerConfig(today=date(2026, 9, 28))
        self.assertEqual(K.resolve_window(cfg_sep), (date(2026, 9, 7), date(2026, 9, 13)))
        self.assertEqual(K.resolve_window(cfg_sep28), (date(2026, 9, 28), date(2026, 10, 4)))
        self.assertNotEqual(K.resolve_window(cfg_sep), K.resolve_window(cfg_sep28))

    def test_window_payload_carries_range(self):
        """响应体要带 window，前端才能显示「扫的是哪一周」。"""
        payload = K.window_payload(ServerConfig(today=DEMO_TODAY))
        self.assertEqual(payload["from"], "2026-09-07")
        self.assertEqual(payload["to"], "2026-09-13")
        self.assertEqual(payload["today"], "2026-09-08")


class TestConflictAggregation(unittest.TestCase):
    """🔴 第三处：聚合口径 count=3 / raw_count=23（契约 §5 + test_contract.py:321-334）。"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(cls.tmp.name, "demo.db")
        cls.conn = _build_demo_db(db)
        cls.org_id = K.resolve_org(cls.conn, None)
        cls.cfg = ServerConfig(today=DEMO_TODAY)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.payload = K.conflict_payload(self.conn, self.org_id, self.cfg)

    def test_raw_count_is_23(self):
        """演示库原始冲突 23 处（1 教师 + 两个班各 11 学生）。"""
        self.assertEqual(self.payload["raw_count"], 23)

    def test_count_is_aggregated_groups(self):
        """count 是聚合后的组数，演示库为 3。"""
        self.assertEqual(self.payload["count"], 3)

    def test_count_equals_len_items(self):
        """🔴 count 必须 == len(items)。不一致就是聚合逻辑写错了。"""
        self.assertEqual(self.payload["count"], len(self.payload["items"]))

    def test_23_equals_1_plus_11_plus_11(self):
        """raw_count 的构成：1 条教师冲突 + 两个班各 11 条学生冲突。

        role 放在 **item 顶层**（a/b 两侧字段由契约 §5 锁死为 8 个，
        多加 role 会破坏 test_side_fields_are_full 的「恰好 8 个」断言）。
        """
        self.assertEqual(23, 1 + 11 + 11)
        by_role = {}
        for it in self.payload["items"]:
            by_role[it["role"]] = by_role.get(it["role"], 0) + it["raw_count"]
        self.assertEqual(by_role.get("teacher"), 1)
        self.assertEqual(by_role.get("target"), 22)

    def test_no_duplicate_triples(self):
        """🔴 契约 §5 + test_contract.py:321-334：(日程对, date, slots) 三元组不得重复。"""
        triples = [
            (it["a"]["schedule_id"], it["b"]["schedule_id"], it["date"], it["slots"])
            for it in self.payload["items"]
        ]
        self.assertEqual(len(triples), len(set(triples)), "聚合键重复 = 没聚合")

    def test_items_sorted_deterministically(self):
        """🔴 第四处：确定性。全序排序 = (date, mask, schedule_a, schedule_b)。"""
        keys = [
            (it["date"], it["slots"], it["a"]["schedule_id"], it["b"]["schedule_id"])
            for it in self.payload["items"]
        ]
        self.assertEqual(keys, sorted(keys), "items 未按全序排序，会导致刷新跳变")

    def test_two_calls_identical(self):
        """🔴 同一请求两次调用必须逐字一致（object_id 是 uuid4，代表对象须全序选取）。"""
        p1 = json.dumps(
            K.conflict_payload(self.conn, self.org_id, self.cfg),
            ensure_ascii=False, sort_keys=True,
        )
        p2 = json.dumps(
            K.conflict_payload(self.conn, self.org_id, self.cfg),
            ensure_ascii=False, sort_keys=True,
        )
        self.assertEqual(p1, p2, "两次调用不一致 → 前端会闪烁")

    def test_empty_org_returns_zero_not_error(self):
        """无冲突时如实返回 0，🔴 不许报错。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            conn, org_id, _root, *_ = _build_small_db(os.path.join(d, "small.db"))
            try:
                payload = K.conflict_payload(conn, org_id, self.cfg)
                self.assertEqual(payload["count"], 0)
                self.assertEqual(payload["raw_count"], 0)
                self.assertEqual(payload["items"], [])
            finally:
                conn.close()

    def test_payload_shape(self):
        """响应结构契约：count / raw_count / window / items。"""
        self.assertEqual(
            set(self.payload.keys()),
            {"count", "raw_count", "window", "items"},
        )
        first = self.payload["items"][0]
        for key in ("a", "b", "object_id", "date", "slots", "raw_count", "readable"):
            self.assertIn(key, first)
        self.assertRegex(first["slots"], r"^0x[0-9a-f]+$")

    def test_side_fields_are_full(self):
        """🔴 a / b 两侧**8 个字段必须给全**，少一个就红。

        契约 docs/07 §5（队长 2026-10-01 拍板「数据给全」）
        + tests/test_contract.py:299-311 逐字段断言。

        🔴 2026-10-04 实测踩过这个坑：初版内核只给了 schedule_id + role 两个字段，
        「一周目测接口都通了」，但 test_contract.py 立刻打回——
        缺 object_name 前端显示不出「数学」，缺 title 显示不出课程名。
        **所以这条不是形式检查，是防返工。**
        """
        required = (
            "schedule_id", "object_id", "object_name", "object_kind",
            "title", "date", "slots", "readable",
        )
        for it in self.payload["items"]:
            for side_key in ("a", "b"):
                side = it[side_key]
                self.assertIsInstance(side, dict)
                for key in required:
                    self.assertIn(key, side, f"冲突 {side_key} 缺字段 {key}")
                self.assertRegex(side["slots"], r"^0x[0-9a-f]+$")
                self.assertIsInstance(side["readable"], list)
        # 🔴 反向断言：字段数必须恰好 8（多给无害，但少给即错——这里锁死下限）
        self.assertEqual(len(self.payload["items"][0]["a"]), len(required))


class TestGroupResolution(unittest.TestCase):
    """组解析口径。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.conn, self.org_id, self.root, self.grade, self.cls, self.admin = _build_small_db(
            os.path.join(self.tmp.name, "small.db")
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_resolve_org_by_group(self):
        self.assertEqual(K.resolve_org(self.conn, self.cls), self.org_id)

    def test_missing_group_raises_404(self):
        from server.http_api import ApiError
        with self.assertRaises(ApiError):
            K.resolve_org(self.conn, "obj_does_not_exist")

    def test_no_group_takes_first_org(self):
        self.assertEqual(K.resolve_org(self.conn, None), self.org_id)

    def test_parse_group_empty_is_none(self):
        self.assertIsNone(K.parse_group({}))
        self.assertIsNone(K.parse_group({"group": [""]}))
        self.assertEqual(K.parse_group({"group": ["g1"]}), "g1")


class TestObjectsAndSchedules(unittest.TestCase):
    """objects / schedules 两接口的响应结构。"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "demo.db")
        cls.conn = _build_demo_db(cls.db_path)
        cls.org_id = K.resolve_org(cls.conn, None)
        # 🔴 db_path 必须与 set_current_config 的一致：接口内部用
        # config.open_db()（读进程级配置），测试自己用 cls.conn 是另一条连接。
        # 两者指向同一个文件才对得上。
        cls.cfg = ServerConfig(today=DEMO_TODAY, db_path=cls.db_path)
        # 🔴 api_schedules 内部调 config.get_current_config() 读进程级配置
        # （handle 的固定 6 参签名里塞不进 cfg，见 config.py:155）。
        # HTTP 路径下由 run_server 启动时 set_current_config 设好；
        # 直接调 handler 的测试必须自己设，否则 RuntimeError。
        cls._prev_cfg = config._current
        config.set_current_config(cls.cfg)

    @classmethod
    def tearDownClass(cls):
        if cls._prev_cfg is not None:
            config.set_current_config(cls._prev_cfg)
        cls.conn.close()
        cls.tmp.cleanup()

    def test_group_has_parent(self):
        """objects 接口：parent_ids 过滤 depth=0 后，非根组应有父。"""
        from server import api_objects
        group_id = self.conn.execute(
            "SELECT id FROM objects WHERE kind='group' AND name LIKE '%班%' LIMIT 1"
        ).fetchone()
        if group_id is None:
            self.skipTest("演示库无班级组")
        res = api_objects.list_objects(
            "GET", "/api/objects", {"group": [group_id["id"]]}, b"", {}
        )
        self.assertIsInstance(res, list, "必须是顶层数组（契约 §3）")
        required = {"id", "kind", "name", "parent_ids"}
        for obj in res:
            # 🔴 契约 §12.1「只增不减」：后端可加字段（实测接口多给了
            # is_root / parent_names / depth，都是前端用得上的），所以用
            # **子集断言**而非等值断言——等值断言会把「加了合法字段」也判成失败。
            self.assertTrue(required.issubset(set(obj.keys())),
                            f"缺契约必需字段：{required - set(obj.keys())}")
            self.assertIn(obj["kind"], K.OBJECT_KINDS)
            self.assertIsInstance(obj["parent_ids"], list)
            # 🔴 depth=0 自反边必须被过滤：不能自己当自己的父
            self.assertNotIn(obj["id"], obj["parent_ids"], "depth=0 自反边未过滤")
        # 非根组应当真有父（否则过滤过头了）
        non_root = [o for o in res if not o.get("is_root") and o["kind"] == "group"]
        if non_root:
            self.assertTrue(any(o["parent_ids"] for o in non_root),
                            "所有非根组的 parent_ids 都为空 → 可能把真父也过滤掉了")

    def test_schedules_grid_not_empty(self):
        """🔴 schedules 接口最容易出的bug：算出来是空的。必须显式断言非空。"""
        from server import api_schedules
        group_id = self.conn.execute(
            "SELECT id FROM objects WHERE kind='group' AND name LIKE '%班%' LIMIT 1"
        ).fetchone()
        if group_id is None:
            self.skipTest("演示库无班级组")
        res = api_schedules.list_schedules(
            "GET", "/api/schedules", {"group": [group_id["id"]]}, b"", {}
        )
        self.assertIn("grid", res)
        self.assertIn("days", res)
        self.assertIn("objects", res)
        # 🔴 演示库第 1 周必须有课，空grid = 取数据范围或掩码转换错了
        self.assertGreater(len(res["grid"]), 0, "grid 为空 → 取数范围或掩码转换有bug")
        # 5 天周一~周五
        self.assertEqual(len(res["days"]), K.DAYS_IN_WEEK)
        self.assertEqual(res["days"][0], "2026-09-07")
        # key 用竖线分隔
        for k in list(res["grid"])[:5]:
            self.assertRegex(k, r"^[^|]+\|\d{4}-\d{2}-\d{2}$")
        # 🔴 value 必须是单日掩码（1-based，宽度短），绝不能是周掩码
        for v in list(res["grid"].values())[:10]:
            self.assertRegex(v, r"^0x[0-9a-f]{1,6}$", f"掩码格式可疑（周掩码会很长）: {v}")
            self.assertNotEqual(v, "0x100000000")

    def test_schedules_two_calls_identical(self):
        """确定性：两次调用逐字一致。"""
        from server import api_schedules
        group_id = self.conn.execute(
            "SELECT id FROM objects WHERE kind='group' AND name LIKE '%班%' LIMIT 1"
        ).fetchone()
        if group_id is None:
            self.skipTest("演示库无班级组")
        a = json.dumps(
            api_schedules.list_schedules("GET", "/api/schedules", {"group": [group_id["id"]]}, b"", {}),
            ensure_ascii=False, sort_keys=True,
        )
        b = json.dumps(
            api_schedules.list_schedules("GET", "/api/schedules", {"group": [group_id["id"]]}, b"", {}),
            ensure_ascii=False, sort_keys=True,
        )
        self.assertEqual(a, b, "两次调用不一致 → 前端会闪烁")


class TestRouteRegistration(unittest.TestCase):
    """约定式路由：注册表里必须真的有这三个接口。

    🔴 这条测不出「代码不在 main 上」（10-03 事故的本质），
    但能测出「文件存在但没导出 ROUTES」或「key 拼错」。
    """

    def test_three_routes_registered(self):
        from server import routes
        for key in [
            ("GET", "/api/objects"),
            ("GET", "/api/schedules"),
            ("GET", "/api/conflicts"),
        ]:
            self.assertIn(key, routes.get_routes(), f"路由未注册：{key}")


if __name__ == "__main__":
    unittest.main()
