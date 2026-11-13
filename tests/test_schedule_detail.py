"""GET /api/schedules/detail 的验收测试（卡 rYoMa7 / docs/07 §4.1）。

设计意图（为什么这些用例长这样）：
    这个接口存在的唯一理由是**把「一格到底是谁占的」讲清楚**。它最容易犯的错
    不是算错节次，而是**用空结果或单条结果冒充正确答案**：
      · 一格两条日程只返回一条 → 前端双击取消了一条，另一条还在撞，
        老师以为调好了（假阴性，本项目最痛形态）；
      · once 活动不带 kind → 前端挂上「取消」按钮，
        `POST /api/exceptions` 直接 400，点了就炸；
      · object_id 不属于本组织却返回 200 + 空 cells →
        前端当成「这天没课」，双击就去排课。
    所以本文件的断言重心是「不许静默」，而不是「字段齐不齐」。

    三个「必须真跑」的落点：
      ① `periods` 必须来自 `effective_slot_config`：本文件造一个**换作息**
         （special）的日期，该方案没有第 7 节，并让一条周期日程占第 7 节。
         实现若直接查 `period_slots`，periods 会冒出 idx 7、cells 也会出现
         一格「排不进去的课」→ 本文件立刻变红。
      ② `mask` 必须是**单日掩码**（0x000002），不是 `entry.mask` 的周位序
         （0x100000000）。两套位序混用会让页面整体错位且不报错。
      ③ 只读：调用前后关键表逐行一致。

运行：python -m unittest tests.test_schedule_detail -v
只用 Python 标准库。
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect  # noqa: E402
from scheduler.occupancy import effective_slot_config, load_exceptions  # noqa: E402
from scheduler.slots import bit, decode_mask, load_slot_config, mask_to_hex  # noqa: E402
from server import api_schedules, auth, config  # noqa: E402
from server.config import ServerConfig, ensure_database  # noqa: E402
from server.http_api import ApiError  # noqa: E402

#: seed_demo 的 term 锚定 2026-09-07（周一）；周三 2026-09-09 是「冲突演示」那天。
TODAY = date(2026, 9, 8)
WED = "2026-09-09"
THU = "2026-09-10"

#: 只读断言要逐表比行数（含 B9/计划外键表）。
_TABLES = (
    "schedules",
    "schedule_periodic",
    "schedule_once",
    "schedule_participants",
    "schedule_resources",
    "schedule_exceptions",
    "calendar_exceptions",
    "period_slots",
    "period_sets",
    "set_slots",
    "objects",
)


def _issue_token(conn, login: str) -> str:
    row = conn.execute(
        "SELECT id, login, role, object_id FROM users WHERE login = ?", (login,)
    ).fetchone()
    assert row is not None, f"账号 {login} 不存在"
    return "Bearer " + auth.issue_token(
        row["id"], row["login"], row["role"], row["object_id"]
    )


class TestScheduleDetail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="sched-detail-test-")
        cls.db_path = os.path.join(cls.tmpdir, "demo.db")
        cfg = ServerConfig(db_path=cls.db_path, today=TODAY)
        ensure_database(cfg)
        cls._prev_cfg = config._current
        config.set_current_config(cfg)

        conn = connect(cls.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM objects WHERE kind='group' AND name='高一(1)班'"
            ).fetchone()
            cls.class_id = row["id"]
            cls.org_id = conn.execute(
                "SELECT org_id FROM objects WHERE id = ?", (cls.class_id,)
            ).fetchone()["org_id"]
            cls.admin_token = _issue_token(conn, "admin")
            cls.teacher_token = _issue_token(conn, "t01")
            # 教师被授权的组（seeds 按任教关系精确授权，不能假设就是高一(1)班）
            trow = conn.execute(
                "SELECT o.id FROM group_roles gr"
                " JOIN users u ON u.id = gr.user_id"
                " JOIN objects o ON o.id = gr.group_id"
                " WHERE u.login = 't01' ORDER BY o.name, o.id LIMIT 1"
            ).fetchone()
            assert trow is not None, "t01 没有任何 group_roles 授权（P0-1 回归）"
            cls.teacher_class_id = trow["id"]

            # 🔴 日程 id 由 new_id() 随机生成（新建库每次不同），所以按**标题**定位。
            # 卡面点名的那两个 id 属于 data/demo.db（种子库 id 固定），
            # 交付报告里对 demo.db 副本单独贴真实输出。
            cls.physics_id = conn.execute(
                "SELECT id FROM schedules WHERE title = '（冲突演示）高一(1)班 物理'"
            ).fetchone()["id"]
            cls.event_id = conn.execute(
                "SELECT id FROM schedules WHERE title = '全年级大会'"
            ).fetchone()["id"]
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        if cls._prev_cfg is not None:
            config.set_current_config(cls._prev_cfg)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def _call(self, query, headers=None, path="/api/schedules/detail"):
        return api_schedules.schedule_detail(
            "GET", path, query, b"", headers or {}, None
        )

    def _q(self, object_id=None, d=WED, **extra):
        q = {}
        if object_id is not None:
            q["object_id"] = [object_id]
        if d is not None:
            q["date"] = [d]
        for k, v in extra.items():
            q[k] = [v]
        return q

    def _conn(self):
        return connect(self.db_path)

    # -- 1. 一格两条日程并存（卡面必验 1、2）--------------------------------
    def test_conflicting_cell_returns_both_schedules(self):
        """周三第 1 节：物理课与全年级大会必须**同时**出现，且各带 kind。

        这是 2026-09-09 的既有冲突（seeds 故意造的）。断言用 id + 标题 + kind
        三重绑定，避免「换了个同名的另一条日程也算过」。
        """
        result = self._call(self._q(self.class_id), {"authorization": self.admin_token})
        self.assertEqual(result["object_id"], self.class_id)
        self.assertEqual(result["date"], WED)
        self.assertEqual(result["day_of_week"], 3)

        cell1 = self._cell(result, 1)
        schedules = cell1["schedules"]
        self.assertIsInstance(schedules, list, "cells[].schedules 必须是数组")
        by_id = {s["id"]: s for s in schedules}

        self.assertIn(self.physics_id, by_id, "高一(1)班 物理 必须在这一格")
        self.assertIn(self.event_id, by_id, "全年级大会 必须与物理课同格并存")
        self.assertEqual(by_id[self.physics_id]["kind"], "periodic")
        # 卡面必验 2：全年级大会是 once（前端靠它决定双击能不能挂例外）
        self.assertEqual(by_id[self.event_id]["kind"], "once")
        self.assertIn("物理", by_id[self.physics_id]["title"])
        self.assertEqual(by_id[self.event_id]["title"], "全年级大会")

        # 每条都要有 role（前端判「我是被占的一方还是发起方」）
        for s in schedules:
            self.assertIn(s["role"], ("target", "teacher", "participant", "resource"))

        # 每条都必须是 dict 且字段齐全（少字段会让前端 undefined 静默渲染）
        for s in schedules:
            self.assertEqual(set(s), {"id", "title", "kind", "role"})

    def test_once_activity_covers_its_own_periods(self):
        """全年级大会占 1-2 节：第 2 节只有它一条（证明拆位正确，不是整格复制）。"""
        result = self._call(self._q(self.class_id), {"authorization": self.admin_token})
        cell2 = self._cell(result, 2)
        self.assertEqual([s["id"] for s in cell2["schedules"]], [self.event_id])
        self.assertEqual(cell2["schedules"][0]["kind"], "once")

    # -- 2. mask 口径（单日掩码，不是周位序）--------------------------------
    def test_mask_is_single_day_not_week_bitmap(self):
        """🔴 mask 必须是 1-based 单日掩码（第 N 节 → 位 N），与 grid 同口径。

        `entry.mask` 是周位序：周三第 1 节 = 0x100000000。直接透出会让前端
        与 grid 的 "0x000002" 对不上，页面整体错位且不报错。
        """
        result = self._call(self._q(self.class_id), {"authorization": self.admin_token})
        for cell in result["cells"]:
            expected = "0x" + format(1 << cell["idx"], "06x")
            self.assertEqual(cell["mask"], expected)
            self.assertNotEqual(cell["mask"], "0x100000000")
        self.assertEqual(self._cell(result, 1)["mask"], "0x000002")
        self.assertEqual(self._cell(result, 2)["mask"], "0x000004")

    # -- 3. periods 与 build_occupancy 同一份 config ------------------------
    def test_periods_match_effective_slot_config_normal_day(self):
        """普通日：periods 必须与 effective_slot_config 逐项一致（不是硬编码 1..10）。"""
        conn = self._conn()
        try:
            slot_cfg = load_slot_config(conn, self.org_id)
            exc = load_exceptions(conn, self.org_id)
            day_cfg = effective_slot_config(exc, slot_cfg, WED)
            expected = day_cfg.enabled_slots(3)
        finally:
            conn.close()

        result = self._call(self._q(self.class_id), {"authorization": self.admin_token})
        got = [(3, p["idx"]) for p in result["periods"]]
        self.assertEqual(got, expected)
        for p in result["periods"]:
            self.assertTrue(p["enabled"])
            self.assertTrue(p["name"])
            self.assertRegex(p["start"], r"^\d{2}:\d{2}$")
            self.assertRegex(p["end"], r"^\d{2}:\d{2}$")

        # cells 的 idx 必须落在 periods 之内（否则前端拿到一个画不出来的格子）
        allowed = {p["idx"] for p in result["periods"]}
        for cell in result["cells"]:
            self.assertIn(cell["idx"], allowed)

    def test_periods_carry_server_side_new_mask(self):
        """🔴 破坏法落点：每个节次必须自带 `new_mask`，且等于 1<<idx 的单日掩码。

        为什么必须有这个字段：`POST /api/exceptions` 的 `new_mask` 要求「第 N 节 → 位 N」，
        而**空白节次不在 cells 里**，前端没有别的办法拿到目标掩码 —— 没有这个字段，
        前端只能自己 `1 << idx` 算，违反 AGENTS §3.4「前端不做位运算，mask 由服务端给」。

        断言两件事（少一件都测不出错）：
          ① 格式与 cells[].mask 完全一致（`0x%06x`，可直接当 new_mask 传）；
          ② 数值恒等于 `1 << idx` —— 若有人误用周位序
             `(dow-1)*16+(idx-1)`，值会变成 0x100000000 那一路，本用例当场红。

        破坏法：把 `api_schedules._periods()` 里的 `"new_mask"` 一行删掉 → 本用例必红。
        """
        result = self._call(self._q(self.class_id), {"authorization": self.admin_token})
        periods = result["periods"]
        self.assertTrue(periods, "periods 不应为空")
        for p in periods:
            self.assertIn("new_mask", p, f"第 {p['idx']} 节缺 new_mask")
            mask = p["new_mask"]
            self.assertRegex(mask, r"^0x[0-9a-f]{6}$", f"格式必须与 cells[].mask 一致：{mask}")
            self.assertEqual(
                int(mask, 16),
                1 << p["idx"],
                f"第 {p['idx']} 节的 new_mask 必须是单日掩码 1<<{p['idx']}（不是周位序）",
            )

        # 与 cells[].mask 同一套口径：同一节的 mask 文本必须与 periods 里的一致。
        by_idx = {p["idx"]: p["new_mask"] for p in periods}
        for cell in result["cells"]:
            self.assertEqual(cell["mask"], by_idx[cell["idx"]])

    def test_periods_and_cells_agree_on_special_day(self):
        """🔴 破坏法落点：换作息那天，periods 与 cells 必须同时不出现第 7 节。

        造法：给周四绑一套只有 1..6 节的作息方案，并让一条周期日程占周四第 7 节。
        · 实现若偷懒直接查 `period_slots` → periods 会冒出 idx 7（红）；
        · 实现若给 periods 过了 effective_slot_config、但占用走了别的 config
          或不过滤 → cells 会出现 idx 7（红）。
        两者必须同源：这是本卡「periods 与 build_occupancy 用同一份 config」的
        唯一硬证据。
        """
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO period_sets(id, org_id, name, note) VALUES (?,?,?,NULL)",
                ("set_short", self.org_id, "短日作息"),
            )
            for idx in range(1, 7):
                conn.execute(
                    "INSERT INTO set_slots(id, set_id, day_of_week, idx, start_time,"
                    " end_time, enabled) VALUES (?,?,?,?,?,?,1)",
                    (f"set_short_{idx}", "set_short", 4, idx, "08:00", "08:45"),
                )
            conn.execute(
                "INSERT INTO schedule_exceptions(id, org_id, scope, target_id, date,"
                " mode, set_id) VALUES (?,?,?,NULL,?,?,?)",
                ("se_short", self.org_id, "day", THU, "special", "set_short"),
            )
            # 周四第 7 节：短日作息里不存在这一节
            conn.execute(
                "INSERT INTO schedules(id, org_id, kind, title) VALUES (?,?,?,?)",
                ("sch_p7", self.org_id, "periodic", "周四第7节 测试日程"),
            )
            conn.execute(
                "INSERT INTO schedule_periodic(schedule_id, week_parity, slot_bitmap,"
                " valid_from, valid_to) VALUES (?,?,?,?,?)",
                ("sch_p7", 0, mask_to_hex(bit(4, 7)), "2026-09-07", "2026-12-31"),
            )
            conn.execute(
                "INSERT INTO schedule_participants(schedule_id, object_id, role)"
                " VALUES (?,?,?)",
                ("sch_p7", self.class_id, "target"),
            )
            conn.commit()
        finally:
            conn.close()

        result = self._call(
            self._q(self.class_id, d=THU), {"authorization": self.admin_token}
        )
        idxs = [p["idx"] for p in result["periods"]]
        self.assertEqual(idxs, [1, 2, 3, 4, 5, 6], "periods 必须来自换过的作息方案")
        self.assertNotIn(7, [c["idx"] for c in result["cells"]], "不存在的第 7 节不许出现在 cells")

        # 与领域层同源核对：同一 (dow=4) 的 enabled_slots 就是这 6 节
        conn = self._conn()
        try:
            day_cfg = effective_slot_config(
                load_exceptions(conn, self.org_id),
                load_slot_config(conn, self.org_id),
                THU,
            )
            self.assertEqual([i for _d, i in day_cfg.enabled_slots(4)], idxs)
        finally:
            conn.close()

    # -- 4. 空白格（卡面必验 5）--------------------------------------------
    def test_blank_period_absent_from_cells(self):
        """周三第 3 节没人占 → 200，且 cells 里**没有** idx 3（不是 404、不是报错）。"""
        result = self._call(self._q(self.class_id), {"authorization": self.admin_token})
        self.assertEqual([c["idx"] for c in result["cells"]], [1, 2])
        self.assertNotIn(3, [c["idx"] for c in result["cells"]])
        # 但第 3 节仍然在 periods 里（「有这一节、只是空着」≠「这天只有两节」）
        self.assertIn(3, [p["idx"] for p in result["periods"]])

    # -- 5. 状态码四条（卡面必验 4）----------------------------------------
    def test_unauthenticated_401(self):
        status, body = self._call(self._q(self.class_id), headers={})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_cross_org_group_403(self):
        """教师传别组织的 group → 403（越权，不是 200 空结果）。"""
        conn = self._conn()
        try:
            org_b, _root = og.create_organization(conn, "测试校B-detail")
            group_b = og.create_object(conn, org_b, "group", "B班")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ApiError) as ctx:
            self._call(
                self._q(self.class_id, group=group_b),
                {"authorization": self.teacher_token},
            )
        self.assertEqual(ctx.exception.status, 403)

    def test_object_of_another_org_404(self):
        """对象属于别的组织 → 404（⛔ 不是 200 + 空 cells）。"""
        conn = self._conn()
        try:
            org_b, _root = og.create_organization(conn, "测试校C-detail")
            foreign_id = og.create_object(conn, org_b, "group", "C班")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ApiError) as ctx:
            self._call(
                {"object_id": [foreign_id], "date": [WED]},
                {"authorization": self.admin_token},
            )
        self.assertEqual(ctx.exception.status, 404)

    def test_unknown_object_404(self):
        with self.assertRaises(ApiError) as ctx:
            self._call(
                {"object_id": ["obj_does_not_exist"], "date": [WED]},
                {"authorization": self.admin_token},
            )
        self.assertEqual(ctx.exception.status, 404)

    def test_missing_and_bad_params_400(self):
        cases = [
            {"date": [WED]},                                  # 缺 object_id
            {"object_id": [self.class_id]},                    # 缺 date
            {"object_id": [self.class_id], "date": ["abc"]},   # 格式错
            {"object_id": [self.class_id], "date": ["20260909"]},  # 无连字符（宽松解析会放过）
            {"object_id": [self.class_id], "date": ["2026-02-30"]},  # 日期不存在
            {"object_id": [""], "date": [WED]},                # 空串
        ]
        for q in cases:
            with self.subTest(query=q):
                with self.assertRaises(ApiError) as ctx:
                    self._call(q, {"authorization": self.admin_token})
                self.assertEqual(ctx.exception.status, 400)

    # -- 6. 非管理员走一遍（AGENTS §4）-------------------------------------
    def test_teacher_can_read_own_group(self):
        """admin → 全组织短路会掩盖教师全空，必须用非管理员账号实测。"""
        result = self._call(
            self._q(self.teacher_class_id), {"authorization": self.teacher_token}
        )
        self.assertEqual(result["object_id"], self.teacher_class_id)
        self.assertTrue(result["periods"])

    # -- 7. 只读 + 确定性 --------------------------------------------------
    def test_is_read_only(self):
        def counts():
            conn = self._conn()
            try:
                return {
                    t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                    for t in _TABLES
                }
            finally:
                conn.close()

        before = counts()
        for _ in range(2):
            self._call(self._q(self.class_id), {"authorization": self.admin_token})
        self.assertEqual(counts(), before, "detail 是只读接口，不许写任何表")

    def test_deterministic_output(self):
        """schedule id 是 uuid，未定序会导致同格两条日程顺序随机。"""
        q = self._q(self.class_id)
        a = self._call(q, {"authorization": self.admin_token})
        b = self._call(q, {"authorization": self.admin_token})
        self.assertEqual(a, b)

    # -- 8. 不改 /api/schedules（契约 §4 已冻结）----------------------------
    def test_existing_schedules_contract_untouched(self):
        """旧接口字段集合与 grid 值必须一位不变。"""
        old = api_schedules.list_schedules(
            "GET", "/api/schedules", {"week": [WED]}, b"",
            {"authorization": self.admin_token}, None,
        )
        self.assertEqual(set(old), {"objects", "days", "grid"})
        self.assertEqual(old["days"][0], "2026-09-07")
        self.assertEqual(old["grid"][f"{self.class_id}|{WED}"], "0x000006")

        # 两条路由同时在表里（同文件追加，不是替换）
        self.assertIn(("GET", "/api/schedules"), api_schedules.ROUTES)
        self.assertIn(("GET", "/api/schedules/detail"), api_schedules.ROUTES)

    # -- helpers -----------------------------------------------------------
    def _cell(self, result, idx):
        for c in result["cells"]:
            if c["idx"] == idx:
                return c
        self.fail(f"cells 里没有 idx={idx}：{[c['idx'] for c in result['cells']]}")


if __name__ == "__main__":
    unittest.main()
