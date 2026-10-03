"""P0-1 回归测试：rebuild_closure 清空闭包表 + BFS depth off-by-one。

关键点：本文件**直接调 rebuild_closure()**，不走 add_edge 的增量维护路径。
test_b0.py 全部走 add_edge，从不直接调 rebuild_closure，因此 31 项全绿掩盖了这个 bug。

运行：python -m unittest tests.test_p0_1 -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect, init_db  # noqa: E402


def fresh():
    conn = connect(":memory:")
    init_db(conn)
    return conn


class TestRebuildClosureP01(unittest.TestCase):
    """每个用例独立建 :memory: 库，不共享 conn。"""

    def _build_chain(self, conn):
        """根组→A→B→C 三层链，返回 (org, root, A, B, C)。"""
        org, root = og.create_organization(conn, "测试校")
        A = og.create_object(conn, org, "group", "A")
        B = og.create_object(conn, org, "group", "B")
        C = og.create_object(conn, org, "group", "C")
        og.add_edge(conn, root, A)
        og.add_edge(conn, A, B)
        og.add_edge(conn, B, C)
        return org, root, A, B, C

    def _depth_of(self, conn, ancestor, descendant):
        row = conn.execute(
            "SELECT depth FROM object_edges WHERE ancestor_id=? AND descendant_id=?",
            (ancestor, descendant),
        ).fetchone()
        return row["depth"] if row else None

    def _ancestor_depths(self, conn, descendant):
        """某节点的所有祖先 depth，升序返回（集合语义，与顺序无关）。"""
        return sorted(
            r["depth"]
            for r in conn.execute(
                "SELECT depth FROM object_edges WHERE descendant_id=? AND depth>0",
                (descendant,),
            ).fetchall()
        )

    def _count_gt0(self, conn):
        return conn.execute(
            "SELECT COUNT(*) c FROM object_edges WHERE depth>0"
        ).fetchone()["c"]

    def _snapshot(self, conn):
        return sorted(
            tuple(r)
            for r in conn.execute(
                "SELECT ancestor_id, descendant_id, depth FROM object_edges"
            ).fetchall()
        )

    def test_rebuild_closure_nonempty(self):
        """用例 1：直接调 rebuild_closure 后闭包表非空（修前 depth>0 = 0）。"""
        conn = fresh()
        try:
            org, root, A, B, C = self._build_chain(conn)
            X = og.create_object(conn, org, "user", "X")
            og.add_edge(conn, root, X)
            before = self._count_gt0(conn)
            self.assertGreater(before, 0)  # add_edge 增量已产生 depth>0 行
            og.rebuild_closure(conn)
            after = self._count_gt0(conn)
            self.assertGreater(after, 0)  # 修前此值为 0（整表被清空）
        finally:
            conn.close()

    def test_rebuild_closure_depth_exact(self):
        """用例 2：C 祖先 depth 集合恰好 {3,2,1}（根组=3, A=2, B=1）。"""
        conn = fresh()
        try:
            org, root, A, B, C = self._build_chain(conn)
            og.rebuild_closure(conn)
            self.assertEqual(self._depth_of(conn, root, C), 3)
            self.assertEqual(self._depth_of(conn, A, C), 2)
            self.assertEqual(self._depth_of(conn, B, C), 1)
            self.assertEqual(self._ancestor_depths(conn, C), [1, 2, 3])
        finally:
            conn.close()

    def test_rebuild_closure_idempotent(self):
        """用例 3：连调两次 rebuild，全表逐条比对相同（幂等，不锁死错值）。"""
        conn = fresh()
        try:
            org, root, A, B, C = self._build_chain(conn)
            og.rebuild_closure(conn)
            first = self._snapshot(conn)
            og.rebuild_closure(conn)
            second = self._snapshot(conn)
            self.assertEqual(first, second)
        finally:
            conn.close()

    def test_remove_edge_on_chain(self):
        """用例 4：remove_edge(根组,A) 后 depth>0 == 3、C 祖先 depth == {2,1}。"""
        conn = fresh()
        try:
            org, root, A, B, C = self._build_chain(conn)
            og.remove_edge(conn, root, A)  # 内部触发 rebuild_closure
            self.assertEqual(self._count_gt0(conn), 3)
            self.assertEqual(self._ancestor_depths(conn, C), [1, 2])
            self.assertEqual(self._depth_of(conn, A, C), 2)
            self.assertEqual(self._depth_of(conn, B, C), 1)
        finally:
            conn.close()

    def test_multi_parent_dag(self):
        """用例 5：多父 DAG 在 rebuild 后不被破坏，祖先 depth == {2,1,1}。"""
        conn = fresh()
        try:
            org, root = og.create_organization(conn, "测试校")
            cls = og.create_object(conn, org, "group", "班级")
            club = og.create_object(conn, org, "group", "社团")
            stu = og.create_object(conn, org, "user", "学生")
            og.add_edge(conn, root, cls)
            og.add_edge(conn, root, club)
            og.add_edge(conn, cls, stu)
            og.add_edge(conn, club, stu)
            og.rebuild_closure(conn)
            self.assertEqual(len(og.groups_of(conn, stu)), 3)  # 班级+社团+根组
            self.assertIn(stu, og.descendants(conn, cls))
            self.assertIn(stu, og.descendants(conn, club))
            self.assertEqual(self._ancestor_depths(conn, stu), [1, 1, 2])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
