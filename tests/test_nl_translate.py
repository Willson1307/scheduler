"""卡 rHrY9D · v1.1 入向：老师的话 → 规则 IR → 只读查询 → 答案。

运行：python -m unittest tests.test_nl_translate -v

---------------------------------------------------------------------------
🔴 本文件钉死的口径（每条对应卡面一条验收 + 一个静默失效风险）
---------------------------------------------------------------------------

  1. **合法 IR 才走模型路径**：`rule.source=model`、`mode=llm`、答案事实来自后端。
  2. **任何一环不过都回退关键词**：JSON 非法 / intent 不在白名单 / object 库里没有
     / query 不是只读枚举 / 返回内部 ID / 模型超时 —— 全部 `rule.source=keyword`，
     不抛、不 500。
  3. **未配 env 一次 socket 都不开**（离线基线，smoke --offline 外呼为 0 的前提）。
  4. **20 条口语问句**：模型解析正确时意图不丢；关掉 LLM 同一批仍然可答（会降但不断崖）。
  5. **脱敏先于外发**：送给模型的文本里不出现真实班级名 / 姓名 / 内部 ID。

⚠️ 关于「正确率」的口径：本文件**不测模型质量**（那是线上人工试用的事）。
`complete` 是注入的，验的是「模型说对了 → 系统能不能整条链路走通并且不说走样」，
以及「模型说错了 → 系统能不能挡住并降级」。两条都必须可复现，否则就是假绿。

只用 Python 标准库，全部注入 `complete`，不打真网。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from server import api_chat, config  # noqa: E402
from server import b3_kernels as B3  # noqa: E402
from server import b_ai_kernels as K  # noqa: E402
from server import b_llm_client  # noqa: E402
from server import b_nl_kernels as NL  # noqa: E402
from server.config import ServerConfig, ensure_database, set_current_config  # noqa: E402

TODAY = date(2026, 10, 5)  # 周一；演示库冲突落在本周周三
_ENV_KEYS = ("SCHEDULER_LLM_BASE", "SCHEDULER_LLM_KEY", "SCHEDULER_LLM_MODEL",
             "SCHEDULER_LLM", "no_proxy")

#: 判断「这条回答是模型解析路径送来的」—— IR 提示词里有这个角色名。
_IR_MARKER = "意图识别器"


def _ir(intent_id, **slots):
    return json.dumps(
        {"intent_id": intent_id, "slots": slots, "reply": ""}, ensure_ascii=False
    )


def _complete_with(ir_payload):
    """假模型：IR 调用返回指定 payload；润色调用一律返 None（不参与本文件）。"""

    def fn(system, user, timeout_s):
        if _IR_MARKER in system:
            return ir_payload
        return None

    return fn


class _EnvMixin:
    def _save_env(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        NL.reset_cache()

    def _restore_env(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        NL.reset_cache()

    def _enable_llm(self):
        os.environ["SCHEDULER_LLM_BASE"] = "https://llm.example.invalid/v4"
        os.environ["SCHEDULER_LLM_KEY"] = "sk-xxxx"  # 假 key，不是真凭据


def _issue_token(conn, login: str) -> str:
    from server import auth

    row = conn.execute(
        "SELECT id, login, role, object_id FROM users WHERE login = ?", (login,)
    ).fetchone()
    assert row is not None, f"账号 {login} 不存在"
    return "Bearer " + auth.issue_token(
        row["id"], row["login"], row["role"], row["object_id"]
    )


class _ChatCase(_EnvMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="nl-translate-")
        cls.db_path = os.path.join(cls.tmpdir, "demo.db")
        cfg = ServerConfig(db_path=cls.db_path, today=TODAY)
        ensure_database(cfg)
        cls._prev_cfg = config._current
        set_current_config(cfg)

        from scheduler.db import connect

        conn = connect(cls.db_path)
        try:
            cls.admin_token = _issue_token(conn, "admin")
            cls.teacher_token = _issue_token(conn, "t01")
            conn2 = config.open_db()
            try:
                org_id = conn2.execute(
                    "SELECT org_id FROM objects WHERE name='高一(1)班' LIMIT 1"
                ).fetchone()[0]
                payload = B3.conflict_payload(conn2, org_id, config.get_current_config())
            finally:
                conn2.close()
        finally:
            conn.close()

        cls.conflict = (payload.get("items") or [None])[0]
        assert cls.conflict, "演示库本周必须有冲突，否则取值关的用例无效"

    @classmethod
    def tearDownClass(cls):
        if cls._prev_cfg is not None:
            set_current_config(cls._prev_cfg)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        self._save_env()

    def tearDown(self):
        self._restore_env()

    # -- helpers ----------------------------------------------------------
    def _ask(self, question: str, complete=None, token=None):
        headers = {"authorization": token or self.teacher_token}
        body = json.dumps({"question": question}, ensure_ascii=False).encode("utf-8")
        with mock.patch.object(b_llm_client, "complete", complete or (lambda *a, **k: None)):
            with mock.patch.object(b_llm_client, "polish", lambda *a, **k: None):
                return api_chat.chat("POST", "/api/chat", {}, body, headers, None)


# ---------------------------------------------------------------------------
# 一、模型路径：合法 IR → 只读查询 → 答案
# ---------------------------------------------------------------------------

class TestModelPath(_ChatCase):
    def test_colloquial_question_gets_real_conflict(self):
        """🔴 验收 1：口语问句 → 合法 IR → 后端查到的**真实冲突**。"""
        self._enable_llm()
        item = self.conflict
        period = (item.get("readable") or [{}])[0].get("period")
        ir = _ir("conflict_why", object="高一(1)班", date=item["date"],
                 period=period, query="conflicts")
        got = self._ask("我们班那个点是不是撞上了", complete=_complete_with(ir))

        self.assertEqual(got["rule"]["source"], "model", "模型解析成功必须标 model")
        self.assertEqual(got["rule"]["intent_id"], "conflict_why")
        self.assertEqual(got["mode"], "llm")
        self.assertFalse(got["degraded"])
        self.assertIs(got["grounded"], True)
        # 数字与对象来自库，不是模型编的
        self.assertIn(item["date"], got["answer"])
        self.assertTrue(got["sources"])

    def test_overview_without_slots(self):
        """不带槽位也能答：走「本周总览」，数字与 /api/conflicts 同源。"""
        self._enable_llm()
        got = self._ask("这周一共撞了多少", complete=_complete_with(_ir("conflict_why", query="conflicts")))
        self.assertEqual(got["rule"]["source"], "model")
        self.assertIn("组冲突", got["answer"])

    def test_howto_query_reuses_intent_answer(self):
        """`query=howto` → 复用意图表答案（不新造查询、不调库）。"""
        self._enable_llm()
        got = self._ask("就是想问问怎么弄", complete=_complete_with(_ir("temp_adjust", query="howto")))
        self.assertEqual(got["rule"]["source"], "model")
        self.assertIn("临时微调", got["answer"])

    def test_reply_is_used_only_when_grounded(self):
        """模型那句开场白：数字/对象都接地才拼进去，否则只用事实块。"""
        self._enable_llm()
        item = self.conflict
        period = (item.get("readable") or [{}])[0].get("period")
        slots = {"object": "高一(1)班", "date": item["date"], "period": period,
                 "query": "conflicts"}

        good = json.dumps({"intent_id": "conflict_why", "slots": slots,
                           "reply": "是的，这一节撞了。"}, ensure_ascii=False)
        got = self._ask("我们班那个点是不是撞上了", complete=_complete_with(good))
        self.assertIn("是的，这一节撞了。", got["answer"])

        bad = json.dumps({"intent_id": "conflict_why", "slots": slots,
                          "reply": "这一节一共撞了 99 处。"}, ensure_ascii=False)
        got2 = self._ask("我们班那个点是不是撞上了", complete=_complete_with(bad))
        self.assertNotIn("99", got2["answer"], "模型编的数字不许进答案")


# ---------------------------------------------------------------------------
# 二、兜底路径：任何一环不过都回退关键词（卡面验收 2–6）
# ---------------------------------------------------------------------------

class TestFallbacks(_ChatCase):
    def _assert_keyword(self, got, msg):
        self.assertEqual(got["rule"]["source"], "keyword", msg)
        self.assertIn(got["mode"], ("retrieval", "template"), msg)
        self.assertTrue(got["answer"].strip(), msg)

    def test_invalid_json_falls_back(self):
        """验收 2：JSON 非法（少括号 / 多字段）→ 回退关键词，不抛。"""
        self._enable_llm()
        for bad in ('{"intent_id": "reschedule"', '{"intent_id":"reschedule",'
                    '"slots":{},"reply":"","extra":1}'):
            got = self._ask("怎么调课", complete=_complete_with(bad))
            self._assert_keyword(got, f"非法 JSON 应回退：{bad!r}")
            self.assertTrue(got["degraded"])

    def test_intent_not_in_whitelist_falls_back(self):
        """验收 3：intent_id 不在白名单 → 整条丢弃。"""
        self._enable_llm()
        got = self._ask("怎么调课", complete=_complete_with(_ir("hacked_intent", query="howto")))
        self._assert_keyword(got, "白名单外的 intent 必须丢弃")

    def test_null_intent_falls_back(self):
        self._enable_llm()
        got = self._ask("怎么调课", complete=_complete_with(
            json.dumps({"intent_id": None, "slots": {}, "reply": ""})))
        self._assert_keyword(got, "模型弃权也要回退")

    def test_unknown_object_falls_back(self):
        """验收 4：slots.object 是库里没有的名字 → 回退。"""
        self._enable_llm()
        got = self._ask("怎么调课", complete=_complete_with(
            _ir("conflict_why", object="不存在的高一(9)班", query="conflicts")))
        self._assert_keyword(got, "库内不存在的对象必须挡掉")

    def test_date_out_of_window_falls_back(self):
        self._enable_llm()
        got = self._ask("怎么调课", complete=_complete_with(
            _ir("conflict_why", date="2027-06-01", query="conflicts")))
        self._assert_keyword(got, "窗口外的日期必须挡掉")

    def test_period_out_of_slots_falls_back(self):
        self._enable_llm()
        got = self._ask("怎么调课", complete=_complete_with(
            _ir("conflict_why", date=self.conflict["date"], period=99,
                query="conflicts")))
        self._assert_keyword(got, "当日没有的节次必须挡掉")

    def test_write_query_is_rejected(self):
        """验收 5（一）：query=write → schema 关挡掉。"""
        self._enable_llm()
        got = self._ask("怎么调课", complete=_complete_with(
            _ir("reschedule", query="write", object="高一(1)班")))
        self._assert_keyword(got, "写操作 query 不在只读枚举里")
        self.assertTrue(got["degraded"])

    def test_internal_id_is_rejected(self):
        """验收 5（二）：模型返回 sch_ ID → 红线关挡掉，且不进答案。"""
        self._enable_llm()
        payload = json.dumps({
            "intent_id": "reschedule",
            "slots": {"object": "高一(1)班", "query": "howto"},
            "reply": "已经帮你把 sch_abcdef123456 挪好了",
        }, ensure_ascii=False)
        got = self._ask("怎么调课", complete=_complete_with(payload))
        self._assert_keyword(got, "输出内部 ID 必须整条丢弃")
        self.assertNotIn("sch_", got["answer"])
        self.assertTrue(got["degraded"])

    def test_action_key_is_rejected(self):
        """slots 里夹带 action 键 → schema 关挡掉（防模型拼动作）。"""
        self._enable_llm()
        got = self._ask("怎么调课", complete=_complete_with(
            json.dumps({"intent_id": "reschedule",
                        "slots": {"action": "move", "query": "howto"},
                        "reply": ""}, ensure_ascii=False)))
        self._assert_keyword(got, "slots 非法键必须丢弃")

    def test_model_timeout_falls_back(self):
        """验收 6：模型返 None（超时）→ 回退 + degraded=True，不抛。"""
        self._enable_llm()
        got = self._ask("怎么调课", complete=lambda *a, **k: None)
        self._assert_keyword(got, "超时必须回退")
        self.assertTrue(got["degraded"])

    def test_model_exception_falls_back(self):
        self._enable_llm()

        def boom(*a, **k):
            raise TimeoutError("模拟超时")

        got = self._ask("怎么调课", complete=boom)
        self._assert_keyword(got, "注入实现抛异常也不许 500")
        self.assertTrue(got["degraded"])

    def test_no_facts_no_claim(self):
        """有筛选却查不到数据 → 回退关键词，⛔ 不让模型说「没有冲突」。"""
        self._enable_llm()
        # 演示库周日（第 2 天之后）没有课，用它构造「查不到」
        got = self._ask("我们班周日下午那节课撞了吗", complete=_complete_with(
            _ir("conflict_why", object="高一(1)班", date="2026-10-11", query="conflicts")))
        self.assertEqual(got["rule"]["source"], "keyword")
        self.assertNotIn("0 组冲突", got["answer"])


# ---------------------------------------------------------------------------
# 四、三关的单元级断言（不依赖库，破坏「白名单校验」这条必须红）
# ---------------------------------------------------------------------------

class TestGateUnits(unittest.TestCase):
    """直接打 `b_nl_kernels.parse_rule` 的三关 —— 不经过 HTTP、不连库。

    🔴 为什么必须有这一层：只跑端到端会漏掉「最后一关丢了但恰好结果相同」。
    例：把 intent 白名单校验注释掉 → 端到端仍会因「查不到事实」而回退，
    症状一模一样，测试却全绿（这就是假绿）。这里直接看 parse_rule 的返回值。
    """

    IDS = ("conflict_why", "reschedule", "temp_adjust")
    CTX = {
        "objects": {"高一(1)班": 1},
        "from": "2026-10-05",
        "to": "2026-10-11",
        "periods": {"2026-10-07": [1, 2]},
    }

    def setUp(self):
        NL.reset_cache()
        NL.reset_system_cache()

    def _parse(self, payload):
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        return NL.parse_rule(
            "随便问一句", self.IDS,
            complete=lambda system, user, timeout_s: payload,
            gate_ctx=self.CTX,
        )

    def test_valid_rule_passes(self):
        got = self._parse({"intent_id": "conflict_why",
                           "slots": {"object": "高一(1)班", "date": "2026-10-07",
                                     "period": 1, "query": "conflicts"},
                           "reply": "看一下这一节。"})
        self.assertIsNotNone(got)
        self.assertEqual(got["intent_id"], "conflict_why")
        self.assertEqual(got["slots"]["period"], 1)

    def test_intent_whitelist_is_enforced(self):
        """🔴 破坏法①：注释掉白名单校验，这条必须红。"""
        self.assertIsNone(self._parse({"intent_id": "drop_table",
                                       "slots": {}, "reply": ""}))

    def test_extra_key_is_rejected(self):
        self.assertIsNone(self._parse({"intent_id": "reschedule", "slots": {},
                                       "reply": "", "cmd": "x"}))

    def test_write_query_is_rejected(self):
        self.assertIsNone(self._parse({"intent_id": "reschedule",
                                       "slots": {"query": "write"}, "reply": ""}))

    def test_object_must_exist(self):
        self.assertIsNone(self._parse({"intent_id": "conflict_why",
                                       "slots": {"object": "高一(9)班"}, "reply": ""}))

    def test_date_must_be_in_window(self):
        self.assertIsNone(self._parse({"intent_id": "conflict_why",
                                       "slots": {"date": "2027-01-01"}, "reply": ""}))

    def test_date_must_be_iso(self):
        self.assertIsNone(self._parse({"intent_id": "conflict_why",
                                       "slots": {"date": "下周三"}, "reply": ""}))

    def test_period_must_be_enabled_that_day(self):
        self.assertIsNone(self._parse({"intent_id": "conflict_why",
                                       "slots": {"date": "2026-10-07", "period": 9},
                                       "reply": ""}))

    def test_internal_id_in_reply_is_rejected(self):
        self.assertIsNone(self._parse({"intent_id": "reschedule", "slots": {},
                                       "reply": "已把 sch_abcdef123456 挪走"}))

    def test_write_verb_in_reply_is_rejected(self):
        self.assertIsNone(self._parse({"intent_id": "reschedule", "slots": {},
                                       "reply": "已改为周四第 3 节"}))

    def test_reply_is_truncated_to_60(self):
        got = self._parse({"intent_id": "reschedule", "slots": {},
                           "reply": "很长的解释" * 30})
        self.assertIsNotNone(got)
        self.assertLessEqual(len(got["reply"]), NL.MAX_REPLY_CHARS + 1)

    def test_no_gate_ctx_rejects_everything(self):
        """拿不到库内事实时**一律判不过**（宁可回退关键词，也不放行未核对的规则）。"""
        got = NL.parse_rule(
            "随便问一句", self.IDS,
            complete=lambda *a, **k: json.dumps({"intent_id": "reschedule",
                                                "slots": {}, "reply": ""}),
            gate_ctx=None,
        )
        self.assertIsNone(got)


# ---------------------------------------------------------------------------
# 五、离线基线（卡面验收 7）
# ---------------------------------------------------------------------------

class TestOfflineBaseline(_ChatCase):
    def test_no_env_no_socket_at_all(self):
        """🔴 验收 7：未配 env → 一次 socket 都不开，连模型函数都不该被调用。

        🔴 两个断言缺一不可：只数 `urlopen` 抓不到「代码在未配置时去调
        `complete()` 但被 env 短路提前 return」这种改法（照样 0 次 socket，
        但语义已经错了 —— 一旦哪天 env 配错就会突然出网）。所以再钉一个调用计数。
        """
        self.assertFalse(K.llm_enabled())
        calls = []

        def counting(system, user, timeout_s):
            calls.append(1)
            return None

        with mock.patch("urllib.request.urlopen") as urlopen:
            got = self._ask("怎么调课", complete=counting)
        self.assertEqual(urlopen.call_count, 0,
                         "不配 env 时一个 socket 都不能开（smoke --offline 底线）")
        self.assertEqual(calls, [], "未配置时不许调模型函数")
        self.assertEqual(got["rule"]["source"], "keyword")
        self.assertEqual(got["degraded"], False)

    def test_switch_off_disables_both_directions(self):
        """`SCHEDULER_LLM=off` 一处关掉两向：即便 key/url 都在也不出网。"""
        self._enable_llm()
        self.assertTrue(K.llm_enabled())
        os.environ["SCHEDULER_LLM"] = "off"
        self.assertFalse(K.llm_enabled())
        self.assertFalse(b_llm_client.configured())
        with mock.patch("urllib.request.urlopen") as urlopen:
            got = self._ask("怎么调课")
        self.assertEqual(urlopen.call_count, 0)


# ---------------------------------------------------------------------------
# 四、20 条口语问句（卡面验收 2：正确 intent ≥18；关 LLM 仍可答）
# ---------------------------------------------------------------------------

#: (问句, 期望 intent_id)。刻意混入错别字、倒装、多槽位、口语——真实老师的说法。
#:
#: 🔴 刻意避开 6 条 `no_polish` 意图（propose_approve / resolve_how / login_fail /
#:    how_to_use / what_is / count_meaning）的强词：那六条**在进模型之前**就返回写死
#:    文本（卡面红线），它们的正确答案不依赖模型解析，拿来做「解析正确率」没有意义。
_SPOKEN = (
    ("高一(1)班周三第一节是不是撞了", "conflict_why"),
    ("我们这周哪节课撞一起了", "conflict_why"),
    ("啥叫冲突啊我一直没搞懂", "conflict_why"),
    ("只想动今天这一天别动整周怎么弄", "temp_adjust"),
    ("某天不上课了要怎么标一下", "cancel_once"),
    ("老师临时有事那天的课怎么去掉", "cancel_once"),
    ("为啥系统说我无权限", "resolve_need_approval"),
    ("冲突数一直没降下来是怎么回事", "resolve_remaining"),
    ("消完还剩几处没处理", "resolve_remaining"),
    ("空位怎么查啊", "slot_query"),
    ("哪节还空着能排个活动", "slot_query"),
    ("碰上假期那天按哪天的课表算", "makeup"),
    ("补课那天按周几走", "makeup"),
    ("页面一直转圈打不开", "page_not_open"),
    ("样式错乱了是要强刷吗", "cache_stale"),
    ("手机上打开会乱吗", "narrow_screen"),
    ("你们会不会记录我的名字", "privacy"),
    ("看不见冲突列表是空的", "conflicts_zero"),
    ("下拉框里没有班级可选", "empty_groups"),
    ("一个老师能看到别的班的课表吗", "org_isolate"),
)


class TestSpokenQuestions(_ChatCase):
    def test_twenty_spoken_questions_keep_intent(self):
        """模型解析正确时，20 条里 ≥18 条意图不被链路走样（全部走 query=howto）。"""
        self._enable_llm()
        ok = 0
        failed = []
        for question, want in _SPOKEN:
            NL.reset_cache()
            got = self._ask(question, complete=_complete_with(_ir(want, query="howto")))
            if got["rule"]["source"] == "model" and got["rule"]["intent_id"] == want:
                ok += 1
            else:
                failed.append((question, want, got["rule"]))
        self.assertGreaterEqual(ok, 18, f"只对了 {ok}/20：{failed}")

    def test_same_batch_still_answerable_without_llm(self):
        """关掉 LLM 同一批仍可答（会降但不断崖）。

        ⚠️ 这里量的是**关键词层的覆盖率**，不是模型质量：口语化问法检索不到
        是已知边界（文件头已写明），但必须保证「有相当一部分仍然答得出」，
        且**一条都不许抛异常 / 500**。

        2026-11-15 本机实测（demo 库，today=2026-10-05）：**13/20 仍可答**。
        门槛设在 8 是防「断崖」（整批变 template），不是按实测数字卡线。
        """
        answered = 0
        for question, _want in _SPOKEN:
            got = self._ask(question)   # 不配 env、不注入模型
            self.assertIn(got["mode"], ("retrieval", "template"))
            self.assertTrue(got["answer"].strip())
            if got["mode"] != "template":
                answered += 1
        self.assertGreaterEqual(answered, 8,
                                f"关 LLM 后只剩 {answered}/20 条能答，属断崖")


if __name__ == "__main__":
    unittest.main(verbosity=2)
