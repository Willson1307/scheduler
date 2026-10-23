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


def _request_raw(method: str, path: str, body=None, token=None, timeout=5.0):
    """同 _request，但额外返回响应头（键统一小写）。

    供契约「通用约定」里只能看响应头才能断言的条款用，
    例如「未登录统一 401（响应体不带 WWW-Authenticate 头）」。
    """
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
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, _parse(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, _parse(e.read())
    except OSError as e:  # 连接拒绝 / 超时等（URLError 是其子类）
        raise _Unreachable(repr(e)) from e


# 模块级共享状态：setUpModule 里探测健康 + 登录一次，供各测试复用。
_TOKEN = None
_DEFAULT_GROUP = None
_LOGIN_ERR = None
# 🔴 服务端的「今天」。config.py 的铁律是「凡是需要今天的地方一律读 cfg.today，
# 禁止直接调 date.today()」——测试同样受这条约束约束：服务可用 --today /
# SCHEDULER_TODAY 注入演示日期（现场演示要能复现同一结果），若测试用机器日期
# 断言，服务一注入日期就误报失败。2026-10-04 实测踩过：服务 --today 2026-09-08
# 时 test_default_week 报 `2026-09-07 != 2026-09-28`，看起来像实现 bug，
# 实际是测试用错了日期来源。改为向 /api/health 的 server_date 要答案。
_SERVER_DATE = None


def server_today() -> date:
    """服务端视角的「今天」。拿不到时退回机器日期（并由调用方断言去暴露异常）。"""
    return _SERVER_DATE if _SERVER_DATE is not None else date.today()


def setUpModule():
    global _TOKEN, _DEFAULT_GROUP, _LOGIN_ERR, _SERVER_DATE
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
    # 1.5) 记下服务端「今天」——供涉及"本周/缺省"口径的测试断言使用。
    # health 多返了 server_date 正是为了这个（详见 api_health.py 注释与 docs/20 §5.2.2）。
    if _is_iso_date(body.get("server_date")):
        _SERVER_DATE = date.fromisoformat(body["server_date"])
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
        today = server_today()
        self.assertEqual(
            monday,
            today - timedelta(days=today.weekday()),
            "week 缺省应=本周（含服务端今天的那一周）",
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
        # ⚠️ 已知缺口（2026-10-04）：/api/demands 属 B5（意图解析），
        # 契约 §7 已冻结但src/ 下零实现 → 本项 404 标红。
        #
        # 🔴 这里**故意不** skip、不 expectedFailure：模块 docstring 的设计原则是
        # 「服务已启动但接口未实现 → 如实标红，把后端卡住」。给它加 skip 等于
        # 把红灯变绿灯，B5 就再也没人会注意到——这正是本项目吃过 6 次的
        # 「静默失效」套路。红灯本身是提醒，不是缺陷。
        # B5 落地后本项应自动转绿，无需改测试。
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


# ---------------------------------------------------------------------------
# 通用约定 · 鉴权与拒绝语义（401 / 伪 token / 组越权 403 / 授权组数据收窄）
# ---------------------------------------------------------------------------
class TestAuthRejection(_Authed):
    """契约「通用约定」+ §10 的鉴权与拒绝语义（2026-10-21 补，断言逐条对应原文）。

    对应的契约原文（docs/07-API契约.md）：
      - 通用约定：「认证：`Authorization: Bearer <token>`；未登录统一 **401**
        （响应体**不带** `WWW-Authenticate` 头）」
      - 通用约定：「统一错误体：`{"error":{"code":"...","message":"..."}}`」
      - 通用约定：「当前组：`?group=<group_id>` 或 `X-Group` 请求头」
        （X-Group 头后端未实现，不在本测试范围）
      - 通用约定：「拒绝语义：越权访问具体资源 → 404 ｜ 档位不足 → 403 ｜
        参数不合法 → 400」——其中「越权访问具体资源 → 404」指**按 id 访问
        具体资源**（如 §11 exceptions 的 schedule_id）；`?group=` 是**范围选择
        参数**，越权属 §10 显式写明的 403（「group 越权 403」），两种语义不混。
      - §10 /api/ping 鉴权行：「必须登录（Bearer token），未登录 401；
        `group` 越权 403」

    🔴 参数名防假绿：当前组的查询参数名是 `group`（通用约定原文）。
    写错成 `?group_id=` 时后端会**静默忽略**该参数并回退缺省组、返回 200——
    测试会假绿。因此「200 且数据属于指定组」的断言必须验证数据确实收窄到
    该组闭包（严格小于缺省全量），参数名写错时才会变红（破坏法验证见交付报告）。

    载体账号：admin（全组织可见）+ t01（演示库教师，seeds.py 按任教关系
    仅授权「高一(1)班」）——admin 有全组织短路，测不出组越权，越权用例
    必须用授权范围受限的教师 token。非演示库（无 t01）时相关用例跳过。
    """

    def test_missing_token_gets_401_with_contract_error_body(self):
        # 通用约定：未登录统一 401 + 统一错误体 + 不带 WWW-Authenticate 头。
        # 覆盖 §3 objects / §4 schedules / §5 conflicts / §10 ping 四个接口。
        cases = [
            ("GET", "/api/objects", None),
            ("GET", "/api/schedules", None),
            ("GET", "/api/conflicts", None),
            ("POST", "/api/ping", {"event": "task_start", "session_id": "contract-noauth"}),
        ]
        for method, path, body in cases:
            with self.subTest(f"{method} {path} 未带 Authorization"):
                try:
                    st, headers, data = _request_raw(method, path, body=body, token=None)
                except _Unreachable as e:
                    self.skipTest(f"服务不可达：{e}")
                self.assertEqual(
                    st, 401,
                    f"{method} {path} 未带 Authorization 应 401（通用约定），"
                    f"实际 {st}，响应={data!r}",
                )
                self._assert_contract_error_body(data, f"{method} {path}")
                self.assertNotIn(
                    "www-authenticate", headers,
                    "通用约定：401 响应不带 WWW-Authenticate 头",
                )

    def test_garbage_token_gets_401(self):
        # 伪 token 与未登录同语义 → 401（通用约定「认证」条）。
        # 两种形态：无点分隔的乱串 / 结构合法但签名不符。
        for token in ("not-a-real-token", "eyJmYWtlIjoidHJ1ZSJ9.aW52YWxpZF9zaWdu"):
            with self.subTest(f"伪 token {token[:12]}..."):
                try:
                    st, data = _request("GET", "/api/objects", token=token)
                except _Unreachable as e:
                    self.skipTest(f"服务不可达：{e}")
                self.assertEqual(
                    st, 401,
                    f"伪 token 应 401（通用约定），实际 {st}，响应={data!r}",
                )
                self._assert_contract_error_body(data, "伪 token")

    def test_unauthorized_group_param_gets_403(self):
        # §10「group 越权 403」：token 有效，但 ?group= 指向自己没有授权的组。
        # 覆盖 §3 objects（query 传 group）/ §4 schedules（query 传 group）/
        # §10 ping（请求体传 group）三个入口。
        t_token = self._teacher_login()[0]
        try:
            st, groups = _request("GET", "/api/groups", token=t_token)
        except _Unreachable as e:
            self.skipTest(f"服务不可达：{e}")
        self.assertEqual(st, 200, f"教师 GET /api/groups 期望 200，实际 {st}")
        teacher_ids = {g["group_id"] for g in groups}
        self.assertTrue(
            teacher_ids, "教师至少应看到自己任教的班级（P0-1 修复后演示库非空）"
        )
        # 从 admin 可见的组里挑一个教师未授权的（组存在、但不属于教师授权范围）
        admin_groups = self._json200("GET", "/api/groups")
        admin_ids = {g["group_id"] for g in admin_groups}
        unauthorized = sorted(admin_ids - teacher_ids)
        if not unauthorized:
            self.fail("演示库中找不到教师未授权的组，无法测 ?group= 越权（数据不合预期）")
        target = unauthorized[0]
        # §3 /api/objects?group=<未授权组>
        try:
            st, data = _request("GET", "/api/objects?group=" + target, token=t_token)
        except _Unreachable as e:
            self.skipTest(f"服务不可达：{e}")
        self.assertEqual(
            st, 403,
            f"?group= 指向未授权组应 403（§10『group 越权 403』），"
            f"实际 {st}，响应={data!r}",
        )
        # §4 /api/schedules?group=<未授权组>
        try:
            st, data = _request("GET", "/api/schedules?group=" + target, token=t_token)
        except _Unreachable as e:
            self.skipTest(f"服务不可达：{e}")
        self.assertEqual(
            st, 403,
            f"?group= 指向未授权组应 403（§10『group 越权 403』），"
            f"实际 {st}，响应={data!r}",
        )
        # §10 POST /api/ping（group 在请求体里）
        try:
            st, data = _request(
                "POST", "/api/ping",
                body={"event": "task_start", "session_id": "contract-authz", "group": target},
                token=t_token,
            )
        except _Unreachable as e:
            self.skipTest(f"服务不可达：{e}")
        self.assertEqual(
            st, 403,
            f"ping 的 group 越权应 403（§10 鉴权行），实际 {st}，响应={data!r}",
        )

    def test_authorized_group_returns_scoped_data(self):
        # 通用约定「当前组：?group=<group_id>」+ §3「只返回当前组闭包内的对象」：
        # 显式指定组 → 200，且数据确实收窄到该组闭包（不是缺省组的全量）。
        root = self._json200("GET", "/api/objects")
        root_ids = {o["id"] for o in root}
        groups = self._json200("GET", "/api/groups")
        subclasses = [g for g in groups if g["parent_name"] is not None]
        self.assertTrue(subclasses, "演示库应有非根组（班级），否则无法验证 ?group= 收窄")
        gid = subclasses[0]["group_id"]
        data = self._json200("GET", "/api/objects?group=" + gid)
        scoped_ids = {o["id"] for o in data}
        self.assertTrue(scoped_ids, "班级组闭包内至少应有对象（班级自身 + 学生）")
        self.assertTrue(
            scoped_ids <= root_ids,
            "指定组返回的对象必须仍在根组闭包内（同一演示库）",
        )
        self.assertLess(
            len(scoped_ids), len(root_ids),
            "指定班级组时不得返回与缺省根组相同的数据"
            "（防 ?group= 参数名写错被后端忽略、静默回退缺省组 → 假绿）",
        )
        # §4 /api/schedules?group=<授权组> → 200 且结构完整（与 403 用例成对照，
        # 证明 403 来自授权判定）。⚠️ 契约 §4 未规定 grid 按组收窄（§3 才有
        # 「只返回当前组闭包内的对象」），实现上网格按组织输出，故此处不断言
        # grid 的组归属——那是契约没写的口径，留给队长拍板（见交付报告）。
        sched = self._json200("GET", "/api/schedules?group=" + gid)
        for key in ("objects", "days", "grid"):
            self.assertIn(key, sched, f"schedules?group= 响应缺 {key}")

    def test_teacher_authorized_group_200(self):
        # 对照用例：教师访问**自己授权**的组 → 200，证明上面 403 用例的 403
        # 确实来自授权判定，而不是「该接口/参数对教师一律拒绝」。
        t_token, t_default = self._teacher_login()
        self.assertIsNotNone(t_default, "教师登录响应应含 default_group_id")
        try:
            st, data = _request("GET", "/api/objects?group=" + t_default, token=t_token)
        except _Unreachable as e:
            self.skipTest(f"服务不可达：{e}")
        self.assertEqual(
            st, 200,
            f"教师访问自己授权的组应 200，实际 {st}，响应={data!r}",
        )
        self.assertTrue(data, "自己班级闭包内至少应有对象")
        # §10 ping：group 给自己授权的组 → 200（与上面 ping 403 成对照）
        try:
            st, data = _request(
                "POST", "/api/ping",
                body={"event": "task_start", "session_id": "contract-authz-ok",
                      "group": t_default},
                token=t_token,
            )
        except _Unreachable as e:
            self.skipTest(f"服务不可达：{e}")
        self.assertEqual(
            st, 200,
            f"ping 带授权 group 应 200，实际 {st}，响应={data!r}",
        )

    # -- 辅助 ------------------------------------------------------------------

    def _teacher_login(self):
        """登录演示库教师 t01（口令见 seeds.py；授权仅限任教班级）。

        非演示库（无 t01 账号）→ 跳过：组越权语义需要授权范围受限的账号，
        admin 有全组织短路测不出 403。
        """
        try:
            st, body = _request("POST", "/api/login", {"login": "t01", "password": "123456"})
        except _Unreachable as e:
            self.skipTest(f"服务不可达：{e}")
        if st != 200 or not isinstance(body, dict) or not body.get("token"):
            self.skipTest(
                f"演示库教师 t01 登录失败（{st} {body!r}）——"
                "组越权用例需要教师账号，请用演示库（data/demo.db）起服务重试"
            )
        return body["token"], body.get("default_group_id")

    def _assert_contract_error_body(self, data, where):
        """通用约定：统一错误体 `{"error":{"code":"...","message":"..."}}`。"""
        self.assertIsInstance(
            data, dict, f"{where}：错误响应应是 JSON 对象，实际 {data!r}"
        )
        err = data.get("error")
        self.assertIsInstance(
            err, dict, f"{where}：error 应为对象（统一错误体），实际 {err!r}"
        )
        self.assertIsInstance(err.get("code"), str, f"{where}：error.code 应为字符串")
        self.assertTrue(err["code"], f"{where}：error.code 不能为空")
        self.assertIsInstance(err.get("message"), str, f"{where}：error.message 应为字符串")


if __name__ == "__main__":
    unittest.main(verbosity=2)
