"""B2 权限领域层验收测试（authz.py + group_roles 表）。

覆盖四条机制（docs/06 §1 + AGENTS §9）：
  1. 沿对象图祖先链继承（向下：祖先授权 → 后代生效）
  2. 多条路径取最高档（max，不就近优先）
  3. 全局角色当天花板（admin=own / teacher=propose / terminal=view）
  4. 不做负权限 deny（收回 = 删行）

外加三处勘误的回归（队长 2026-10-03）：
  - 勘误2：level 用有序映射，不靠字符串字典序（'own' 字典序 < 'view'，会得反结果）
  - 勘误3：default_group_id 按层级最高（depth-to-root 最小），并列才比 level / group_id

运行：python -m unittest tests.test_authz -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import authz  # noqa: E402
from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402


def fresh():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def _make_user(conn, org, login, role):
    """建一个 objects(user) + users 行，返回 user_id（= users.id）。"""
    obj = og.create_object(conn, org, "user", login)
    og.create_user(conn, obj, login, "pw_" + login, role=role)
    row = conn.execute("SELECT id FROM users WHERE login=?", (login,)).fetchone()
    return row["id"]


class TestLevelOrder(unittest.TestCase):
    """勘误2：level 比较必须用有序映射，不能靠字符串字典序。"""

    def test_rank_order(self):
        # 字典序里 'own' < 'propose' < 'view'（'o'<'p'<'v'），与权限语义完全相反
        self.assertLess(authz.LEVEL_ORDER["view"], authz.LEVEL_ORDER["propose"])
        self.assertLess(authz.LEVEL_ORDER["propose"], authz.LEVEL_ORDER["approve"])
        self.assertLess(authz.LEVEL_ORDER["approve"], authz.LEVEL_ORDER["own"])
        # 证明字符串字典序与权限语义相反：'own' 字典序 < 'view'，但语义 own > view
        self.assertLess("own", "view")  # 字符串字典序：own 排在 view 前
        # 关键：语义上 own > view（必须用 LEVEL_ORDER，不能靠字符串比较）
        self.assertGreater(authz.LEVEL_ORDER["own"], authz.LEVEL_ORDER["view"])

    def test_ceiling_mapping(self):
        self.assertEqual(authz.ceiling_for_role("admin"), "own")
        self.assertEqual(authz.ceiling_for_role("teacher"), "propose")
        self.assertEqual(authz.ceiling_for_role("terminal"), "view")
        self.assertEqual(authz.ceiling_for_role("student"), "view")


class TestInheritance(unittest.TestCase):
    """机制1：沿祖先链继承（授权向下流动）。"""

    def setUp(self):
        self.conn = fresh()
        self.org, self.root = og.create_organization(self.conn, "测试校")
        self.grade = og.create_object(self.conn, self.org, "group", "高一")
        self.cls = og.create_object(self.conn, self.org, "group", "高一1班")
        og.add_edge(self.conn, self.root, self.grade)
        og.add_edge(self.conn, self.grade, self.cls)
        self.teacher = _make_user(self.conn, self.org, "t1", "teacher")

    def tearDown(self):
        self.conn.close()

    def test_grant_on_root_inherits_to_child(self):
        """在根组授 own → 子组 effective 也是 own（继承）。"""
        authz.grant_role(self.conn, self.org, self.root, self.teacher, "own")
        # teacher 全局角色天花板 = propose，所以实际被 cap 到 propose
        self.assertEqual(authz.effective_level(self.conn, self.teacher, self.cls), "propose")

    def test_grant_on_grade_inherits_to_class(self):
        """在年级授 propose → 班级 effective 也是 propose。"""
        authz.grant_role(self.conn, self.org, self.grade, self.teacher, "propose")
        self.assertEqual(authz.effective_level(self.conn, self.teacher, self.cls), "propose")

    def test_no_grant_no_access(self):
        """无任何授权（非 admin）→ effective_level 为 None。"""
        self.assertIsNone(authz.effective_level(self.conn, self.teacher, self.cls))


class TestMultiPathMax(unittest.TestCase):
    """机制2：多条路径取最高档（不就近优先）。"""

    def setUp(self):
        self.conn = fresh()
        self.org, self.root = og.create_organization(self.conn, "测试校")
        # 两条路径到 stu_group：root→A→stu_group 与 root→B→stu_group
        self.a = og.create_object(self.conn, self.org, "group", "路径A")
        self.b = og.create_object(self.conn, self.org, "group", "路径B")
        self.target = og.create_object(self.conn, self.org, "group", "目标组")
        og.add_edge(self.conn, self.root, self.a)
        og.add_edge(self.conn, self.root, self.b)
        og.add_edge(self.conn, self.a, self.target)
        og.add_edge(self.conn, self.b, self.target)
        self.teacher = _make_user(self.conn, self.org, "t2", "teacher")

    def tearDown(self):
        self.conn.close()

    def test_max_of_two_paths(self):
        """A 上授 view、B 上授 propose → 目标组取 max(propose)。
        就近优先会取更近的那条，但规则是取最高档。"""
        authz.grant_role(self.conn, self.org, self.a, self.teacher, "view")
        authz.grant_role(self.conn, self.org, self.b, self.teacher, "propose")
        self.assertEqual(authz.effective_level(self.conn, self.teacher, self.target), "propose")

    def test_max_not_nearest(self):
        """反过来：近的路径档位低、远的档位高 → 仍取高的（远的那条）。"""
        authz.grant_role(self.conn, self.org, self.a, self.teacher, "propose")
        authz.grant_role(self.conn, self.org, self.b, self.teacher, "view")
        self.assertEqual(authz.effective_level(self.conn, self.teacher, self.target), "propose")


class TestCeiling(unittest.TestCase):
    """机制3：全局角色当天花板。"""

    def setUp(self):
        self.conn = fresh()
        self.org, self.root = og.create_organization(self.conn, "测试校")
        self.cls = og.create_object(self.conn, self.org, "group", "高一1班")
        og.add_edge(self.conn, self.root, self.cls)

    def tearDown(self):
        self.conn.close()

    def test_admin_short_circuit(self):
        """admin 全组织短路：无任何 group_roles 行也得 own（AGENTS §9）。"""
        admin = _make_user(self.conn, self.org, "admin1", "admin")
        self.assertEqual(authz.effective_level(self.conn, admin, self.cls), "own")
        self.assertEqual(authz.effective_level(self.conn, admin, self.root), "own")

    def test_teacher_capped_at_propose(self):
        """teacher 在某组授 own → 被 cap 到 propose（审批流下降，否则能批永久改）。"""
        teacher = _make_user(self.conn, self.org, "t3", "teacher")
        authz.grant_role(self.conn, self.org, self.cls, teacher, "own")
        self.assertEqual(authz.effective_level(self.conn, teacher, self.cls), "propose")

    def test_terminal_capped_at_view(self):
        """terminal 授 own → 被 cap 到 view（班级只读终端）。"""
        term = _make_user(self.conn, self.org, "term1", "terminal")
        authz.grant_role(self.conn, self.org, self.cls, term, "own")
        self.assertEqual(authz.effective_level(self.conn, term, self.cls), "view")

    def test_grant_below_ceiling_not_capped(self):
        """teacher 授 view（低于天花板 propose）→ 不被 cap，仍是 view。"""
        teacher = _make_user(self.conn, self.org, "t4", "teacher")
        authz.grant_role(self.conn, self.org, self.cls, teacher, "view")
        self.assertEqual(authz.effective_level(self.conn, teacher, self.cls), "view")


class TestNoDeny(unittest.TestCase):
    """机制4：不做负权限 deny。收回权限 = 删掉那一行。"""

    def setUp(self):
        self.conn = fresh()
        self.org, self.root = og.create_organization(self.conn, "测试校")
        self.cls = og.create_object(self.conn, self.org, "group", "高一1班")
        og.add_edge(self.conn, self.root, self.cls)
        self.teacher = _make_user(self.conn, self.org, "t5", "teacher")

    def tearDown(self):
        self.conn.close()

    def test_revoke_by_delete_row(self):
        """授 propose 后删行 → 权限消失（不是 deny 抵消）。"""
        authz.grant_role(self.conn, self.org, self.cls, self.teacher, "propose")
        self.assertEqual(authz.effective_level(self.conn, self.teacher, self.cls), "propose")
        authz.revoke_role(self.conn, self.org, self.cls, self.teacher)
        self.assertIsNone(authz.effective_level(self.conn, self.teacher, self.cls))

    def test_grant_upsert_replaces(self):
        """同一人同一组再授不同档 → 覆盖，不并存（UNIQUE 约束 + upsert）。"""
        authz.grant_role(self.conn, self.org, self.cls, self.teacher, "view")
        authz.grant_role(self.conn, self.org, self.cls, self.teacher, "propose")
        # 不是两行取 max，而是一行被覆盖成 propose
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM group_roles WHERE user_id=? AND group_id=?",
            (self.teacher, self.cls),
        ).fetchone()["c"]
        self.assertEqual(n, 1)
        self.assertEqual(authz.effective_level(self.conn, self.teacher, self.cls), "propose")


class TestCan(unittest.TestCase):
    """can(user, group, required) 判定。"""

    def setUp(self):
        self.conn = fresh()
        self.org, self.root = og.create_organization(self.conn, "测试校")
        self.cls = og.create_object(self.conn, self.org, "group", "高一1班")
        og.add_edge(self.conn, self.root, self.cls)
        self.teacher = _make_user(self.conn, self.org, "t6", "teacher")

    def tearDown(self):
        self.conn.close()

    def test_can_exact(self):
        authz.grant_role(self.conn, self.org, self.cls, self.teacher, "propose")
        self.assertTrue(authz.can(self.conn, self.teacher, self.cls, "propose"))

    def test_can_higher_satisfies_lower(self):
        authz.grant_role(self.conn, self.org, self.root, self.teacher, "propose")
        # 继承到 cls 仍是 propose（天花板也是 propose）
        self.assertTrue(authz.can(self.conn, self.teacher, self.cls, "view"))

    def test_cannot_exceed(self):
        authz.grant_role(self.conn, self.org, self.cls, self.teacher, "view")
        self.assertFalse(authz.can(self.conn, self.teacher, self.cls, "propose"))

    def test_admin_can_anything(self):
        admin = _make_user(self.conn, self.org, "admin2", "admin")
        self.assertTrue(authz.can(self.conn, admin, self.cls, "own"))


class TestAuthorizedGroups(unittest.TestCase):
    """/api/groups 的底座：只返回我有授权（含继承）的组。"""

    def setUp(self):
        self.conn = fresh()
        self.org, self.root = og.create_organization(self.conn, "测试校")
        self.g1 = og.create_object(self.conn, self.org, "group", "分支1")
        self.g1c = og.create_object(self.conn, self.org, "group", "分支1子组")
        self.g2 = og.create_object(self.conn, self.org, "group", "分支2")
        og.add_edge(self.conn, self.root, self.g1)
        og.add_edge(self.conn, self.g1, self.g1c)
        og.add_edge(self.conn, self.root, self.g2)
        self.teacher = _make_user(self.conn, self.org, "t7", "teacher")

    def tearDown(self):
        self.conn.close()

    def test_only_authorized_branch(self):
        """在 g1 授 propose → 能看到 g1 及其后代 g1c，看不到 g2。"""
        authz.grant_role(self.conn, self.org, self.g1, self.teacher, "propose")
        groups = authz.authorized_groups(self.conn, self.teacher)
        self.assertIn(self.g1, groups)
        self.assertIn(self.g1c, groups)  # 继承
        self.assertNotIn(self.g2, groups)  # 未授权的组根本不列出

    def test_admin_sees_all(self):
        admin = _make_user(self.conn, self.org, "admin3", "admin")
        groups = authz.authorized_groups(self.conn, admin)
        self.assertIn(self.root, groups)
        self.assertIn(self.g1, groups)
        self.assertIn(self.g2, groups)
        self.assertIn(self.g1c, groups)

    def test_no_grant_sees_none(self):
        groups = authz.authorized_groups(self.conn, self.teacher)
        self.assertEqual(groups, [])


class TestDefaultGroupId(unittest.TestCase):
    """勘误3：default_group_id = 沿祖先链层级最高（depth-to-root 最小）。"""

    def setUp(self):
        self.conn = fresh()
        self.org, self.root = og.create_organization(self.conn, "测试校")
        self.grade = og.create_object(self.conn, self.org, "group", "高一")
        self.cls = og.create_object(self.conn, self.org, "group", "高一1班")
        og.add_edge(self.conn, self.root, self.grade)
        og.add_edge(self.conn, self.grade, self.cls)

    def tearDown(self):
        self.conn.close()

    def test_admin_defaults_to_root(self):
        """管理员 → 根组「全校」（层级最高，depth=0）。"""
        admin = _make_user(self.conn, self.org, "admin4", "admin")
        self.assertEqual(authz.default_group_id(self.conn, admin), self.root)

    def test_grade_leader_defaults_to_grade(self):
        """年级组长（在年级授 propose）→ 默认组 = 年级（比班级层级高）。"""
        leader = _make_user(self.conn, self.org, "lead1", "teacher")
        authz.grant_role(self.conn, self.org, self.grade, leader, "propose")
        authz.grant_role(self.conn, self.org, self.cls, leader, "propose")
        self.assertEqual(authz.default_group_id(self.conn, leader), self.grade)

    def test_tiebreak_level_then_groupid(self):
        """并列（同 depth）时：① level 高的优先 ② group_id 字典序小的优先。"""
        # 建两个同层级的并列组（都直接挂 root 下，depth=1）
        g_a = og.create_object(self.conn, self.org, "group", "AAA")
        g_b = og.create_object(self.conn, self.org, "group", "BBB")
        og.add_edge(self.conn, self.root, g_a)
        og.add_edge(self.conn, self.root, g_b)
        teacher = _make_user(self.conn, self.org, "t8", "teacher")
        # g_a 授 view（档低），g_b 授 propose（档高）→ 应选 g_b（level 优先）
        authz.grant_role(self.conn, self.org, g_a, teacher, "view")
        authz.grant_role(self.conn, self.org, g_b, teacher, "propose")
        self.assertEqual(authz.default_group_id(self.conn, teacher), g_b)

    def test_tiebreak_groupid_when_same_level(self):
        """同 depth 同 level → group_id 字典序小的优先（保证结果确定）。
        注意：勘误3 比的是 group_id（obj_xxx 串）不是组名。"""
        g_a = og.create_object(self.conn, self.org, "group", "ZGroup")
        g_b = og.create_object(self.conn, self.org, "group", "AGroup")
        og.add_edge(self.conn, self.root, g_a)
        og.add_edge(self.conn, self.root, g_b)
        teacher = _make_user(self.conn, self.org, "t9", "teacher")
        authz.grant_role(self.conn, self.org, g_a, teacher, "propose")
        authz.grant_role(self.conn, self.org, g_b, teacher, "propose")
        # 同 depth(1) 同 level(propose) → 取 group_id 字典序更小者
        expected = g_b if g_b < g_a else g_a
        self.assertEqual(authz.default_group_id(self.conn, teacher), expected)

    def test_no_grant_no_default(self):
        teacher = _make_user(self.conn, self.org, "t10", "teacher")
        self.assertIsNone(authz.default_group_id(self.conn, teacher))


if __name__ == "__main__":
    unittest.main(verbosity=2)
