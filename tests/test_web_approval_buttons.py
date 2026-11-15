"""审批按钮「权限口径」验收测试（卡 rrMot2）。

问题（改之前）：
  `reqActions()` 只看「是不是我提的」——
    · 自己提的 → 出「撤回」；
    · **别人提的一律出「同意/拒绝」**，不管我是什么档。
  于是教师账号看别人的 pending 请求也能看见「同意」，一点就 403
  （后端 `b8_kernels.require_approve_in_org` 要 approve 档，teacher 天花板是
  propose）。演示时最伤：老师以为是系统坏了，而不是自己权限不够。

本文件钉死五件事，任何一条被改回去必须立刻变红：

1. `canApproveHere()` 与后端**同源**：admin 先短路（`authz.effective_level`
   对 admin 走全组织短路），否则看 `state.groupLevels` 里**任一组**
   ∈ {approve, own}（`/api/groups` 给的就是含继承与天花板的有效档位）。
2. `reqActions()` 真被这道闸拦住：`!canApproveHere()` → 不出「同意/拒绝」。
3. pending 但没按钮时**必须说明原因**，三种情况分开写，且**终态只读不补说明**。
4. 全程 `textContent`，`web/app.js` 无任何 `innerHTML` 赋值。
5. `ASSET_VERSION` 与 `web/index.html` 三处 `?v=` 同值，且常量只定义一次 ——
   漏一处 = 浏览器继续跑旧 JS，按钮行为跟代码不一致，且**不报错**（静默失效）。

为什么静态断言之外还要真跑一遍 JS：
  本项目最贵的 bug 是「设计意图写对了、代码也从没跑过」（契约测试 `Ran 0 tests`
  却 `OK` 那次）。只做正则匹配的话，`return true;` 这种把闸拆掉改法只要保留了
  几个关键词就能骗过断言。所以另设 `TestRealLogicWithNode`：把 `web/app.js` 里
  **真实的** `canApproveHere / reqActions / reqNoActionNote …` 源码抽出来，
  在 node 里对 9 个场景跑真值表，比对按钮与文案。抽的是源码本身，不是复制品。

为什么 node 那段是条件注册：
  项目硬约束是 Python 标准库 + 零第三方依赖（AGENTS §5），不能假设每台机器都有
  node。没有 node 时该类**不注册**（而不是 `skipTest`）—— 因为本项目判定标准是
  「skipped>0 即失败」，用 skip 会让整套测试永远到不了 `OK`。
  代价（如实写）：无 node 的机器上少了这层真值表保护，只剩静态断言。
  本机（演示机）node v22 可用，该类实际会跑。

运行：python -m unittest tests.test_web_approval_buttons -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from server import config  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _function_body(src: str, name: str) -> str:
    """取 `function <name>(...) { ... }` 的完整源码（按花括号配平，零依赖）。

    与 `tests/test_web_conflict_periods.py` 里的同名辅助同构，故意不抽公共模块：
    抽出去以后三个测试文件互相牵连，改一处要同时改三处。
    """
    start = src.index("function " + name)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("花括号没配平，取不到 " + name + " 的函数体")


def _code_only(js: str) -> str:
    """去掉注释，只留代码。

    必须去注释再扫结构：这些函数的注释里**故意**写着「不看 current 组」「不能用
    字典序」之类的解释文字，不剥离就会把讲解当成实现（逼下一个人删注释）。
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


#: 三类必出的说明文案（逐字全句）。改文案必须先改本断言，防止「顺手改一句」把
#: 演示口径改掉 —— 这三句是现场答辩时讲权限边界的话术。
#: ⚠️ 用**全句**而不是关键词：源码里一旦被拆成 `'…' + '…'` 拼接，整句就消失，
#:    断言立刻变红（本文件初版就被这个坑绊过一次：文案跨行拼接，正则照样过）。
NOTE_TOP = ("这是你自己提出的请求，不能自己批准。你已是本组织最高权限，"
            "没有账号能批准这条——演示时请用教师账号提请求，再由管理员批准。")
NOTE_SELF_PLAIN = "这是你自己提出的请求，不能自己批准。需要级别比你高的账号（管理员）批准。"
NOTE_NO_LEVEL = "你当前的权限档位是「提议」，批准需要「审批」档及以上。"
NOTE_NO_GRANT = "你当前的权限档位是「未授权」，批准需要「审批」档及以上。"


class TestCanApproveHere(unittest.TestCase):
    """`canApproveHere()` 必须与后端 `require_approve_in_org` 同源。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(os.path.join("web", "app.js"))
        cls.body = _code_only(_function_body(cls.src, "canApproveHere"))

    def test_admin_short_circuits(self):
        """admin 不看 group_roles 行 —— 后端 authz 对 admin 走全组织短路。"""
        self.assertIn("global_role === 'admin'", self.body)
        self.assertIn("return true", self.body)

    def test_reads_effective_group_levels(self):
        """非 admin 必须读 `state.groupLevels`（有效档位），不是 global_role。"""
        self.assertIn("state.groupLevels", self.body)

    def test_accepts_approve_and_own_only(self):
        """档位判据只有 {approve, own}；propose 及以下不许放行。"""
        self.assertIn("'approve'", self.body)
        self.assertIn("'own'", self.body)
        self.assertNotIn("'propose'", self.body, "propose 档不能通过审批闸")
        self.assertNotIn("'view'", self.body, "view 档不能通过审批闸")

    def test_no_unconditional_return_true(self):
        """破坏法兜底：函数体不许是「一进来就 return true」。

        允许 `if (...) return true;`（admin 短路），但第一行逻辑就必须是判断，
        不能出现「函数体只有 return true」这种把闸拆掉的写法。
        """
        stripped = re.sub(r"\s+", " ", self.body).strip()
        self.assertNotEqual(
            stripped, "function canApproveHere() { return true; }",
            "canApproveHere 被改成恒 true —— 审批闸失效，教师也会看到「同意」按钮",
        )

    def test_rank_table_is_numeric_not_lexicographic(self):
        """档位比较用 LEVEL_RANK 整数，不用字符串字典序（'own' < 'view'，会反）。"""
        self.assertIn("LEVEL_RANK", self.src)
        rank = re.search(r"const LEVEL_RANK = \{([^}]*)\}", self.src)
        self.assertIsNotNone(rank, "找不到 LEVEL_RANK 表")
        for word, num in (("view", "0"), ("propose", "1"), ("approve", "2"), ("own", "3")):
            self.assertRegex(
                rank.group(1), r"%s:\s*%s" % (word, num),
                "LEVEL_RANK 必须与 authz.LEVEL_ORDER 同序：%s=%s" % (word, num),
            )


class TestReqActionsGate(unittest.TestCase):
    """`reqActions()` 必须真被权限闸拦住，且自提优先于权限闸。"""

    @classmethod
    def setUpClass(cls):
        cls.body = _code_only(_function_body(_read(os.path.join("web", "app.js")), "reqActions"))

    def test_calls_can_approve_here(self):
        self.assertIn("canApproveHere()", self.body)

    def test_gate_is_negated_guard_returning_empty(self):
        """必须是 `if (!canApproveHere()) return [];` 形态 —— 直接 return []，
        不是「把按钮变灰」。灰按钮照样诱导点击，后端照样 403。"""
        self.assertRegex(
            self.body,
            r"!\s*canApproveHere\(\)\s*\)\s*return\s*\[\s*\]\s*;",
            "reqActions 里没看到「!canApproveHere() → return []」这道闸",
        )

    def test_self_request_checked_before_gate(self):
        """自提的人只能撤回（哪怕他是 admin）—— 顺序不能反。"""
        self.assertLess(
            self.body.index("isSelfRequest"), self.body.index("canApproveHere"),
            "isSelfRequest 必须在 canApproveHere 之前判：admin 自己提的也只能撤回",
        )

    def test_terminal_states_have_no_actions(self):
        self.assertRegex(self.body, r"status\s*!==\s*'pending'\)\s*return\s*\[\s*\]\s*;")


class TestNoActionNote(unittest.TestCase):
    """pending 没按钮时必须说明原因；终态只读不解释。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(os.path.join("web", "app.js"))
        cls.note = _code_only(_function_body(cls.src, "reqNoActionNote"))
        cls.item = _code_only(_function_body(cls.src, "renderRequestItem"))

    def test_three_copy_strings_exist(self):
        for s in (NOTE_TOP, NOTE_SELF_PLAIN, NOTE_NO_LEVEL):
            self.assertIn(s, self.note, "说明文案缺失或被改写：" + s)

    def test_copy_splits_top_level_from_plain(self):
        """自提要分两种：我是顶格（没人能批）vs 我还能被上级批。"""
        self.assertIn("isTopLevelHere()", self.note)
        self.assertLess(
            self.note.index("isTopLevelHere()"), self.note.index(NOTE_SELF_PLAIN),
            "必须先判「我是不是顶格」，否则把 own 也说成「等上级批准」（骗人）",
        )

    def test_terminal_states_return_empty(self):
        """终态（applied/rejected/withdrawn）只读，不补说明。"""
        self.assertIn("'pending'", self.note)
        self.assertRegex(
            self.note, r"status[^\n]*!==\s*'pending'[^\n]*return\s*''",
            "reqNoActionNote 必须对非 pending 直接返回空串",
        )

    def test_note_is_rendered_with_textcontent(self):
        """说明行由 renderRequestItem 用 el() + text 输出（textContent，不是 HTML）。"""
        self.assertIn("'req-why'", self.item, "没把说明行挂到 .req-why")
        self.assertIn("textContent", self.src)

    def test_css_class_defined_once(self):
        html = _read(os.path.join("web", "index.html"))
        self.assertEqual(html.count(".req-why {"), 1, "index.html 里 .req-why 应只定义一次")
        self.assertIn("var(--fs-sm)", html.split(".req-why {")[1].split("}")[0])


class TestNoInnerHtml(unittest.TestCase):
    """AGENTS §8：禁 innerHTML，动态文本一律 textContent。"""

    def test_app_js_has_no_innerhtml_assignment(self):
        src = _read(os.path.join("web", "app.js"))
        hits = re.findall(r"\.innerHTML\s*(?:\+)?=", src)
        self.assertEqual(hits, [], "web/app.js 出现 innerHTML 赋值（存储型 XSS 面）")
        for bad in ("insertAdjacentHTML", "document.write", "outerHTML"):
            self.assertNotIn(bad, src)

    def test_new_functions_use_el_only(self):
        src = _read(os.path.join("web", "app.js"))
        for name in ("renderRequestItem", "reqNoActionNote"):
            body = _function_body(src, name)
            self.assertNotIn("innerHTML", body, name + " 里出现 innerHTML")


class TestAssetVersion(unittest.TestCase):
    """缓存键必须同值 —— 漏一处就是「改了按钮行为但浏览器还在跑旧 JS」。"""

    def test_config_defines_once(self):
        src = _read(os.path.join("src", "server", "config.py"))
        hits = re.findall(r"(?m)^ASSET_VERSION\s*=", src)
        self.assertEqual(
            len(hits), 1,
            "config.py 里 ASSET_VERSION 定义了 %d 次 —— 只能留一处，"
            "两行同值也会因为「后者生效」让前者变成骗人的注释" % len(hits),
        )
        self.assertRegex(config.ASSET_VERSION, r"^\d+$")

    def test_all_index_refs_match_config(self):
        html = _read(os.path.join("web", "index.html"))
        refs = re.findall(r"\?v=([0-9]+)", html)
        self.assertEqual(len(refs), 3, "index.html 的 ?v= 引用数量变了，断言需同步")
        self.assertEqual(len(set(refs)), 1, "index.html 三处 ?v= 不一致：%r" % (refs,))
        self.assertEqual(
            refs[0], config.ASSET_VERSION,
            "config.ASSET_VERSION=%s 与 index.html 的 ?v=%s 不一致（漏改一处等于没改）"
            % (config.ASSET_VERSION, refs[0]),
        )


# ---------------------------------------------------------------------------
# 真值表：把 app.js 里的真实函数抽出来，在 node 里跑一遍
# ---------------------------------------------------------------------------

_NODE = shutil.which("node")

#: 9 个场景 → 期望（actions 与说明行）。
#: user 里的 id 固定 u1；req.requester_id = u1 即「我自己提的」。
_CASES = [
    # 1. admin 看别人的 pending → 出「同意/拒绝」
    ({"id": "u1", "global_role": "admin"}, {}, "u2", "pending",
     ["approve", "reject"], ""),
    # 2. 教师（propose）看别人的 pending → 没按钮，并说明档位不够
    ({"id": "u1", "global_role": "teacher"}, {"g1": "propose"}, "u2", "pending",
     [], NOTE_NO_LEVEL),
    # 3. 教师自己提的 → 只有撤回，且要说明「我不能自批、得等上级」
    ({"id": "u1", "global_role": "teacher"}, {"g1": "propose"}, "u1", "pending",
     ["withdraw"], NOTE_SELF_PLAIN),
    # 4. admin 自己提的 → 只有撤回，且说明「没人能批」（自批 403 是规则）
    ({"id": "u1", "global_role": "admin"}, {"g1": "own"}, "u1", "pending",
     ["withdraw"], NOTE_TOP),
    # 5. 非 admin 但有效档 own（演示里授了也封顶 propose，这里是理论顶格）→ 同上
    ({"id": "u1", "global_role": "teacher"}, {"g1": "own"}, "u1", "pending",
     ["withdraw"], NOTE_TOP),
    # 6. 非 admin、approve 档，自己提的 → 必须说「等上级批」，不许说「没人能批」
    ({"id": "u1", "global_role": "teacher"}, {"g1": "approve"}, "u1", "pending",
     ["withdraw"], NOTE_SELF_PLAIN),
    # 7. view 档看别人的 → 没按钮，且如实说「查看」（不能写死「提议」）
    ({"id": "u1", "global_role": "terminal"}, {"g1": "view"}, "u2", "pending",
     [], "你当前的权限档位是「查看」，批准需要「审批」档及以上。"),
    # 8. 终态 → 无按钮、无说明（只读）
    ({"id": "u1", "global_role": "admin"}, {"g1": "own"}, "u2", "applied",
     [], ""),
    # 9. 没有授权组（groupLevels 还没加载）→ fail-closed，不出按钮，且不假称有提议权
    ({"id": "u1", "global_role": "teacher"}, {}, "u2", "pending",
     [], NOTE_NO_GRANT),
]


def _build_bundle(src: str) -> str:
    """拼一个 node 能跑的小脚本：state 由外部给，函数体原样取自 app.js。"""
    parts = []
    for name in ("str", "isSelfRequest", "maxGroupLevel", "isTopLevelHere",
                 "canApproveHere", "reqActions", "reqNoActionNote"):
        parts.append(_function_body(src, name))
    for const in ("LEVEL_RANK", "LEVEL_CN"):
        m = re.search(r"(?m)^const %s = \{[^}]*\};" % const, src)
        if m is None:
            raise AssertionError("app.js 里找不到 const " + const)
        parts.append(m.group(0))

    cases_js = "\n".join(
        "  {user: %s, levels: %s, requester: %s, status: %s},"
        % (json.dumps(u), json.dumps(lv), json.dumps(rq), json.dumps(st))
        for (u, lv, rq, st, _acts, _note) in _CASES
    )
    return (
        "const state = { user: null, groupLevels: {}, groupId: '' };\n"
        + "\n".join(parts)
        + "\nconst CASES = [\n" + cases_js + "\n];\n"
        + """
const out = [];
for (const c of CASES) {
  state.user = c.user;
  state.groupLevels = c.levels;
  const req = { status: c.status, requester_id: c.requester, id: 'cr_1' };
  let acts, note, err = null;
  try {
    acts = reqActions(req);
    note = reqNoActionNote(req);
  } catch (e) {
    acts = null; note = null; err = String(e && e.message || e);
  }
  out.push({ acts: acts, note: note, err: err });
}
console.log(JSON.stringify(out));
"""
    )


if _NODE is not None:

    class TestRealLogicWithNode(unittest.TestCase):
        """真跑 app.js 的源码，不靠字符串匹配猜行为。

        本机有 node 就跑（演示机 node v22）；没 node 的机器上本类不注册，
        理由见模块 docstring。
        """

        @classmethod
        def setUpClass(cls):
            src = _read(os.path.join("web", "app.js"))
            bundle = _build_bundle(src)
            tmp = tempfile.mkdtemp(prefix="wb_req_")
            path = os.path.join(tmp, "bundle.js")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(bundle)
            cls._tmp = tmp
            cls._res = subprocess.run(
                [_NODE, path], capture_output=True, text=True, timeout=60,
            )

        @classmethod
        def tearDownClass(cls):
            shutil.rmtree(getattr(cls, "_tmp", ""), ignore_errors=True)

        def _results(self):
            self.assertEqual(
                self._res.returncode, 0,
                "node 跑不动抽取出来的函数（说明函数签名/依赖变了）：\n"
                + self._res.stderr[-2000:],
            )
            return json.loads(self._res.stdout)

        def test_nine_scenarios(self):
            got = self._results()
            self.assertEqual(len(got), len(_CASES))
            for idx, (case, real) in enumerate(zip(_CASES, got), start=1):
                user, levels, requester, status, want_acts, want_note = case
                where = "场景 %d（%s / %s / %s / %s）" % (
                    idx, user.get("global_role"), levels, requester, status)
                self.assertIsNone(real["err"], where + " 抛异常：" + str(real["err"]))
                self.assertEqual(real["acts"], want_acts, where + " 按钮不对")
                if want_note is None:
                    continue
                self.assertEqual(real["note"], want_note, where + " 说明行不对")

        def test_teacher_never_sees_approve(self):
            """本卡的核心：propose 档看别人的 pending 必须零按钮。"""
            got = self._results()
            self.assertNotIn("approve", got[1]["acts"], "教师仍看到「同意」按钮")
            self.assertNotIn("reject", got[1]["acts"], "教师仍看到「拒绝」按钮")

        def test_admin_can_still_approve_others(self):
            """别把闸修得过头：admin 批别人的请求必须仍有按钮（后端不 403）。"""
            got = self._results()
            self.assertEqual(got[0]["acts"], ["approve", "reject"])


if __name__ == "__main__":
    unittest.main()
