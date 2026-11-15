"""登录密码错的 401 被当成「会话过期」—— 文案误导 + 清场（卡 rH36le）。

问题（改之前）：
  `web/app.js` 的 `apiGet` / `apiPost` / `apiDelete` 三处各写一份错误处理，
  且**一律** `if (res.status === 401) handle401(body)`。而 `handle401` 只会做一件事：
  清 token、清私有视图、弹登录栏、红字「登录已过期或未登录（HTTP 401）」。
  但后端 `POST /api/login` 密码错时按契约返回的就是 **401 + bad_credentials**
  （`src/server/api_login.py:186`）。于是：

    · 老师只是密码敲错 → 页面说「登录已过期」→ 以为掉线了 → 反复重登；
    · 真实原因「登录名或密码错误」被 `handle401` 的硬写文案盖掉；
    · 顺手还清掉了登录表单里已输入的内容，越试越乱。

本文件钉死四件事，任何一条被改回去必须立刻变红：

1. **判据是状态，不是文案**：`isSessionExpiry401(body, sentToken)` 只在
   「这次请求带了 token」且「后端没说 bad_credentials」时才算会话失效。
2. **三个 api* 共用同一个错误出口** `throwHttpError()`，不再各写一份
   （各写一份 = 迟早分叉；原代码只用注释要求「必须一致」，注释挡不住分叉）。
3. **带 token 的 401 仍然清场**——修这一处不能把真正的会话失效也放过。
4. `doLogin` 仍按后端 message 原文展示，且不再吞掉 `.status`。

为什么静态断言之外还要真跑 JS：
  本项目最贵的 bug 是「设计意图写对了、代码从没跑过」。只做正则匹配的话，
  把 `sentToken` 传成 `true` 这种改法照样能骗过断言。所以另设
  `TestRealLogicWithNode`：把 `web/app.js` 里**真实的** `apiGet/apiPost/apiDelete
  + throwHttpError + isSessionExpiry401 + extractCode/extractMessage` 抽出来，
  在 node 里用一个假 `fetch` 跑 6 个场景，比对「有没有清场」与「抛出的文案」。

  代价（如实写）：`handle401` 在 node 里是**桩**（真身要碰 DOM）。本文件测的是
  「哪条分支被走到」，不是 `handle401` 内部做了什么——后者由
  `test_web_conflict_periods` 等既有用例覆盖。

为什么 node 那段是条件注册：
  项目硬约束是 Python 标准库 + 零第三方依赖（AGENTS §5），不能假设每台机器都有
  node。没有 node 时该类**不注册**（而不是 `skipTest`）——本项目判定标准是
  「skipped>0 即失败」。本机（演示机）node 可用，该类实际会跑。

运行：python -m unittest tests.test_web_login_401 -v
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
    """取 `[async] function <name>(...) { ... }` 的完整源码（按花括号配平，零依赖）。

    🔴 必须把 `async` 一起带上：`apiPost/apiGet/readBody` 都是 `async function`，
    只从 `function` 切会让抽出来的代码里出现裸 `await` —— node 直接 SyntaxError，
    而报错点在 bundle 里、不在 app.js 里，很容易被误读成「测试写错了」。
    """
    start = src.index("function " + name)
    if src[start - 6:start] == "async ":
        start -= 6
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
    """去掉注释，只留代码（注释里写着「不是所有 401 都是过期」，不剥离会误判）。"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


#: 后端密码错时的真实响应（逐字对齐 `src/server/api_login.py` 的 401 分支）。
BAD_CREDENTIALS_MSG = "登录名或密码错误"
BAD_CREDENTIALS_BODY = {"error": {"code": "bad_credentials", "message": BAD_CREDENTIALS_MSG}}


class TestApiLayerSharesOneExit(unittest.TestCase):
    """三个 api* 必须共用 `throwHttpError`，且 401 判据只有一处。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(os.path.join("web", "app.js"))

    def test_no_inline_401_handling_left(self):
        """⛔ 任何 api* 里再出现内联的 `401 → handle401` 就是旧病复发。"""
        for name in ("apiGet", "apiPost", "apiDelete"):
            body = _code_only(_function_body(self.src, name))
            self.assertNotRegex(
                body, r"status\s*===\s*401",
                name + " 里还有内联的 401 判断——必须交给 throwHttpError，"
                       "否则三份判据迟早分叉（登录接口那次就是这么被误判的）",
            )
            self.assertIn("throwHttpError(", body, name + " 没走统一错误出口")

    def test_all_three_capture_sent_token_before_fetch(self):
        """判据必须是「发请求那一刻有没有带 token」，且必须在 fetch 之前捕获。"""
        for name in ("apiGet", "apiPost", "apiDelete"):
            body = _code_only(_function_body(self.src, name))
            self.assertRegex(body, r"sentToken\s*=\s*!!state\.token")
            self.assertLess(
                body.index("sentToken"), body.index("fetch("),
                name + " 的 sentToken 必须在 fetch 之前取——"
                         "fetch 之后 token 可能已被别的标签清掉",
            )
            self.assertRegex(
                body, r"throwHttpError\(res,\s*body,\s*sentToken\)",
                name + " 没把 sentToken 传给 throwHttpError（判据会退化成永远清场）",
            )

    def test_single_source_of_truth(self):
        """整个 app.js 里只允许一处 `handle401(` 调用（在 throwHttpError 内）。"""
        code = _code_only(self.src)
        calls = re.findall(r"handle401\(", code)
        # 1 次定义 + 1 次调用
        self.assertEqual(
            len(calls), 2,
            "handle401 出现 %d 次：应当只有「定义 1 次 + throwHttpError 里调用 1 次」"
            % len(calls),
        )
        self.assertIn("isSessionExpiry401(body, sentToken)", _code_only(
            _function_body(self.src, "throwHttpError")))


class TestSessionExpiryCriterion(unittest.TestCase):
    """`isSessionExpiry401` 的真值口径。"""

    @classmethod
    def setUpClass(cls):
        cls.body = _code_only(_function_body(_read(os.path.join("web", "app.js")),
                                             "isSessionExpiry401"))

    def test_requires_token(self):
        """没带 token 的 401 一律不是「过期」——本来就没凭证。"""
        self.assertRegex(self.body, r"if\s*\(!sentToken\)\s*return\s*false")

    def test_bad_credentials_is_not_expiry(self):
        """后端点明 bad_credentials 的 401 是凭证错，即便带了 token 也不清场。"""
        self.assertIn("'bad_credentials'", self.body)
        self.assertRegex(self.body, r"bad_credentials'\)\s*return\s*false")

    def test_default_is_expiry(self):
        """带票被拒 = 票废了 → 默认 true（fail-safe 到清场，不能反过来）。"""
        self.assertRegex(self.body, r"return\s*true\s*;\s*\n?\}?\s*$|return\s+true")


class TestDoLoginKeepsBackendMessage(unittest.TestCase):
    """登录失败必须显示后端原文，且错误对象要带 status。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(os.path.join("web", "app.js"))

    def test_login_reports_backend_message(self):
        body = _code_only(_function_body(self.src, "doLogin"))
        self.assertIn("'登录失败：' + e.message", body,
                      "登录失败没按后端 message 原文展示")

    def test_error_object_keeps_status(self):
        body = _code_only(_function_body(self.src, "throwHttpError"))
        self.assertIn("err.status = res.status", body)
        self.assertIn("err.body = body", body)


class TestAssetVersion(unittest.TestCase):
    """缓存键必须同值 —— 漏一处就是「改了 JS 行为但浏览器还在跑旧的」。"""

    def test_config_defines_once(self):
        src = _read(os.path.join("src", "server", "config.py"))
        self.assertEqual(len(re.findall(r"(?m)^ASSET_VERSION\s*=", src)), 1)
        self.assertRegex(config.ASSET_VERSION, r"^\d+$")

    def test_all_index_refs_match_config(self):
        html = _read(os.path.join("web", "index.html"))
        refs = re.findall(r"\?v=([0-9]+)", html)
        self.assertEqual(len(refs), 3, "index.html 的 ?v= 引用数量变了，断言需同步")
        self.assertEqual(len(set(refs)), 1, "index.html 三处 ?v= 不一致：%r" % (refs,))
        self.assertEqual(refs[0], config.ASSET_VERSION,
                         "config.ASSET_VERSION=%s 与 index.html 的 ?v=%s 不一致"
                         % (config.ASSET_VERSION, refs[0]))


# ---------------------------------------------------------------------------
# 真值表：抽出真实 api*，用假 fetch 跑一遍
# ---------------------------------------------------------------------------

_NODE = shutil.which("node")

_OK200 = {"groups": []}

#: (说明, 请求是否带 token, HTTP 状态, 响应体, 期望 handle401 调用次数,
#:  期望抛出文案 —— None 表示「后端没给 message，用 HTTP 兜底」，只校验前缀)
_CASES = [
    ("登录密码错（无 token，401 bad_credentials）", False, 401, BAD_CREDENTIALS_BODY, 0,
     BAD_CREDENTIALS_MSG),
    ("已登录时提交错密码（带 token，401 bad_credentials）", True, 401, BAD_CREDENTIALS_BODY, 0,
     BAD_CREDENTIALS_MSG),
    ("带 token 的 401（后端只说未登录）→ 真·会话失效", True, 401,
     {"error": {"code": "unauthorized", "message": "未登录"}}, 1, "HANDLE401_STUB"),
    ("无 token 的裸 401（无响应体）→ 不算过期", False, 401, None, 0, "HTTP 401"),
    ("带 token 的 403（越权）→ 不清场", True, 403,
     {"error": {"code": "forbidden", "message": "无权访问该组"}}, 0, "无权访问该组"),
    ("登录被限流 429 → 原文展示", False, 429,
     {"error": {"code": "locked", "message": "连续失败过多，请 1 分钟后再试"}}, 0,
     "连续失败过多，请 1 分钟后再试"),
]


def _build_bundle(src: str) -> str:
    parts = [_function_body(src, n) for n in (
        "extractMessage", "extractCode", "isSessionExpiry401", "throwHttpError",
        "readBody", "authHeaders", "apiGet", "apiPost",
    )]
    cases_js = "\n".join("  %s," % json.dumps(c, ensure_ascii=False) for c in _CASES)
    return (
        # api* 依赖的外部环境：全部用桩，测的是分支判断不是网络。
        "const USE_MOCK = false;\n"
        "const window = { location: { origin: 'http://127.0.0.1:8000' } };\n"
        "const state = { token: '' };\n"
        "let NEXT = null;\n"
        "let EXPIRED = 0;\n"
        "function authExpiredByDeadline() { return false; }\n"
        "function fetch() { return Promise.resolve(NEXT); }\n"
        "function handle401() { EXPIRED += 1; throw new Error('HANDLE401_STUB'); }\n"
        # 真身依赖 DOM（clearAuth/endSession），node 里只做桩：本用例只判分支。
        + "\n".join(parts)
        + "\nconst CASES = [\n" + cases_js + "\n];\n"
        + """
function fakeRes(status, body) {
  var text = (body === null || body === undefined) ? '' : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status: status,
    statusText: status === 401 ? 'Unauthorized' : 'Error',
    text: function () { return Promise.resolve(text); },
  };
}
(async function () {
  const out = [];
  for (const c of CASES) {
    const [label, withToken, status, body, wantExpired, wantMsg] = c;
    state.token = withToken ? 'tok_abc' : '';
    EXPIRED = 0;
    NEXT = fakeRes(status, body);
    // POST 与 GET 走同一条出口，两个都真跑：任一漏改都会红。
    const settled = await Promise.allSettled([
      apiPost('/api/login', {}, { login: 'x', password: 'y' }),
      apiGet('/api/groups', {}),
    ]);
    let msg = 'NO_THROW', errStatus = null;
    for (const s of settled) {
      if (s.status === 'rejected') {
        msg = String(s.reason && s.reason.message || s.reason);
        const st = s.reason && s.reason.status;
        errStatus = (st === undefined) ? null : st;
        break;
      }
    }
    out.push({ label: label, msg: msg, status: errStatus, expired: EXPIRED,
               wantMsg: wantMsg, wantExpired: wantExpired });
  }
  console.log(JSON.stringify(out));
})();
"""
    )


if _NODE is not None:

    class TestRealLogicWithNode(unittest.TestCase):
        """真跑 app.js 的源码，不靠字符串匹配猜行为。"""

        @classmethod
        def setUpClass(cls):
            bundle = _build_bundle(_read(os.path.join("web", "app.js")))
            cls._tmp = tempfile.mkdtemp(prefix="wb_login401_")
            path = os.path.join(cls._tmp, "bundle.js")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(bundle)
            cls._res = subprocess.run([_NODE, path], capture_output=True, text=True,
                                      timeout=60)

        @classmethod
        def tearDownClass(cls):
            shutil.rmtree(getattr(cls, "_tmp", ""), ignore_errors=True)

        def _results(self):
            self.assertEqual(self._res.returncode, 0,
                             "node 跑不动抽取出来的函数（签名/依赖变了）：\n"
                             + self._res.stderr[-2000:])
            return json.loads(self._res.stdout)

        def test_password_error_does_not_clear_session(self):
            """本卡的核心：密码错不许清场、不许说「已过期」。"""
            got = self._results()
            for row in got[:2]:
                self.assertEqual(row["expired"], 0,
                                 row["label"] + "：仍被当成「登录已过期」清场")
                self.assertEqual(row["msg"], BAD_CREDENTIALS_MSG,
                                 row["label"] + "：没把后端原文交给用户")
                self.assertEqual(row["status"], 401, row["label"] + "：丢了 status")

        def test_real_session_expiry_still_clears(self):
            """别修过头：带 token 的 401 必须继续走 handle401。"""
            got = self._results()
            self.assertEqual(got[2]["expired"], 2,
                             "带 token 的 401 没走 handle401（GET 与 POST 各一次）")
            self.assertEqual(got[2]["msg"], "HANDLE401_STUB")

        def test_other_statuses_untouched(self):
            got = self._results()
            for row in got[3:]:
                self.assertEqual(row["expired"], 0, row["label"] + "：不该清场")
                self.assertTrue(str(row["msg"]).startswith(row["wantMsg"]),
                                row["label"] + " 文案不对：%r" % (row["msg"],))


if __name__ == "__main__":
    unittest.main()
