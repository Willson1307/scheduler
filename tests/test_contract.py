"""L2 T10 · API 契约符合性测试（唯一依据：docs/07-API契约.md，v1.0 已冻结）。

用途与设计意图（答辩口径）：
  1. 契约是三人并行开发的唯一接口依据。本测试逐条校验后端返回 JSON 的
     「字段名 / 类型 / 枚举值 / 结构」是否符合 docs/07 v1.0 —— 后端一旦跑偏，
     联调前就能被拦下，而不是等联调时才发现两边对不上。
  2. 只测契约、不测实现：只走 HTTP（方法 + 路径 + 请求体 + 响应体），不 import
     后端内部函数；掩码只做「是不是十六进制文本」的格式校验，不做位运算；数组
     长度不写死（「最多 3 个方案」是上限）；不因后端多返回字段而失败（契约 §12
     宽容解析）。断言不设上限：绝不为了让测试变绿而删检查项。
  3. 服务未启动 → 整体 skip（不报错、不破坏 `unittest discover` 的 31 项老测试）；
     服务已启动但接口未实现（404）→ 如实标红，把后端卡住。

运行方式（服务需先启动、且库已灌入演示数据）：
    python run_server.py                    # 后端（B1+），默认 http://127.0.0.1:8000
    python -m unittest tests.test_contract -v
  可用环境变量 SCHEDULER_BASE_URL 指向已启动的服务（如 http://127.0.0.1:8000）。
  服务未启动时本模块整体 skip —— 这是预期行为，不是失败。

契约文档内部的已知分歧（测试按队长拍板的口径写，见交付报告）：
  - conflicts 的 readable 元素键名：契约 §5 示例用 "day"，字段表用 "dow"；本测试按「dow」。
  - 掩码格式：§5/§6 示例带 0x 前缀，AGENTS §9b 说 mask_to_hex 无前缀；本测试两者都接受。
  - groups 的 level 字段：契约 §2 未写类型，按 docs/06 §1 裁定为四档权限（字符串
    view/propose/approve/own），不是层级深度（层级用 object_edges.depth 表示）。
"""

from __future__ import annotations

import json
import os
import re
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE_URL = os.environ.get("SCHEDULER_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# 本地契约测试直连本机服务，绕过系统/环境里可能配置的 HTTP 代理
# （否则代理会拦截 127.0.0.1 并返回 502，把「服务未启动」误判成「服务异常」）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_KIND = {"organization", "group", "user", "resource"}
_LEVEL = {"view", "propose", "approve", "own"}
_TERMINATION = {"ok", "no_solution", "best_effort"}
_HEX_RE = re.compile(r"^(?:0[xX])?[0-9a-fA-F]+$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _Unreachable(Exception):
    """服务不可达（连接拒绝 / 超时等），用于触发优雅跳过。"""


def _is_iso_date(s) -> bool:
    if not isinstance(s, str) or _ISO_RE.match(s) is None:
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _is_hex_text(s) -> bool:
    """仅校验「是十六进制文本」，不解码、不做位运算（契约只保证它是 hex）。"""
    return isinstance(s, str) and _HEX_RE.match(s) is not None


def _parse(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def _request(method: str, path: str, body=None, token=None, timeout=5.0):
    url = BASE_URL + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except OSError as e:  # 连接拒绝 / 超时等（URLError 是其子类）
        raise _Unreachable(repr(e)) from e


# 模块级共享状态：setUpModule 里探测健康 + 登录一次，供各测试复用。
_TOKEN = None
_DEFAULT_GROUP = None
_LOGIN_ERR = None


def setUpModule():
    global _TOKEN, _DEFAULT_GROUP, _LOGIN_ERR
    # 1) 健康探测：连不上 → 服务未启动，整体跳过（保持 discover 干净）。
    try:
        st, body = _request("GET", "/api/health", timeout=3.0)
    except _Unreachable as e:
        raise unittest.SkipTest(
            f"服务未启动：无法连接 {BASE_URL}（{e}）。"
            "请先启动后端并灌入演示库（python run_server.py），"
            "或用环境变量 SCHEDULER_BASE_URL 指向已启动的服务。"
        )
    if not (st == 200 and isinstance(body, dict) and body.get("ok") is True):
        raise unittest.SkipTest(
            f"服务未就绪：GET /api/health 返回 {st} {body!r}，期望 200 {{\"ok\": true}}。"
        )
    # 2) 登录一次，供需要鉴权的测试复用；失败只记原因，交由 TestLogin 如实标红。
    try:
        st, body = _request("POST", "/api/login", {"login": "admin", "password": "admin123"})
    except _Unreachable as e:
        _LOGIN_ERR = f"服务不可达：{e}"
        return
    if st == 200 and isinstance(body, dict) and body.get("token"):
        _TOKEN = body["token"]
        _DEFAULT_GROUP = body.get("default_group_id")
    else:
        _LOGIN_ERR = f"POST /api/login 返回 {st} {body!r}（期望 200 且含 token）"


class _Authed(unittest.TestCase):
    """需要 token 的测试基类：登录失败时优雅跳过（登录契约由 TestLogin 单独卡）。"""

    def setUp(self):
        if _LOGIN_ERR is not None:
            self.skipTest(f"未取得 token（登录失败），跳过本组：{_LOGIN_ERR}")
        if _TOKEN is None:
            self.skipTest("未取得 token，跳过本组")

    def _call(self, method, path, body=None):
        try:
            return _request(method, path, body=body, token=_TOKEN)
        except _Unreachable as e:
            self.skipTest(f"服务不可达：{e}")

    def _json200(self, method, path, body=None):
        st, data = self._call(method, path, body)
        self.assertEqual(st, 200, f"{method} {path} 期望 HTTP 200，实际 {st}，响应={data!r}")
        return data


# ---------------------------------------------------------------------------
# §1 POST /api/login（既是取 token 的前置，本身也是契约 §1）
# ---------------------------------------------------------------------------
class TestLogin(unittest.TestCase):
    def test_login_returns_token_and_default_group(self):
        st, body = _request("POST", "/api/login", {"login": "admin", "password": "admin123"})
        self.assertEqual(st, 200)
        self.assertIsInstance(body, dict)
        self.assertIsInstance(body.get("token"), str)
        self.assertTrue(body["token"])
        self.assertIsInstance(body.get("default_group_id"), str)
        self.assertTrue(body["default_group_id"])
        user = body.get("user")
        self.assertIsInstance(user, dict)
        for key in ("id", "login", "global_role"):
            self.assertIn(key, user, f"user 缺字段 {key}")
        self.assertNotIn("password_hash", body)  # §1 🔴 绝不回传 password_hash


# ---------------------------------------------------------------------------
# §2 GET /api/groups
# ---------------------------------------------------------------------------
class TestGroups(_Authed):
    def _groups(self):
        return self._json200("GET", "/api/groups")

    def test_groups_list_and_fields(self):
        data = self._groups()
        self.assertIsInstance(data, list)
        self.assertTrue(data, "管理员至少应有 1 个可见组（根组）")
        for item in data:
            self.assertIsInstance(item, dict)
            for key in ("group_id", "name", "level", "member_count", "parent_name"):
                self.assertIn(key, item, f"组缺字段 {key}")
            self.assertIsInstance(item["group_id"], str)
            self.assertTrue(item["group_id"])
            self.assertIsInstance(item["name"], str)
            self.assertTrue(item["name"])
            self.assertIsInstance(item["level"], str)
            self.assertIn(item["level"], _LEVEL, f"level 取值非法：{item['level']!r}")
            self.assertIsInstance(item["member_count"], int)
            self.assertGreaterEqual(item["member_count"], 0)
            self.assertTrue(item["parent_name"] is None or isinstance(item["parent_name"], str))

    def test_default_group_is_highest_ancestor(self):
        data = self._groups()
        ids = {g["group_id"] for g in data}
        self.assertIsNotNone(_DEFAULT_GROUP, "登录响应缺少 default_group_id")
        self.assertIn(_DEFAULT_GROUP, ids, "default_group_id 必须是 /api/groups 里可见的组")
        default = next(g for g in data if g["group_id"] == _DEFAULT_GROUP)
        self.assertIsNone(
            default["parent_name"],
            "缺省组=沿祖先链层级最高（到根 depth 最小）= 管理员根组「全校」，无父组",
        )


# ---------------------------------------------------------------------------
# §3 GET /api/objects
# ---------------------------------------------------------------------------
class TestObjects(_Authed):
    def test_objects_kind_is_one_of_four(self):
        data = self._json200("GET", "/api/objects")
        self.assertIsInstance(data, list)
        self.assertTrue(data, "管理员当前组闭包内至少应有对象")
        kinds = set()
        for item in data:
            self.assertIsInstance(item, dict)
            for key in ("id", "kind", "name", "parent_ids"):
                self.assertIn(key, item, f"对象缺字段 {key}")
            self.assertIsInstance(item["id"], str)
            self.assertTrue(item["id"])
            self.assertIn(item["kind"], _KIND, f"kind 取值非法：{item['kind']!r}")
            self.assertIsInstance(item["name"], str)
            self.assertIsInstance(item["parent_ids"], list)
            kinds.add(item["kind"])
        self.assertTrue(kinds <= _KIND)


# ---------------------------------------------------------------------------
# §4 GET /api/schedules（grid key 用竖线；week 缺省=本周，周一起）
# ---------------------------------------------------------------------------
class TestSchedules(_Authed):
    def _schedules(self, query=""):
        return self._json200("GET", "/api/schedules" + query)

    def test_response_structure(self):
        data = self._schedules()
        self.assertIsInstance(data, dict)
        for key in ("objects", "days", "grid"):
            self.assertIn(key, data, f"schedules 响应缺 {key}")
        self.assertIsInstance(data["objects"], list)
        self.assertIsInstance(data["days"], list)
        self.assertIsInstance(data["grid"], dict)

    def test_grid_key_uses_pipe_and_value_is_hex(self):
        data = self._schedules()
        grid = data["grid"]
        self.assertIsInstance(grid, dict)
        for key, value in grid.items():
            self.assertIsInstance(key, str)
            self.assertEqual(key.count("|"), 1, f"grid key 必须竖线分隔：{key!r}")
            oid, day = key.split("|")
            self.assertTrue(oid, f"grid key 缺 object_id：{key!r}")
            self.assertTrue(_is_iso_date(day), f"grid key 日期非法：{key!r}")
            self.assertTrue(_is_hex_text(value), f"mask 不是十六进制文本：{value!r}")

    def test_default_week_starts_monday_and_is_this_week(self):
        data = self._schedules()
        days = data["days"]
        self.assertTrue(days, "days 不能为空")
        self.assertTrue(all(_is_iso_date(d) for d in days), "days 每一项都应是 ISO 日期")
        monday = date.fromisoformat(days[0])
        self.assertEqual(monday.weekday(), 0, "week 缺省应以周一为起点")
        for i, d in enumerate(days):
            self.assertEqual(date.fromisoformat(d), monday + timedelta(days=i), "days 应连续")
        today = date.today()
        self.assertEqual(
            monday, today - timedelta(days=today.weekday()), "week 缺省应=本周（含今天的那一周）"
        )

    def test_week_param_normalizes_to_monday(self):
        base = self._schedules()
        self.assertTrue(base["days"], "days 不能为空")
        monday = date.fromisoformat(base["days"][0])
        wed = (monday + timedelta(days=2)).isoformat()
        data = self._schedules("?week=" + wed)
        self.assertEqual(data["days"][0], monday.isoformat(), "week 传周内任一日期应自行归到周一")


# ---------------------------------------------------------------------------
# §5 GET /api/conflicts
# ---------------------------------------------------------------------------
class TestConflicts(_Authed):
    def _conflicts(self):
        return self._json200("GET", "/api/conflicts")

    def test_conflicts_structure(self):
        data = self._conflicts()
        self.assertIsInstance(data, dict)
        self.assertIn("count", data)
        self.assertIn("items", data)
        self.assertIsInstance(data["count"], int)
        self.assertGreaterEqual(data["count"], 0)
        self.assertIsInstance(data["items"], list)
        if data["count"] > 0:
            self.assertTrue(data["items"], "count>0 时 items 不应为空")

    def test_conflict_item_fields_are_full(self):
        data = self._conflicts()
        for item in data["items"]:
            self.assertIsInstance(item, dict)
            for key in ("a", "b", "date", "slots", "readable"):
                self.assertIn(key, item, f"冲突项缺 {key}")
            for side_key in ("a", "b"):
                side = item[side_key]
                self.assertIsInstance(side, dict)
                for key in (
                    "schedule_id", "object_id", "object_name",
                    "object_kind", "title", "date", "slots", "readable",
                ):
                    self.assertIn(key, side, f"冲突 a/b 缺字段 {key}")
            self.assertTrue(_is_iso_date(item["date"]))
            self.assertTrue(_is_hex_text(item["slots"]))
            self.assertIsInstance(item["readable"], list)
            for r in item["readable"]:
                self.assertIsInstance(r, dict)
                self.assertIn("dow", r, "readable 缺 dow")
                self.assertIn("period", r, "readable 缺 period")
                self.assertIsInstance(r["dow"], int)
                self.assertIsInstance(r["period"], int)

    def test_conflicts_are_aggregated(self):
        data = self._conflicts()
        seen = set()
        for item in data["items"]:
            a, b = item.get("a"), item.get("b")
            if not isinstance(a, dict) or not isinstance(b, dict):
                continue  # 字段完整性由 test_conflict_item_fields_are_full 校验
            key = (
                frozenset((a.get("schedule_id"), b.get("schedule_id"))),
                item.get("date"),
                item.get("slots"),
            )
            self.assertNotIn(key, seen, f"冲突未按「日程对×日期×节次」聚合：{key}")
            seen.add(key)


# ---------------------------------------------------------------------------
# §6 GET /api/resolve
# ---------------------------------------------------------------------------
class TestResolve(_Authed):
    def _resolve(self):
        return self._json200("GET", "/api/resolve")

    def test_plans_at_most_three_and_termination(self):
        data = self._resolve()
        self.assertIsInstance(data, dict)
        self.assertIn("plans", data)
        self.assertIn("termination", data)
        self.assertIsInstance(data["plans"], list)
        self.assertLessEqual(len(data["plans"]), 3, "方案最多 3 个（上限，非定值）")
        self.assertIn(data["termination"], _TERMINATION, f"termination 非法：{data['termination']!r}")

    def test_plan_fields_are_full(self):
        data = self._resolve()
        for plan in data["plans"]:
            self.assertIsInstance(plan, dict)
            for key in ("index", "cost", "diff_text", "impact", "moves", "warnings"):
                self.assertIn(key, plan, f"方案缺字段 {key}")
            self.assertIsInstance(plan["index"], int)
            self.assertIsInstance(plan["cost"], (int, float))
            self.assertIsInstance(plan["diff_text"], str)
            self.assertTrue(plan["diff_text"].strip(), "diff_text 必填（人话说明改动）")
            impact = plan["impact"]
            self.assertIsInstance(impact, dict)
            self.assertIsInstance(impact.get("schedule_count"), int)
            self.assertIsInstance(impact.get("object_count"), int)
            self.assertIsInstance(plan["moves"], list)
            self.assertIsInstance(plan["warnings"], list)

    def test_move_mask_is_hex_text(self):
        data = self._resolve()
        for plan in data["plans"]:
            for mv in plan["moves"]:
                self.assertIsInstance(mv, dict)
                self.assertIn("schedule_id", mv)
                self.assertIn("title", mv)
                for endpoint in ("from", "to"):
                    self.assertIn(endpoint, mv, f"move 缺 {endpoint}")
                    node = mv[endpoint]
                    self.assertIsInstance(node, dict)
                    self.assertTrue(_is_iso_date(node.get("date")), f"move.{endpoint}.date 非法")
                    self.assertTrue(
                        _is_hex_text(node.get("slots")),
                        f"move.{endpoint}.slots 不是十六进制文本",
                    )

    def test_warning_shape(self):
        data = self._resolve()
        for plan in data["plans"]:
            for w in plan["warnings"]:
                self.assertIsInstance(w, dict)
                self.assertIn("type", w)
                self.assertIn("message", w)
                self.assertIsInstance(w["type"], str)
                self.assertIsInstance(w["message"], str)

    def test_plans_are_deduplicated(self):
        data = self._resolve()
        serialized = [json.dumps(p, sort_keys=True, ensure_ascii=False) for p in data["plans"]]
        self.assertEqual(len(serialized), len(set(serialized)), "方案必须去重")


# ---------------------------------------------------------------------------
# §7 POST /api/demands
# ---------------------------------------------------------------------------
class TestDemands(_Authed):
    def test_demands_echoes_raw_text(self):
        raw = "下周三下午实验室A我要用，必须安排两节课"
        data = self._json200("POST", "/api/demands", {"raw_text": raw, "hard": True})
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("raw_text"), raw, "响应必须原样回显 raw_text（用户原话）")
        self.assertIs(data.get("ok"), True)
        self.assertIsInstance(data.get("demand_id"), str)
        self.assertTrue(data["demand_id"])
        self.assertIsInstance(data.get("parsed"), dict)
        self.assertIsInstance(data.get("conflicts"), list)
        self.assertIsInstance(data.get("message"), str)
        self.assertTrue(data["message"].strip(), "绝不静默失败：message 不能为空")


if __name__ == "__main__":
    unittest.main(verbosity=2)
