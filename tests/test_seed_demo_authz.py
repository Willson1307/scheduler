"""P0-1 验收测试：seed_demo() 教师授权（group_roles）。

覆盖（AGENTS.md §0.0b rji49M 卡面验收 #4）：
  1. seed_demo 后每位教师在 group_roles 至少一行
  2. 同一教师授两次不同档不产生重复行（upsert）
  3. 按任教关系精确授权：教师 authorized_groups 恰为其任教班级，不含根组/别班

运行：python -m unittest tests.test_seed_demo_authz -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import authz  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402
from scheduler.seeds import seed_demo  # noqa: E402


def fresh():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def _user_id(conn, object_id):
    row = conn.execute("SELECT id FROM users WHERE object_id=?", (object_id,)).fetchone()
    return row["id"] if row else None


class TestSeedDemoAuthz(unittest.TestCase):
    def setUp(self):
        self.conn = fresh()

    def tearDown(self):
        self.conn.close()

    def test_every_teacher_has_group_roles(self):
        info = seed_demo(self.conn, classes=3, students_per_class=3, teachers=9, rooms=4)
        distinct = self.conn.execute(
            "SELECT COUNT(DISTINCT user_id) c FROM group_roles"
        ).fetchone()["c"]
        self.assertEqual(distinct, 9)
        for tid in info["teacher_ids"]:
            uid = _user_id(self.conn, tid)
            self.assertIsNotNone(uid, f"教师对象 {tid} 缺 users 行")
            n = self.conn.execute(
                "SELECT COUNT(*) c FROM group_roles WHERE user_id=?", (uid,)
            ).fetchone()["c"]
            self.assertGreaterEqual(n, 1, f"教师 {tid} 无授权行")

    def test_reauthorize_upsert_no_dup(self):
        info = seed_demo(self.conn, classes=3, students_per_class=3, teachers=9, rooms=4)
        before = self.conn.execute("SELECT COUNT(*) c FROM group_roles").fetchone()["c"]
        # 任取一条已有授权，换个档再授一次 → 行数不变（UNIQUE upsert，不并存）
        uid, gid, _level = info["group_roles"][0]
        authz.grant_role(self.conn, info["org_id"], gid, uid, "propose")
        after = self.conn.execute("SELECT COUNT(*) c FROM group_roles").fetchone()["c"]
        self.assertEqual(after, before)

    def test_authorization_scoped_not_full_org(self):
        info = seed_demo(self.conn, classes=3, students_per_class=3, teachers=9, rooms=4)
        # t01 = teacher_ids[0] 只教高一(1)班语文 → authorized_groups 恰好 {class_ids[0]}
        t01_uid = _user_id(self.conn, info["teacher_ids"][0])
        gids = set(authz.authorized_groups(self.conn, t01_uid))
        self.assertEqual(gids, {info["class_ids"][0]})
        self.assertNotIn(info["root_group_id"], gids)
        # 冲突教师 teacher_ids[3] 教 class_ids[1]（语文）+ 冲突演示补 class_ids[0]
        t04_uid = _user_id(self.conn, info["teacher_ids"][3])
        gids4 = set(authz.authorized_groups(self.conn, t04_uid))
        self.assertEqual(gids4, {info["class_ids"][0], info["class_ids"][1]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
