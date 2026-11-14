"""L-17（rtFhoL）验收测试：b_llm_client.polish（问答层第 2 层，契约签名冻结）。

运行：python -m unittest tests.test_llm_client -v

---------------------------------------------------------------------------
🔴 本文件钉死的口径（每条对应卡面验收 + 一个静默失效风险）
---------------------------------------------------------------------------

  1. **未配置 = 不启用 = 零外联**：BASE/KEY 任一缺失 → 立即返 None，
     urlopen 调用次数必须为 0（patch 断言）。这条守住 smoke.py --offline
     外联为 0 的底线：不配 env 的部署绝对不出网。
  2. **失败只降级、不抛、不 retry**：非 200 / 响应体非 JSON / socket.timeout /
     连接被拒，全部返 None。api_chat 的三层降级依赖这个语义——抛一个异常
     出去整个问答接口就 500 了，等于把「不接 AI 也能答」破坏掉。
  3. **timeout 真传**：timeout_s 必须原样出现在 urlopen 的调用参数里。
     只在 docstring 写 8 秒、实际不限时 = 静默挂死风险，inspect call args
     钉死（不真等 8 秒）。
  4. **跨人契约（api_chat 侧已按此写）**：URL 拼接 {BASE}/chat/completions、
     body 的 model/messages/temperature 结构、系统提示原文、Authorization
     头格式，全部断言——两边谁跑偏，联调前就被这里拦下。

测试全部 mock 或连 127.0.0.1:1（必拒端口），**不访问任何真实 LLM 服务**，
key 一律用假值 sk-xxxx，真实凭据永不进测试代码。
"""

import json
import os
import socket
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from server import b_llm_client as C  # noqa: E402

_ENV_KEYS = ("SCHEDULER_LLM_BASE", "SCHEDULER_LLM_KEY", "SCHEDULER_LLM_MODEL")
_FAKE_KEY = "sk-xxxx"  # 假 key：仅用于让「已配置」分支成立，不是真凭据


def _make_response(status=200, body=b"{}"):
    resp = mock.MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _ok_body(content="手册说：先到冲突列表点一键消解。"):
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    ).encode("utf-8")


class PolishEnvTestCase(unittest.TestCase):
    """env 配置语义：未配置短路 / 请求体契约 / timeout 传递。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["SCHEDULER_LLM_BASE"] = "https://llm.example.invalid/v4"
        os.environ["SCHEDULER_LLM_KEY"] = _FAKE_KEY

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # -- 验收 1：不配 env → None，urlopen 一次都不调 -------------------------

    def test_unconfigured_returns_none_without_any_socket(self):
        combos = [
            ("缺 BASE", {"SCHEDULER_LLM_KEY": _FAKE_KEY}),
            ("缺 KEY", {"SCHEDULER_LLM_BASE": "https://llm.example.invalid/v4"}),
            ("空 KEY", {"SCHEDULER_LLM_BASE": "https://llm.example.invalid/v4",
                        "SCHEDULER_LLM_KEY": ""}),
            ("空 BASE", {"SCHEDULER_LLM_BASE": "",
                         "SCHEDULER_LLM_KEY": _FAKE_KEY}),
        ]
        for label, env in combos:
            for k in _ENV_KEYS:
                os.environ.pop(k, None)
            os.environ.update(env)
            with mock.patch("urllib.request.urlopen") as urlopen:
                result = C.polish("怎么消解冲突？", ["第一条资料", "第二条资料"])
            self.assertIsNone(result, msg=f"{label} 时必须返 None")
            self.assertEqual(
                urlopen.call_count, 0,
                msg=f"{label} 时一次 socket 都不能开（smoke --offline 底线）",
            )

    # -- 验收 4：timeout 真传给 urlopen（inspect call args，不真等） --------

    def test_timeout_is_really_passed_to_urlopen(self):
        with mock.patch("urllib.request.urlopen", return_value=_make_response(
                body=_ok_body())) as urlopen:
            C.polish("q", ["p"])
        self.assertEqual(urlopen.call_count, 1)
        kwargs = urlopen.call_args.kwargs
        self.assertIn("timeout", kwargs, msg="timeout 必须显式传给 urlopen")
        self.assertEqual(kwargs["timeout"], 8.0, msg="缺省 timeout_s=8.0")

    def test_timeout_s_override_is_passed_through(self):
        with mock.patch("urllib.request.urlopen", return_value=_make_response(
                body=_ok_body())) as urlopen:
            C.polish("q", ["p"], timeout_s=3.5)
        self.assertEqual(urlopen.call_args.kwargs.get("timeout"), 3.5)

    # -- 验收 3：mock 假服务器的三种失败（非 200 / 非 JSON / 超时） ----------

    def test_http_500_returns_none_no_raise(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_make_response(status=500)):
            self.assertIsNone(C.polish("q", ["p"]))

    def test_non_json_body_returns_none_no_raise(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_make_response(body=b"not json")):
            self.assertIsNone(C.polish("q", ["p"]))

    def test_socket_timeout_returns_none_no_raise(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=socket.timeout("timed out")):
            self.assertIsNone(C.polish("q", ["p"]))

    # -- 跨人契约：URL / body 结构 / 系统提示 / 鉴权头 -----------------------

    def test_request_shape_matches_contract(self):
        with mock.patch("urllib.request.urlopen", return_value=_make_response(
                body=_ok_body())) as urlopen:
            C.polish("怎么消解冲突？", ["段落一", "段落二"])
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://llm.example.invalid/v4/chat/completions")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.headers.get("Authorization"), "Bearer " + _FAKE_KEY)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], "glm-4-flash")  # 缺省模型
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1]["role"], "user")
        # 系统提示：契约意图不变（只依据资料、查不到要说不知道、不许编），
        # 但 2026-11-14 改了判据与话术 —— 旧话术「资料里没有就直接说『手册里没有这条』」
        # 让模型把「问法措辞不同」当成「资料里没有」，实测常问胶囊「调课」「永久改课」
        # 点开即拒答。故断言改为：必须明确兜底行为，且**不得再出现旧的固定话术**。
        sp = body["messages"][0]["content"]
        self.assertIn("只根据下面提供的资料回答", sp)
        self.assertIn("不要推测、不要编造步骤或数字", sp)
        self.assertIn("确实找不到相关内容时", sp)
        self.assertNotIn("请找开发者确认", sp)
        # user 内容 = question + 段落拼接
        self.assertEqual(
            body["messages"][1]["content"],
            "怎么消解冲突？" + "\n\n【资料】\n" + "段落一\n段落二",
        )

    def test_model_env_override(self):
        os.environ["SCHEDULER_LLM_MODEL"] = "glm-4-plus"
        with mock.patch("urllib.request.urlopen", return_value=_make_response(
                body=_ok_body())) as urlopen:
            C.polish("q", ["p"])
        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["model"], "glm-4-plus")

    # -- 成功路径：去空白 / 空串视为失败 --------------------------------------

    def test_success_returns_stripped_answer(self):
        with mock.patch("urllib.request.urlopen", return_value=_make_response(
                body=_ok_body(content="  回答正文  \n"))):
            result = C.polish("q", ["p"])
        self.assertEqual(result, "回答正文")

    def test_whitespace_only_answer_is_failure(self):
        with mock.patch("urllib.request.urlopen", return_value=_make_response(
                body=_ok_body(content="   \n\t "))):
            self.assertIsNone(C.polish("q", ["p"]))

    def test_missing_choices_structure_is_failure(self):
        with mock.patch("urllib.request.urlopen", return_value=_make_response(
                body=json.dumps({"error": {"code": "x"}}).encode("utf-8"))):
            self.assertIsNone(C.polish("q", ["p"]))

    def test_content_not_string_is_failure(self):
        with mock.patch("urllib.request.urlopen", return_value=_make_response(
                body=json.dumps({"choices": [{"message": {"content": 123}}]})
                .encode("utf-8"))):
            self.assertIsNone(C.polish("q", ["p"]))


class PolishRealRefusedTestCase(unittest.TestCase):
    """验收 2：真连 127.0.0.1:1（必拒端口）→ 返 None 不抛。

    设 no_proxy 绕过本机代理（否则代理会接管 127.0.0.1 返回 502 伪装，
    见仓库教训）；断言耗时 < timeout_s（8 秒）——本机实测回环连接被拒
    约 2 秒（安全软件对回环 TCP 的固定开销，裸 socket 同样 2 秒），
    卡面写的 <1s 在本机不成立，详见交付报告。
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        self._saved_no_proxy = os.environ.get("no_proxy")
        os.environ["SCHEDULER_LLM_BASE"] = "http://127.0.0.1:1"
        os.environ["SCHEDULER_LLM_KEY"] = _FAKE_KEY
        os.environ["no_proxy"] = "127.0.0.1"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self._saved_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._saved_no_proxy

    def test_connection_refused_returns_none_fast(self):
        t0 = time.monotonic()
        result = C.polish("q", ["p"])
        elapsed = time.monotonic() - t0
        self.assertIsNone(result, msg="连接被拒必须返 None 而不是抛异常")
        self.assertLess(elapsed, 8.0,
                        msg=f"必须在 timeout 内快速失败（实测 {elapsed:.2f}s）")


class CompleteChannelTestCase(unittest.TestCase):
    """v1.1（卡 rHrY9D）：`complete(system, user, timeout_s)` —— 纯底层通道。

    入向（话→规则）与出向（事实→人话）都走它，所以它必须
    「同样短的超时语义、同样零外联前提、同样失败返 None」。
    🔴 `polish` 是它的薄封装 —— 上面那批用例全绿就是等价性的证据。
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS + ("SCHEDULER_LLM",)}
        for k in _ENV_KEYS + ("SCHEDULER_LLM",):
            os.environ.pop(k, None)
        os.environ["SCHEDULER_LLM_BASE"] = "https://llm.example.invalid/v4"
        os.environ["SCHEDULER_LLM_KEY"] = _FAKE_KEY

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_unconfigured_opens_no_socket(self):
        for label, env in (
            ("缺 BASE", {"SCHEDULER_LLM_KEY": _FAKE_KEY}),
            ("缺 KEY", {"SCHEDULER_LLM_BASE": "https://llm.example.invalid/v4"}),
        ):
            for k in _ENV_KEYS + ("SCHEDULER_LLM",):
                os.environ.pop(k, None)
            os.environ.update(env)
            with mock.patch("urllib.request.urlopen") as urlopen:
                self.assertIsNone(C.complete("sys", "user"), label)
            self.assertEqual(urlopen.call_count, 0, f"{label} 时不许开 socket")

    def test_switch_off_opens_no_socket(self):
        """`SCHEDULER_LLM=off` 一处关掉整个 AI 层（key/url 还在也不行）。"""
        for value in ("off", "OFF", "0", "false", "no"):
            os.environ["SCHEDULER_LLM"] = value
            with mock.patch("urllib.request.urlopen") as urlopen:
                self.assertIsNone(C.complete("sys", "user"), value)
            self.assertEqual(urlopen.call_count, 0, f"SCHEDULER_LLM={value} 时必须短路")
            self.assertFalse(C.configured())

    def test_body_uses_given_system_and_user(self):
        with mock.patch("urllib.request.urlopen", return_value=_make_response(
                body=_ok_body())) as urlopen:
            C.complete("SYS-TEXT", "USER-TEXT", timeout_s=2.5)
        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["messages"][0]["content"], "SYS-TEXT")
        self.assertEqual(body["messages"][1]["content"], "USER-TEXT")
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 2.5)

    def test_failures_return_none(self):
        cases = (
            ("500", dict(return_value=_make_response(status=500))),
            ("非 JSON", dict(return_value=_make_response(body=b"nope"))),
            ("缺 choices", dict(return_value=_make_response(
                body=json.dumps({"error": {"code": "x"}}).encode()))),
            ("空串", dict(return_value=_make_response(
                body=_ok_body(content="   ")))),
            ("超时", dict(side_effect=socket.timeout("timed out"))),
        )
        for label, kw in cases:
            with mock.patch("urllib.request.urlopen", **kw):
                self.assertIsNone(C.complete("s", "u"), label)

    def test_polish_is_thin_wrapper(self):
        """polish 的请求体必须与「complete + 固定 system」逐字节一致。"""
        with mock.patch("urllib.request.urlopen", return_value=_make_response(
                body=_ok_body())) as urlopen:
            C.polish("问题", ["资料一", "资料二"], timeout_s=4.0)
        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(
            body["messages"][1]["content"], "问题" + "\n\n【资料】\n" + "资料一\n资料二"
        )
        self.assertIn("只根据下面提供的资料回答", body["messages"][0]["content"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 4.0)


if __name__ == "__main__":
    unittest.main()
