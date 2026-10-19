"""CP-H-S2 验收测试：admin 可见范围收敛到所属组织（方案甲-2）。

背景（docs/21 §6.1，2026-10-19 卡）：
  修复前 authz.py 两处 admin 短路（effective_level 无条件 'own'、
  authorized_groups SELECT 全库组）→ 多组织库上 admin 可见全部组织，
  违背「一师一组织隔离」承诺。实测 trial_admin /api/groups 37 项覆盖 13 组织。

方案甲-2 定版语义（队长 2026-10-19 拍板）：
  1. admin 隐式 own 仅限**自己所属组织**（users.object_id → objects.org_id）；
  2. 组织外的组走 group_roles **显式授权**（祖先链继承 + ceiling cap=own），
     无授权 → 不可见 / 403；
  3. admin 无所属组织（object_id 悬挂）→ fail-closed，只剩显式授权。

对照组（不能被改坏）：非 admin 路径完全无感——教师授权继承、跨组织 403
 语义与修复前一致。

破坏法（本文件判红的依据，2026-10-19 执行过）：
  把 authz.py 两处 admin 分支还原成「全库短路」（去掉 org_id 过滤与
  显式授权 fall-through）→ 本文件多组织用例必红。

运行：python -m unittest tests.test_cph_s2_admin_scope -v
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
    """建 objects(user) + users 行，返回 user_id。"""
    obj = og.create_object(conn, org, "user", login)
    og.create_user(conn, obj, login, "pw_" + login, role=role)
    row = conn.execute("SELECT id FROM users WHERE login=?", (login,)).fetchone()
    return row["id"]


class _TwoOrgBase(unittest.TestCase):
    """双组织夹具：org A（admin 所在）与 org B（完全独立）。

    修复前 admin 短路 → A 的 admin 能看到 B 的组（缺陷本体）；
    修复后默认不可见、显式授权后才可见。
    """

    def setUp(self):
        self.conn = fresh()
        # 组织 A：根组 → 班级；admin 挂在 A
        self.org_a, self.root_a = og.create_organization(self.conn, "甲校")
        self.cls_a = og.create_object(self.conn, self.org_a, "group", "甲校1班")
        og.add_edge(self.conn, self.root_a, self.cls_a)
        self.admin = _make_user(self.conn, self.org_a, "a_admin", "admin")

        # 组织 B：根组 → 班级；教师 tb 授 own（天花板压到 propose）
        self.org_b, self.root_b = og.create_organization(self.conn, "乙校")
        self.cls_b = og.create_object(self.conn, self.org_b, "group", "乙校1班")
        og.add_edge(self.conn, self.root_b, self.cls_b)
        self.teacher_b = _make_user(self.conn, self.org_b, "b_t", "teacher")
        authz.grant_role(self.conn, self.org_b, self.cls_b, self.teacher_b, "own")

    def tearDown(self):
        self.conn.close()


class TestAdminImplicitScope(_TwoOrgBase):
    """核心判据①：admin 隐式 own 收敛到所属组织。"""

    def test_own_org_implicit_own(self):
        """本组织内无需任何 group_roles 行即 own（甲-2 保留的隐式能力）。"""
        self.assertEqual(authz.effective_level(self.conn, self.admin, self.root_a), "own")
        self.assertEqual(authz.effective_level(self.conn, self.admin, self.cls_a), "own")

    def test_cross_org_invisible_by_default(self):
        """🔴 CP-H-S2 本体：组织 B 的组对 A 的 admin 默认不可见。"""
        self.assertIsNone(authz.effective_level(self.conn, self.admin, self.root_b))
        self.assertIsNone(authz.effective_level(self.conn, self.admin, self.cls_b))
        self.assertFalse(authz.can(self.conn, self.admin, self.root_b, "view"))
        self.assertFalse(authz.can(self.conn, self.admin, self.cls_b, "approve"))

    def test_authorized_groups_excludes_other_org(self):
        """组列表只含本组织的组（修复前为全库组）。"""
        self.assertEqual(
            set(authz.authorized_groups(self.conn, self.admin)),
            {self.root_a, self.cls_a},
        )

    def test_default_group_is_own_org_root(self):
        """admin 默认组 = 本组织根组（勘误3 语义不变）。"""
        self.assertEqual(authz.default_group_id(self.conn, self.admin), self.root_a)


class TestAdminExplicitGrant(_TwoOrgBase):
    """核心判据②：甲-2 —— 组织外靠显式授权扩展。"""

    def test_grant_extends_scope_with_inheritance(self):
        """授 B 根组 own → B 全树可见（祖先链继承到班级）。"""
        authz.grant_role(self.conn, self.org_b, self.root_b, self.admin, "own")
        self.assertEqual(authz.effective_level(self.conn, self.admin, self.root_b), "own")
        self.assertEqual(authz.effective_level(self.conn, self.admin, self.cls_b), "own")
        self.assertEqual(
            set(authz.authorized_groups(self.conn, self.admin)),
            {self.root_a, self.cls_a, self.root_b, self.cls_b},
        )

    def test_grant_level_not_inflated(self):
        """显式授 view 就是 view（ceiling=own 是上限不是抬档）。"""
        authz.grant_role(self.conn, self.org_b, self.root_b, self.admin, "view")
        self.assertEqual(authz.effective_level(self.conn, self.admin, self.root_b), "view")
        # view 不足以 approve
        self.assertFalse(authz.can(self.conn, self.admin, self.root_b, "approve"))

    def test_revoke_shrinks_scope_back(self):
        """撤销授权（删行，机制④）→ 跨组织可见性随之收回。"""
        authz.grant_role(self.conn, self.org_b, self.root_b, self.admin, "own")
        authz.revoke_role(self.conn, self.org_b, self.root_b, self.admin)
        self.assertIsNone(authz.effective_level(self.conn, self.admin, self.root_b))
        self.assertEqual(
            set(authz.authorized_groups(self.conn, self.admin)),
            {self.root_a, self.cls_a},
        )


class TestFailClosed(_TwoOrgBase):
    """核心判据③：admin 无所属组织 → fail-closed。

    注：schema 外键约束正常情况下保证 object_id 不悬挂（实测 UPDATE 直接
    IntegrityError——这本身是好消息）。这两个用例用 PRAGMA foreign_keys=OFF
    模拟「库被外力改坏」的脏数据，验证领域层不依赖外键也 fail-closed。
    """

    def _dangle_admin_object(self):
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute(
            "UPDATE users SET object_id='obj_nonexistent' WHERE id=?", (self.admin,)
        )
        self.conn.commit()

    def test_dangling_object_id_no_implicit_scope(self):
        """object_id 悬挂 → 本组织短路失效，只剩显式授权（这里是空）。"""
        self._dangle_admin_object()
        self.assertIsNone(authz.effective_level(self.conn, self.admin, self.root_a))
        self.assertEqual(authz.authorized_groups(self.conn, self.admin), [])

    def test_dangling_object_id_keeps_explicit_grant(self):
        """悬挂后显式授权仍有效（授权表是唯一事实源）。"""
        self._dangle_admin_object()
        authz.grant_role(self.conn, self.org_b, self.root_b, self.admin, "own")
        self.conn.commit()
        self.assertEqual(authz.effective_level(self.conn, self.admin, self.root_b), "own")
        self.assertEqual(
            set(authz.authorized_groups(self.conn, self.admin)),
            {self.root_b, self.cls_b},
        )


class TestTeacherControlGroup(_TwoOrgBase):
    """对照组：非 admin 路径完全无感（坑 5：教师路径不能被改坏）。"""

    def test_teacher_scope_unchanged(self):
        """B 组织教师：本组织组可见（propose），A 组织组不可见。"""
        self.assertEqual(
            set(authz.authorized_groups(self.conn, self.teacher_b)), {self.cls_b}
        )
        self.assertEqual(
            authz.effective_level(self.conn, self.teacher_b, self.cls_b), "propose"
        )
        self.assertIsNone(
            authz.effective_level(self.conn, self.teacher_b, self.root_a)
        )


class TestServerLayerAdminScope(unittest.TestCase):
    """服务层 E2E：/api/groups 与 resolve_authorized_org（8 个接口共用的咽喉）。

    修复的「静默失效」风险在接口层：领域层对了、接口层漏调，同样漏。
    这里经 handler 直调（require_auth → authorized_groups）验证。
    """

    def setUp(self):
        import tempfile
        from datetime import date
        from server import config, routes

        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        conn = connect(self.db_path)
        init_db(conn)
        # 双组织：A（admin）+ B
        org_a, root_a = og.create_organization(conn, "甲校")
        cls_a = og.create_object(conn, org_a, "group", "甲校1班")
        og.add_edge(conn, root_a, cls_a)
        a_obj = og.create_object(conn, org_a, "user", "管理员")
        og.create_user(conn, a_obj, "admin", "admin123", role="admin")
        org_b, root_b = og.create_organization(conn, "乙校")
        cls_b = og.create_object(conn, org_b, "group", "乙校1班")
        og.add_edge(conn, root_b, cls_b)
        conn.commit()
        conn.close()

        self.org_b = org_b
        self.root_a, self.cls_a = root_a, cls_a
        self.root_b, self.cls_b = root_b, cls_b
        self.admin_uid = None

        cfg = config.ServerConfig(
            host="127.0.0.1", port=0, db_path=self.db_path,
            today=date(2026, 10, 5), seed_if_missing=False,
        )
        config.set_current_config(cfg)
        routes.reset_cache()
        from server.api_login import reset_locks
        reset_locks()
        self._config, self._routes = config, routes

    def tearDown(self):
        self._config.set_current_config(None)
        self._routes.reset_cache()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _normalize(ret):
        """handler 返回约定：元组 = (status, body)；单值 = 200。"""
        if isinstance(ret, tuple):
            return ret
        return 200, ret

    def _admin_headers(self):
        from server.api_login import ROUTES as LOGIN_ROUTES
        import json
        h = LOGIN_ROUTES[("POST", "/api/login")]
        body = json.dumps({"login": "admin", "password": "admin123"}).encode()
        status, payload = self._normalize(h("POST", "/api/login", {}, body, {}))
        assert status == 200, payload
        self.admin_uid = payload["user"]["id"]
        return {"authorization": "Bearer " + payload["token"]}

    def _groups(self, headers):
        from server.api_groups import ROUTES as GROUP_ROUTES
        h = GROUP_ROUTES[("GET", "/api/groups")]
        return h("GET", "/api/groups", {}, b"", headers)

    def test_api_groups_admin_only_own_org(self):
        """🔴 接口层核心判据：admin 登录 → /api/groups 只含本组织组。"""
        result = self._groups(self._admin_headers())
        self.assertEqual({g["group_id"] for g in result}, {self.root_a, self.cls_a})

    def test_api_groups_explicit_grant_extends(self):
        """显式授权后接口层可见性扩展（甲-2 的接口层闭环）。"""
        headers = self._admin_headers()
        conn = connect(self.db_path)
        authz.grant_role(conn, self.org_b, self.root_b, self.admin_uid, "own")
        conn.commit()
        conn.close()
        result = self._groups(headers)
        self.assertEqual(
            {g["group_id"] for g in result},
            {self.root_a, self.cls_a, self.root_b, self.cls_b},
        )

    def test_resolve_authorized_org_403_cross_org(self):
        """8 个接口共用的咽喉：未授权的跨组织 group → ApiError 403。"""
        from server.b3_kernels import resolve_authorized_org, ApiError
        conn = connect(self.db_path)
        try:
            self._admin_headers()
            with self.assertRaises(ApiError) as cm:
                resolve_authorized_org(conn, self.admin_uid, self.root_b)
            self.assertEqual(cm.exception.status, 403)
        finally:
            conn.close()

    def test_resolve_authorized_org_default_group_ok(self):
        """缺省 group → admin 默认组（本组织根组）解析正常，不 403。"""
        from server.b3_kernels import resolve_authorized_org
        conn = connect(self.db_path)
        try:
            self._admin_headers()
            group, org_id = resolve_authorized_org(conn, self.admin_uid, None)
            self.assertEqual(group, self.root_a)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
