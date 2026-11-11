"""r18onN CSV 批量导入测试：POST /api/import/csv（api_import + b_import_kernels）。

运行：python -m unittest discover -s tests -p "test_import.py" -v

---------------------------------------------------------------------------
卡面 9 条验收 → 本文件测试的对应关系
---------------------------------------------------------------------------
1. 3 行合法导入 + 与手工录入逐字段一致
   → TestHappyPath.test_import_3_rows_visible_in_schedules
     TestHappyPath.test_imported_row_matches_manual_field_by_field
2. dry_run 不写库但能报坏行
   → TestDryRun.*
3. 坏行 400 + 准确行号 + 库内 0 新增（全量回滚）
   → TestBadRows.*（🔴 test_bad_rows_rollback_all_or_nothing 同时是
     破坏法守卫：把回滚改成「跳过坏行」它必须变红）
4. 表头多列（备注）→ 400 回显列名
   → TestBadRows.test_extra_column_rejected
5. GB18030 → 正确解析 + encoding="gb18030"
   → TestEncoding.test_gb18030_raw_body
6. 同名歧义 → 400 列候选，不取第一条
   → TestAmbiguity.test_ambiguous_teacher_lists_candidates
7. teacher 403 / 未登录 401 / 跨组织 403
   → TestPermissions.*
8. 破坏法（改跳过坏行 → 第 3 条变红）
   → 手动注入缺陷后跑 TestBadRows.test_bad_rows_rollback_all_or_nothing，
     交付报告贴「红→还原→绿」两组输出
9. 全量 skipped=0 → 由全量 discover 验收（不在本文件内）

---------------------------------------------------------------------------
🔴 本文件钉死的三个「静默失效」高危点
---------------------------------------------------------------------------
1. 同名取第一条（把课排到同名另一个人头上，不报错）
2. 全量回滚被改成跳过坏行（部分导入，不报错）
3. 未知表头列被静默忽略（「备注」列悄悄丢掉，老师以为导进去了）

每处都写反向断言：破坏后必须红。
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.authz import grant_role  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.occupancy import create_term  # noqa: E402
from scheduler.schedules import NewSchedule, create_periodic  # noqa: E402
from scheduler.slots import bit, setup_default_slots  # noqa: E402
from server import auth, b_import_kernels as B, config  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.http_api import ApiError  # noqa: E402

#: 测试锚定日期：2026-09-15（周二）→ 本周一 = 2026-09-14
TODAY = date(2026, 9, 15)
MONDAY = "2026-09-14"
TERM_START = "2026-09-07"
TERM_END = "2027-01-17"

#: 标准表头（全列）
HEADER = "班级,教师,课程,星期,开始节次,持续节次,教室,生效起始,生效结束,周次"

#: 一行合法样例（映射后：高一1班 / 张老师 / 数学 / 周一第1节 / 每周）
GOOD_ROW = f"高一1班,张老师,数学,周一,1,1,教室A,{MONDAY},2026-09-20,每周"


def _csv(*rows: str, header: str = HEADER) -> str:
    """内联构造 CSV（卡面要求：测试用内联字符串，不引外部样本文件）。"""
    return "\n".join((header, *rows)) + "\n"


class ImportTestCase(unittest.TestCase):
    """公共底座：每个测试一个全新临时库（完全隔离，互不污染）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "import_test.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)

        # 组织 1：根组 + 班级 + 教师 + 管理员 + 撞名对 + 教室
        self.org_id, self.root_id = og.create_organization(self.conn, "导入测试校")
        self.class_id = og.create_object(self.conn, self.org_id, "group", "高一1班")
        og.add_edge(self.conn, self.root_id, self.class_id)
        self.teacher_obj = og.create_object(self.conn, self.org_id, "user", "张老师")
        self.teacher_uid = og.create_user(
            self.conn, self.teacher_obj, "t_imp", "pw123456", role="teacher"
        )
        # 🔴 授 own 也封顶 propose（authz.py:144）→ 必须 403，这是卡面安全核心
        grant_role(self.conn, self.org_id, self.root_id, self.teacher_uid, "own")
        self.admin_obj = og.create_object(self.conn, self.org_id, "user", "管理员甲")
        self.admin_uid = og.create_user(
            self.conn, self.admin_obj, "adm_imp", "pw123456", role="admin"
        )
        # 同名对（都不开登录：歧义检测看的是 objects 行，不是 users 行）
        self.dup1 = og.create_object(self.conn, self.org_id, "user", "王重名")
        self.dup2 = og.create_object(self.conn, self.org_id, "user", "王重名")
        self.room_id = og.create_object(self.conn, self.org_id, "resource", "教室A")

        # 组织 2：跨组织越权测试用
        self.org2_id, self.root2_id = og.create_organization(self.conn, "另一所学校")
        self.admin2_obj = og.create_object(self.conn, self.org2_id, "user", "管理员乙")
        self.admin2_uid = og.create_user(
            self.conn, self.admin2_obj, "adm2_imp", "pw123456", role="admin"
        )

        setup_default_slots(self.conn, self.org_id)
        setup_default_slots(self.conn, self.org2_id)
        create_term(self.conn, self.org_id, "2026-2027 第一学期", TERM_START, TERM_END)
        create_term(self.conn, self.org2_id, "2026-2027 第一学期", TERM_START, TERM_END)
        self.conn.commit()

        # 🔴 接口内部用 config.open_db()（读进程级配置），必须指向本测试库
        self._prev_cfg = config._current
        config.set_current_config(ServerConfig(today=TODAY, db_path=self.db_path))

        self.admin_headers = self._headers(self.admin_uid, "adm_imp", "admin", self.admin_obj)
        self.teacher_headers = self._headers(self.teacher_uid, "t_imp", "teacher", self.teacher_obj)
        self.admin2_headers = self._headers(self.admin2_uid, "adm2_imp", "admin", self.admin2_obj)

    def tearDown(self):
        if self._prev_cfg is not None:
            config.set_current_config(self._prev_cfg)
        self.conn.close()
        self.tmp.cleanup()

    @staticmethod
    def _headers(user_id, login, role, object_id):
        return {
            "authorization": "Bearer "
            + auth.issue_token(user_id, login, role, object_id)
        }

    # -- 请求构造 ----------------------------------------------------------

    def post(self, body: bytes, headers: dict | None = None, query: dict | None = None):
        """直接调 handler（与 test_server_api 同范式；起服链路由契约测试覆盖）。"""
        from server import api_import

        return api_import.import_csv(
            "POST", "/api/import/csv", query or {}, body, headers or {}
        )

    def post_json(self, csv_text: str, dry_run: bool, headers: dict | None = None):
        return self.post(
            json.dumps({"csv": csv_text, "dry_run": dry_run}, ensure_ascii=False).encode("utf-8"),
            headers or self.admin_headers,
        )

    # -- 断言辅助 ----------------------------------------------------------

    def schedule_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM schedules WHERE org_id = ?", (self.org_id,)
        ).fetchone()["c"]

    def schedule_rows(self, title: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM schedules WHERE org_id = ? AND title = ? ORDER BY id",
            (self.org_id, title),
        ).fetchall()


# ---------------------------------------------------------------------------
# 验收 1：合法导入 + 逐字段一致 + GET /api/schedules 可见
# ---------------------------------------------------------------------------


class TestHappyPath(ImportTestCase):
    def test_import_3_rows_visible_in_schedules(self):
        """3 行合法 CSV → imported=3，库内 +3，周视图网格能看到且掩码正确。"""
        before = self.schedule_count()
        res = self.post_json(
            _csv(
                f"高一1班,张老师,数学,周一,1,1,教室A,{MONDAY},2026-09-20,每周",
                f"高一1班,张老师,语文,周二,2,1,教室A,{MONDAY},2026-09-20,每周",
                "高一1班,张老师,英语,周三,3,2,,2026-09-14,2026-09-20,单周",
            ),
            dry_run=False,
        )
        # 200 路径返回 dict（不是元组）
        self.assertIsInstance(res, dict)
        self.assertTrue(res["ok"])
        self.assertFalse(res["dry_run"])
        self.assertEqual(res["imported"], 3)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["encoding"], "utf-8-sig")
        self.assertIn("conflicts", res)
        self.assertEqual(self.schedule_count(), before + 3)

        # GET /api/schedules 周视图可见：周一第 1 节 → 单日掩码 0x2
        from server import api_schedules

        grid_res = api_schedules.list_schedules(
            "GET", "/api/schedules", {"week": [MONDAY]}, b"", self.admin_headers
        )
        key = f"{self.class_id}|{MONDAY}"
        self.assertIn(key, grid_res["grid"], "导入的课没出现在周视图 grid 里")
        self.assertEqual(grid_res["grid"][key], "0x000002", "周一第1节的单日掩码必须是 0x000002")

    def test_imported_row_matches_manual_field_by_field(self):
        """🔴 与手工录入逐字段一致：导入一条 + create_periodic 一条，逐字段比对。

        这是卡面验收 1 的「贴两边对比」：两条路径（CSV 导入 / 手工调领域层）
        产出的存储行必须完全相同，否则导入就成了「另一套写入逻辑」——
        那正是本项目要消灭的双口径。
        """
        csv_row = f"高一1班,张老师,数学,周一,1,1,教室A,{MONDAY},2026-09-20,每周"
        res = self.post_json(_csv(csv_row), dry_run=False)
        self.assertTrue(res["ok"])

        manual_sid = create_periodic(
            self.conn,
            NewSchedule(
                title="数学",
                org_id=self.org_id,
                created_by=self.admin_obj,
                participants={"target": [self.class_id], "teacher": [self.teacher_obj]},
                resources=[self.room_id],
            ),
            slot_bitmap=bit(1, 1),
            week_parity=0,
            valid_from=MONDAY,
            valid_to="2026-09-20",
        )

        imported, manual = self.schedule_rows("数学")
        # schedules 主表
        for field in ("org_id", "kind", "title", "created_by"):
            self.assertEqual(imported[field], manual[field], f"字段 {field} 不一致")
        # schedule_periodic 扩展表（掩码 / 单双周 / 区间）
        p_i = self.conn.execute(
            "SELECT * FROM schedule_periodic WHERE schedule_id = ?", (imported["id"],)
        ).fetchone()
        p_m = self.conn.execute(
            "SELECT * FROM schedule_periodic WHERE schedule_id = ?", (manual_sid,)
        ).fetchone()
        for field in ("week_parity", "slot_bitmap", "valid_from", "valid_to"):
            self.assertEqual(p_i[field], p_m[field], f"周期字段 {field} 不一致")
        self.assertEqual(p_i["slot_bitmap"], format(bit(1, 1), "x"))
        # 参与者 / 资源
        parts = lambda sid: set(
            (r["object_id"], r["role"])
            for r in self.conn.execute(
                "SELECT object_id, role FROM schedule_participants WHERE schedule_id = ?", (sid,)
            )
        )
        self.assertEqual(parts(imported["id"]), parts(manual_sid))
        res_i = set(
            r["resource_id"]
            for r in self.conn.execute(
                "SELECT resource_id FROM schedule_resources WHERE schedule_id = ?", (imported["id"],)
            )
        )
        self.assertEqual(res_i, {self.room_id})

    def test_defaults_applied(self):
        """可选列全缺省：持续=1 / 教室空 / 生效起始=本周一 / 结束=学期末 / 每周。"""
        res = self.post_json(
            _csv("高一1班,张老师,体育,周四,5", header="班级,教师,课程,星期,开始节次"),
            dry_run=False,
        )
        self.assertTrue(res["ok"])
        row = self.schedule_rows("体育")[0]
        per = self.conn.execute(
            "SELECT * FROM schedule_periodic WHERE schedule_id = ?", (row["id"],)
        ).fetchone()
        self.assertEqual(per["slot_bitmap"], format(bit(4, 5), "x"))
        self.assertEqual(per["week_parity"], 0)
        self.assertEqual(per["valid_from"], MONDAY)  # 缺省 = 本周一（today 周二所在周）
        self.assertEqual(per["valid_to"], TERM_END)  # 缺省 = 学期末
        n_res = self.conn.execute(
            "SELECT COUNT(*) AS c FROM schedule_resources WHERE schedule_id = ?", (row["id"],)
        ).fetchone()["c"]
        self.assertEqual(n_res, 0)

    def test_dow_numeric_and_parity_variants(self):
        """星期两种写法（3 / 周三）等价；单周→1、双周→2。"""
        res = self.post_json(
            _csv(
                f"高一1班,张老师,音乐,3,1,,,{MONDAY},2026-09-20,单周",
                f"高一1班,张老师,美术,周三,1,,,{MONDAY},2026-09-20,双周",
                f"高一1班,张老师,自习,3,1,,,{MONDAY},2026-09-20,每周",
            ),
            dry_run=False,
        )
        self.assertTrue(res["ok"])
        music = self.conn.execute(
            "SELECT p.week_parity AS wp, p.slot_bitmap AS sb FROM schedules s"
            " JOIN schedule_periodic p ON p.schedule_id = s.id"
            " WHERE s.org_id = ? AND s.title = ?",
            (self.org_id, "音乐"),
        ).fetchone()
        art = self.conn.execute(
            "SELECT p.week_parity AS wp, p.slot_bitmap AS sb FROM schedules s"
            " JOIN schedule_periodic p ON p.schedule_id = s.id"
            " WHERE s.org_id = ? AND s.title = ?",
            (self.org_id, "美术"),
        ).fetchone()
        self.assertEqual(music["wp"], 1)
        self.assertEqual(art["wp"], 2)
        # 数字「3」与「周三」落同一个位
        self.assertEqual(music["sb"], format(bit(3, 1), "x"))
        self.assertEqual(art["sb"], format(bit(3, 1), "x"))

    def test_conflicts_counted_after_import_and_in_preview(self):
        """冲突统计：真导入后 count>0；dry_run 预演（内存副本）也必须 >0。"""
        first = self.post_json(_csv(GOOD_ROW), dry_run=False)
        self.assertTrue(first["ok"])
        # 与 GOOD_ROW 同班同师同节次（不同课程）→ 撞车
        clash = f"高一1班,张老师,物理,周一,1,1,,{MONDAY},2026-09-20,每周"
        preview = self.post_json(_csv(clash), dry_run=True)
        self.assertTrue(preview["ok"])
        self.assertGreater(preview["conflicts"]["count"], 0, "dry_run 预演没算出冲突")
        self.assertEqual(self.schedule_rows("物理"), [], "dry_run 绝不能写库")
        real = self.post_json(_csv(clash), dry_run=False)
        self.assertTrue(real["ok"])
        self.assertGreater(real["conflicts"]["count"], 0)
        self.assertEqual(
            real["conflicts"]["count"], preview["conflicts"]["count"],
            "预演与真导入的冲突数必须一致（同一个内存副本口径）",
        )

    def test_conflict_window_truncated_with_advisory(self):
        """区间 > 62 天 → 截断 + advisory（防整学期展开拖慢）。"""
        res = self.post_json(
            _csv(f"高一1班,张老师,数学,周一,1,1,,2026-09-14,2027-01-10,每周"),
            dry_run=False,
        )
        self.assertTrue(res["ok"])
        conf = res["conflicts"]
        self.assertEqual(conf["from"], "2026-09-14")
        self.assertEqual(conf["to"], "2026-11-15")  # 09-14 + 62 天
        self.assertIn("advisory", conf)
        self.assertIn("62", conf["advisory"])

    def test_header_only_csv_imports_zero(self):
        """只有表头没有数据行 → 200 / imported=0（空表不算错）。"""
        res = self.post_json(_csv(), dry_run=False)
        self.assertIsInstance(res, dict)
        self.assertTrue(res["ok"])
        self.assertEqual(res["imported"], 0)
        self.assertEqual(res["conflicts"], {"count": 0, "from": None, "to": None})


# ---------------------------------------------------------------------------
# 验收 2：dry_run 不写库但能报坏行
# ---------------------------------------------------------------------------


class TestDryRun(ImportTestCase):
    def test_dry_run_good_csv_writes_nothing(self):
        before = self.schedule_count()
        res = self.post_json(_csv(GOOD_ROW), dry_run=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["imported"], 1)  # 计划导入数
        self.assertEqual(self.schedule_count(), before, "dry_run 写了库 = 事故")

    def test_dry_run_reports_bad_rows(self):
        before = self.schedule_count()
        res = self.post_json(
            _csv(
                GOOD_ROW,
                "不存在的班,张老师,数学,周一,1",
            ),
            dry_run=True,
        )
        status, payload = res
        self.assertEqual(status, 400)
        lines = [e["line"] for e in payload["errors"]]
        self.assertIn(3, lines, "dry_run 必须能报出坏行行号")
        self.assertEqual(self.schedule_count(), before)

    def test_dry_run_default_when_omitted(self):
        """JSON 里不给 dry_run → 缺省 true（安全默认：不显式要求就不写库）。"""
        body = json.dumps({"csv": _csv(GOOD_ROW)}, ensure_ascii=False).encode("utf-8")
        before = self.schedule_count()
        res = self.post(body, self.admin_headers)
        self.assertIsInstance(res, dict)
        self.assertTrue(res["dry_run"])
        self.assertEqual(self.schedule_count(), before)


# ---------------------------------------------------------------------------
# 验收 3 + 4 + 8：坏行 / 多列 / 全量回滚（破坏法守卫）
# ---------------------------------------------------------------------------


class TestBadRows(ImportTestCase):
    def test_bad_rows_rollback_all_or_nothing(self):
        """🔴 全量回滚守卫（验收 3 + 破坏法 8 的自动化断言）。

        2 行好 + 4 行坏（班级不存在 / 星期=8 / 节次=17 / 结束早于起始）→
        400 + 全部行号 + 库内 0 新增。
        🔴 把「任一行错全部回滚」改成「跳过坏行继续」：本用例会在
        status==400 断言处变红（改后变 200）——没红过等于没有测试。
        """
        before = self.schedule_count()
        res = self.post_json(
            _csv(
                GOOD_ROW,                                        # 行2 好
                "不存在的班,张老师,数学,周一,1",                  # 行3 坏：班级不存在
                "高一1班,张老师,数学,8,1",                        # 行4 坏：星期=8
                "高一1班,张老师,数学,周一,17",                    # 行5 坏：节次>16
                f"高一1班,张老师,数学,周一,1,,,{MONDAY},2026-09-13,每周",  # 行6 坏：结束早于起始
                f"高一1班,张老师,物理,周二,2,,,{MONDAY},2026-09-20,每周",  # 行7 好
            ),
            dry_run=False,
        )
        status, payload = res
        self.assertEqual(status, 400, "任一行错必须是 400，部分导入不可接受")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["imported"], 0)
        # 🔴 全部坏行行号都在，一条也不放过（含行号排序）
        lines = [e["line"] for e in payload["errors"]]
        self.assertEqual(lines, [3, 4, 5, 6])
        # 🔴 库内 0 新增（行2、行7 两行好的也必须没写进去）
        self.assertEqual(self.schedule_count(), before, "部分行被写入 = 回滚失效")
        self.assertEqual(self.schedule_rows("数学"), [])
        self.assertEqual(self.schedule_rows("物理"), [])

    def test_every_bad_line_reported_not_just_first(self):
        """第 3 行坏不能遮住第 7 行的错（跨行全收）。"""
        res = self.post_json(
            _csv(
                GOOD_ROW,
                "不存在的班,张老师,数学,周一,1",
                f"高一1班,张老师,语文,周二,2,,,{MONDAY},2026-09-20,每周",
                "高一1班,不存在老师,英语,周三,3",
            ),
            dry_run=False,
        )
        status, payload = res
        self.assertEqual(status, 400)
        lines = [e["line"] for e in payload["errors"]]
        self.assertEqual(lines, [3, 5], "两个坏行的行号都必须出现")

    def test_extra_column_rejected(self):
        """🔴 表头多一列「备注」→ 400 回显列名（不静默忽略）。"""
        before = self.schedule_count()
        res = self.post_json(
            _csv(
                "高一1班,张老师,数学,周一,1,备注内容",
                header="班级,教师,课程,星期,开始节次,备注",
            ),
            dry_run=False,
        )
        status, payload = res
        self.assertEqual(status, 400)
        self.assertEqual(payload["errors"][0]["line"], 1, "表头错误行号=1")
        self.assertIn("备注", payload["errors"][0]["message"], "必须回显未知列名")
        self.assertEqual(self.schedule_count(), before)

    def test_missing_required_column(self):
        res = self.post_json(
            _csv("高一1班,数学,周一,1", header="班级,课程,星期,开始节次"),
            dry_run=False,
        )
        status, payload = res
        self.assertEqual(status, 400)
        self.assertIn("教师", payload["errors"][0]["message"])

    def test_duplicate_rows_rejected(self):
        """重复行（映射后全同）→ 400 并给重复行号对。"""
        res = self.post_json(
            _csv(GOOD_ROW, GOOD_ROW, f"高一1班,张老师,物理,周二,2,,,{MONDAY},2026-09-20,每周"),
            dry_run=False,
        )
        status, payload = res
        self.assertEqual(status, 400)
        dup_errors = [e for e in payload["errors"] if "重复" in e["message"]]
        self.assertEqual(len(dup_errors), 1)
        self.assertEqual(dup_errors[0]["line"], 3)
        self.assertIn("第 2 行", dup_errors[0]["message"])

    def test_row_limit_2000(self):
        rows = [f"高一1班,张老师,课程{i},周一,1" for i in range(2001)]
        res = self.post_json(_csv(*rows), dry_run=True)
        status, payload = res
        self.assertEqual(status, 400)
        self.assertTrue(any("2000" in e["message"] for e in payload["errors"]))

    def test_empty_csv_and_structural_400(self):
        # 空请求体
        with self.assertRaises(ApiError) as ctx:
            self.post(b"", self.admin_headers)
        self.assertEqual(ctx.exception.status, 400)
        # JSON 缺 csv
        with self.assertRaises(ApiError) as ctx:
            self.post(json.dumps({"dry_run": True}).encode(), self.admin_headers)
        self.assertEqual(ctx.exception.status, 400)
        # csv 空串
        with self.assertRaises(ApiError) as ctx:
            self.post(json.dumps({"csv": ""}).encode(), self.admin_headers)
        self.assertEqual(ctx.exception.status, 400)
        # dry_run 非布尔
        with self.assertRaises(ApiError) as ctx:
            self.post(json.dumps({"csv": _csv(GOOD_ROW), "dry_run": "no"}).encode(), self.admin_headers)
        self.assertEqual(ctx.exception.status, 400)
        # 只有空行没有表头
        res = self.post_json("\n\n  \n", dry_run=True)
        status, payload = res
        self.assertEqual(status, 400)
        self.assertEqual(payload["errors"][0]["line"], 1)


# ---------------------------------------------------------------------------
# 验收 5：编码（utf-8-sig / gb18030）
# ---------------------------------------------------------------------------


class TestEncoding(ImportTestCase):
    def test_utf8_sig_with_bom(self):
        """带 BOM 的 UTF-8 请求体 → 正常解析，encoding="utf-8-sig"。"""
        body = b"\xef\xbb\xbf" + json.dumps(
            {"csv": _csv(GOOD_ROW), "dry_run": False}, ensure_ascii=False
        ).encode("utf-8")
        res = self.post(body, self.admin_headers)
        self.assertIsInstance(res, dict)
        self.assertTrue(res["ok"])
        self.assertEqual(res["encoding"], "utf-8-sig")
        self.assertEqual(len(self.schedule_rows("数学")), 1)

    def test_gb18030_raw_body(self):
        """🔴 GB18030 原生 CSV 字节（Excel 中文默认编码）→ 正确解析 + 如实回 encoding。

        GB18030 字节不可能经 JSON 传（JSON 是 UTF-8，先在 json 层炸掉），
        所以走「请求体即 CSV 原文」的兜底形态；dry_run 从 query 读。
        """
        csv_text = "\r\n".join(
            (
                HEADER,
                f"高一1班,张老师,数学,周一,1,1,教室A,{MONDAY},2026-09-20,每周",
            )
        )
        body = csv_text.encode("gb18030")
        # 先证明这字节流确实不是合法 UTF-8（否则测了个寂寞）
        with self.assertRaises(UnicodeDecodeError):
            body.decode("utf-8")

        res = self.post(body, self.admin_headers, query={"dry_run": ["false"]})
        self.assertIsInstance(res, dict)
        self.assertTrue(res["ok"])
        self.assertFalse(res["dry_run"])
        self.assertEqual(res["encoding"], "gb18030")
        self.assertEqual(res["imported"], 1)
        row = self.schedule_rows("数学")
        self.assertEqual(len(row), 1, "gb18030 解析出的中文必须无损落库")
        self.assertEqual(row[0]["title"], "数学")

    def test_gb18030_raw_body_default_is_dry_run(self):
        """原生形态不显式给 dry_run → 只预检不写库（写库必须显式要求）。"""
        body = _csv(GOOD_ROW).encode("gb18030")
        before = self.schedule_count()
        res = self.post(body, self.admin_headers)
        self.assertIsInstance(res, dict)
        self.assertTrue(res["dry_run"])
        self.assertEqual(self.schedule_count(), before)

    def test_undecodable_body_400(self):
        with self.assertRaises(ApiError) as ctx:
            self.post(b"\xff\xfe\x00\x00\xfa\xfb", self.admin_headers)
        self.assertEqual(ctx.exception.status, 400)


# ---------------------------------------------------------------------------
# 验收 6：同名歧义
# ---------------------------------------------------------------------------


class TestAmbiguity(ImportTestCase):
    def test_ambiguous_teacher_lists_candidates(self):
        """🔴 两个同名 user → 400 列出全部候选（name+id），绝不取第一条。"""
        before = self.schedule_count()
        res = self.post_json(_csv("高一1班,王重名,数学,周一,1"), dry_run=False)
        status, payload = res
        self.assertEqual(status, 400)
        msg = payload["errors"][0]["message"]
        self.assertIn("王重名", msg)
        # 两个候选 id 都必须在消息里（能定位到具体对象）
        self.assertIn(self.dup1, msg)
        self.assertIn(self.dup2, msg)
        # 🔴 反向断言：绝不能「取第一条」悄悄导入成功
        self.assertEqual(self.schedule_count(), before)
        self.assertEqual(self.schedule_rows("数学"), [])

    def test_ambiguous_class_kind_mismatch(self):
        """班级列填了教师名（kind=user 不是 group）→ 400，kind 写进错误。"""
        res = self.post_json(_csv("张老师,张老师,数学,周一,1"), dry_run=False)
        status, payload = res
        self.assertEqual(status, 400)
        self.assertIn("kind=group", payload["errors"][0]["message"])

    def test_room_is_resource_not_room_kind(self):
        """教室列填了班级名（kind=group 不是 resource）→ 400。"""
        res = self.post_json(
            _csv(f"高一1班,张老师,数学,周一,1,1,高一1班,{MONDAY},2026-09-20,每周"),
            dry_run=False,
        )
        status, payload = res
        self.assertEqual(status, 400)
        self.assertIn("kind=resource", payload["errors"][0]["message"])

    def test_cross_org_name_not_resolvable(self):
        """别组织的同名对象既不是候选也不能被解析（跨组织不串数据）。"""
        # 在组织 2 建一个同名班级
        other_cls = og.create_object(self.conn, self.org2_id, "group", "高一1班")
        self.conn.commit()
        res = self.post_json(_csv(GOOD_ROW), dry_run=False)
        # 本组织同名班级只有一个，导入照常成功——跨组织的同名者不参与解析
        self.assertIsInstance(res, dict)
        self.assertTrue(res["ok"])
        # 但如果本组织的唯一命中被删掉，只剩组织 2 的同名者 → 必须找不到
        self.conn.execute("DELETE FROM objects WHERE id = ?", (self.class_id,))
        self.conn.commit()
        res2 = self.post_json(_csv(GOOD_ROW), dry_run=False)
        status, payload = res2
        self.assertEqual(status, 400)
        self.assertNotIn(other_cls, payload["errors"][0]["message"])


# ---------------------------------------------------------------------------
# 验收 7：权限三态
# ---------------------------------------------------------------------------


class TestPermissions(ImportTestCase):
    def test_unauthenticated_401(self):
        res = self.post(json.dumps({"csv": _csv(GOOD_ROW)}).encode(), headers={})
        status, payload = res
        self.assertEqual(status, 401)
        self.assertIn("error", payload)

    def test_teacher_403_even_granted_own(self):
        """🔴 teacher 授 own 也被封顶 propose（authz.py:144）→ 403。

        卡面安全核心：导入是批量直接写库，允许 propose 档走这条路
        等于给审批流开后门。
        """
        with self.assertRaises(ApiError) as ctx:
            self.post_json(_csv(GOOD_ROW), dry_run=False, headers=self.teacher_headers)
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("own", ctx.exception.message)

    def test_cross_org_403(self):
        """组织 2 的管理员请求组织 1 的组 → 403。"""
        with self.assertRaises(ApiError) as ctx:
            self.post(
                json.dumps({"csv": _csv(GOOD_ROW), "dry_run": False}).encode(),
                headers=self.admin2_headers,
                query={"group": [self.class_id]},
            )
        self.assertEqual(ctx.exception.status, 403)

    def test_admin_of_other_org_default_group_403(self):
        """组织 2 管理员不带 group（默认组在组织 2）→ 解析到组织 2，组织 1 无写入。"""
        # 组织 2 有自己的组与学期，导入成功但落在组织 2（隔离验证）
        cls2 = og.create_object(self.conn, self.org2_id, "group", "高二1班")
        og.add_edge(self.conn, self.root2_id, cls2)
        teacher2 = og.create_object(self.conn, self.org2_id, "user", "李老师")
        self.conn.commit()
        csv2 = _csv(f"高二1班,李老师,化学,周五,1")
        res = self.post(
            json.dumps({"csv": csv2, "dry_run": False}, ensure_ascii=False).encode(),
            headers=self.admin2_headers,
        )
        self.assertIsInstance(res, dict)
        self.assertTrue(res["ok"])
        rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM schedules WHERE org_id = ?", (self.org2_id,)
        ).fetchone()["c"]
        self.assertEqual(rows, 1, "应写入组织 2")
        self.assertEqual(self.schedule_count(), 0, "组织 1 必须零写入")


if __name__ == "__main__":
    unittest.main()
