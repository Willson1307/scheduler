"""B-6（rn6JxF）验收测试：AI 问答层 POST /api/chat 与三层降级内核。

运行：python -m unittest tests.test_chat -v

---------------------------------------------------------------------------
🔴 本文件钉死的口径（每条对应卡面一条验收 + 一个静默失效风险）
---------------------------------------------------------------------------

  1. **离线可用**：不配任何 LLM 环境变量 → 问「怎么调课」拿到手册口径，
     `mode="retrieval"`，且 `urlopen` 调用次数为 **0**（断网可跑的底线）。
  2. **配了但不通 → 降级不 500**：`SCHEDULER_LLM_BASE` 指向必拒端口，
     仍返回 200 / `mode="retrieval"` / `degraded=True`（如实说「本可以调 AI 但没调成」）。
  3. **答不出来就说不知道**：3 个手册外问题 → 回答含「手册里没有这条」「请找项目
     负责人确认」，且 `sources` 为空。⛔ 不许推测、不许编步骤。
  4. **不传敏感内容**：开 LLM 时拦下真实调用参数，断言不含班级名 / 教师姓名 /
     对象 id（脱敏发生在内核层，不是靠调用方自觉）。
  5. **没拿到 `b_llm_client` 也能跑**：模拟 `ImportError` → 全部降级为纯检索，不抛。

外加接口层：401 未登录 / 400 缺 question / 403 跨组织 / 200 字段齐全
（`mode` 必填）/ 手册缺失返回 200 而不是 500。

⚠️ 关于卡面验收 5 的实现方式：卡面写的是「删掉该文件后全量测试仍绿」。实际执行时
`b_llm_client.py` 已由队长交付且 `tests/test_llm_client.py` 在模块顶层 `import` 它 ——
真删文件会让**队长的测试变红**（那是他的验收，不是本次改动的缺陷）。因此这里用
`sys.modules[...] = None` 精确模拟「该模块不可导入」，验的是同一个契约：
**内核不得硬依赖 `b_llm_client`**。偏离原因已写进交付报告。

只用 Python 标准库。
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scheduler import object_graph as og  # noqa: E402
from scheduler.db import connect  # noqa: E402
from server import api_chat, auth, config  # noqa: E402
from server import b_ai_kernels as K  # noqa: E402
from server.config import ServerConfig, ensure_database  # noqa: E402
from server.http_api import ApiError  # noqa: E402

TODAY = date(2026, 9, 8)  # 周一；seed_demo 的 term_start 锚定 2026-09-07

_ENV_KEYS = ("SCHEDULER_LLM_BASE", "SCHEDULER_LLM_KEY", "SCHEDULER_LLM_MODEL", "no_proxy")

#: 手册外问题（验收第 3 条：3 个必须说「不知道」）
_OUT_OF_MANUAL = (
    "学校食堂几点开门？",
    "明天天气怎么样？",
    "怎么申请报销？",
)


class _EnvMixin:
    """环境变量隔离：每个用例存/还原 LLM 相关 env，防止串味到别的测试文件。"""

    def _save_env(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def _restore_env(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# 一、内核：第 0 层意图表 + 第 1 层检索
# ---------------------------------------------------------------------------

class TestKernelBasics(_EnvMixin, unittest.TestCase):
    def setUp(self):
        self._save_env()
        K.reset_corpus_cache()

    def tearDown(self):
        self._restore_env()
        K.reset_corpus_cache()

    def test_corpus_sections_loaded(self):
        """语料按 `##` 切段：两个文件都要在，段数 > 20。"""
        sections = K.load_corpus()
        self.assertIsNotNone(sections, "docs/30 与 docs/07 必须都能读到")
        docs = {s["doc"] for s in sections}
        self.assertEqual(docs, {"docs/30", "docs/07"})
        self.assertGreater(len(sections), 20)

    def test_intent_table_size_in_card_range(self):
        """卡面：手工 20–30 条（少于 20 覆盖不住，多于 30 没人维护）。"""
        self.assertGreaterEqual(len(K.INTENTS), 20)
        self.assertLessEqual(len(K.INTENTS), 30)
        ids = [i["id"] for i in K.INTENTS]
        self.assertEqual(len(ids), len(set(ids)), "意图 id 不得重复")
        for item in K.INTENTS:
            self.assertTrue(item["strong"], f"{item['id']} 必须有 strong 词")
            self.assertTrue(item["answer"].strip())
            self.assertTrue(item["sources"], f"{item['id']} 必须给出处")

    def test_card_example_question_hits_intent(self):
        """卡面原例：「我想把周三下午的课调开，怎么弄？」必须能答。"""
        q = "我想把周三下午的课调开，怎么弄？"
        self.assertIsNotNone(K.match_intent(q), "卡面原例必须命中意图表")
        result = K.answer(q)
        self.assertEqual(result["mode"], "retrieval")
        self.assertEqual(result["degraded"], False)
        self.assertIn("临时微调", result["answer"])
        self.assertIn("管理员批准", result["answer"])
        self.assertTrue(result["sources"])

    def test_offline_how_to_reschedule(self):
        """🔴 验收 1：不配 LLM → 问「怎么调课」得到手册口径，且零外联。"""
        self.assertFalse(K.llm_enabled())
        with mock.patch("urllib.request.urlopen") as urlopen:
            result = K.answer("怎么调课")
        self.assertEqual(urlopen.call_count, 0,
                         "不配 env 时一次 socket 都不能开（smoke --offline 底线）")
        self.assertEqual(result["mode"], "retrieval")
        self.assertEqual(result["degraded"], False)
        # 🔴 2026-11-13 改：原断言要求答案里出现 `propose` —— 但答案面向老师，
        #    英文档位名属技术描述，正是本轮整改要删的东西。断言改为中文「提议」，
        #    并补一条反向断言：技术词一旦被塞回文案，这里立刻红。
        self.assertIn("提议", result["answer"])
        self.assertIn("管理员批准", result["answer"])
        for bad in ("propose", "approve", "effective", "mode=", "scope=",
                    "termination", "429", "group_roles"):
            self.assertNotIn(bad, result["answer"],
                             f"老师看的答案里不该出现技术词：{bad}")
        for src in result["sources"]:
            self.assertEqual(set(src), {"doc", "section"})

    def test_retrieval_layer_answers_from_manual(self):
        """第 1 层（非意图表）也要能答：问题里带手册中的专有字段。"""
        q = "filtered_out_count 是什么意思"
        self.assertIsNone(K.match_intent(q), "该问题不该命中意图表（验的是检索层）")
        result = K.answer(q)
        self.assertEqual(result["mode"], "retrieval")
        self.assertIn("docs/07", [s["doc"] for s in result["sources"]])
        self.assertIn("手册原文", result["answer"])

    def test_out_of_manual_says_dont_know(self):
        """🔴 验收 3：3 个手册外问题 → 明确说不知道，sources 为空。

        2026-11-13 第二次改（队长口径）：**「手册里没有这条，请找开发者确认」整句删除**。
        对老师来说它像报错（「你问的东西不存在」+「去找一个你不知道是谁的人」）。
        新文案不否定、给下一步，且不出现「手册」二字（老师不知道什么手册）。
        下面两条反向断言就是防它被改回去。
        """
        for q in _OUT_OF_MANUAL:
            result = K.answer(q)
            self.assertEqual(result["mode"], "template", msg=q)
            self.assertEqual(result["sources"], [], msg=q)
            self.assertIn("暂时答不上来", result["answer"], msg=q)
            self.assertIn("关于", result["answer"], msg=q)  # 指向页面底部的反馈入口
            self.assertNotIn("手册里没有这条", result["answer"], msg=q)
            self.assertNotIn("请找开发者确认", result["answer"], msg=q)

    def test_guide_questions_answerable_offline(self):
        """导览类问题必须离线可答（2026-11-13 实测「怎么使用此系统」被判「没有这条」）。

        这类问题**不该靠模型**：它问的是系统本身怎么用，答案在代码里就有。
        所以落成意图表条目，且不配 LLM 时也要能答（离线基线）。
        """
        cases = (
            ("怎么使用此系统", "how_to_use"),
            ("如何使用这个系统", "how_to_use"),
            ("第一次用从哪开始", "how_to_use"),
            ("这是什么", "what_is"),
            ("这套系统能做什么", "what_is"),
        )
        for q, want in cases:
            intent = K.match_intent(q)
            self.assertIsNotNone(intent, f"「{q}」必须命中意图表")
            self.assertEqual(intent["id"], want, f"「{q}」应命中 {want}")
            result = K.answer(q)
            self.assertEqual(result["mode"], "retrieval", q)
            self.assertEqual(result["degraded"], False, q)
            self.assertNotIn("手册里没有这条", result["answer"], q)
            self.assertTrue(result["sources"], q)

    def test_no_answer_hint_examples_are_answerable(self):
        """兜底文案里举的例子必须真的能命中。

        否则等于教用户「照我说的再撞一次墙」—— 2026-11-13 初版就把
        「周三第 1 节撞了没」写进了示例，而它当时同样答不出。
        """
        for q in ("怎么调课", "临时取消一节课", "看不到冲突"):
            self.assertIsNotNone(K.match_intent(q), f"兜底示例「{q}」必须可答")

    def test_empty_question(self):
        result = K.answer("   ")
        self.assertEqual(result["mode"], "template")
        self.assertIn("请先输入问题", result["answer"])

    def test_corpus_missing_does_not_raise(self):
        """卡面：两文件缺失 → 「手册未就绪」，⛔ 不 500、不抛。"""
        tmp = tempfile.mkdtemp(prefix="chat-nocorpus-")
        try:
            with mock.patch.object(K, "repo_root", return_value=tmp):
                K.reset_corpus_cache()
                result = K.answer("怎么调课")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            K.reset_corpus_cache()
        self.assertEqual(result["mode"], "template")
        self.assertIn("手册未就绪", result["answer"])
        self.assertEqual(result["sources"], [])


# ---------------------------------------------------------------------------
# 二、第 2 层：LLM 润色与降级
# ---------------------------------------------------------------------------

class TestLlmLayer(_EnvMixin, unittest.TestCase):
    def setUp(self):
        self._save_env()
        K.reset_corpus_cache()

    def tearDown(self):
        self._restore_env()
        K.reset_corpus_cache()

    def test_guide_intent_skips_polish(self):
        """导览类（no_polish）即使配了 LLM 也不许调模型。

        2026-11-13 实测：润色「怎么使用此系统」要 9.2 秒，且模型把操作步骤压成
        一句、丢掉「双击格子」。导览是清单不是文案 —— 必须 0 延迟返回原文。
        """
        os.environ["SCHEDULER_LLM_BASE"] = "https://llm.example.invalid/v4"
        os.environ["SCHEDULER_LLM_KEY"] = "sk-xxxx"
        called = {"n": 0}

        def fake_polish(question, passages, timeout_s=8.0):
            called["n"] += 1
            return "不该被调用"

        result = K.answer("怎么使用此系统", polish=fake_polish)
        self.assertEqual(called["n"], 0, "导览类意图不许调模型")
        self.assertEqual(result["mode"], "retrieval")
        self.assertEqual(result["degraded"], False)
        self.assertIn("第一次用", result["answer"])

    def test_llm_success_sets_mode_llm(self):
        os.environ["SCHEDULER_LLM_BASE"] = "https://llm.example.invalid/v4"
        os.environ["SCHEDULER_LLM_KEY"] = "sk-xxxx"  # 假 key，不是真凭据
        captured = {}

        def fake_polish(question, passages, timeout_s=8.0):
            captured["question"] = question
            captured["passages"] = list(passages)
            captured["timeout_s"] = timeout_s
            return "模型润色后的回答"

        result = K.answer("filtered_out_count 是什么意思", polish=fake_polish)
        self.assertEqual(result["mode"], "llm")
        self.assertEqual(result["degraded"], False)
        self.assertEqual(result["answer"], "模型润色后的回答")
        # 🔴 2026-11-13 改：8.0 → 15.0。线上实测「3 段手册原文 + glm-4-flash」
        #    整段耗时稳定超过 8 秒，8 秒超时会让老师每次都看到「AI 这次没响应」。
        #    现在检索层显式传 15 秒（意图表层传 10 秒）；`b_llm_client.polish`
        #    的**缺省值仍是 8.0**（跨人契约未动），由调用方按场景显式收紧/放宽。
        self.assertEqual(captured["timeout_s"], 15.0,
                         "检索层必须给够 15 秒，否则线上稳定超时降级")

    def test_llm_returns_none_degrades_with_flag(self):
        """🔴 验收 2：配了但调不通 → mode=retrieval + degraded=True，不抛。"""
        os.environ["SCHEDULER_LLM_BASE"] = "https://llm.example.invalid/v4"
        os.environ["SCHEDULER_LLM_KEY"] = "sk-xxxx"
        result = K.answer("filtered_out_count 是什么意思", polish=lambda *a, **kw: None)
        self.assertEqual(result["mode"], "retrieval")
        self.assertTrue(result["degraded"], "配了却没调成，必须如实标降级")
        self.assertIn("手册原文", result["answer"])

    def test_llm_raising_is_swallowed(self):
        """客户端抛异常也必须降级 —— 否则问答接口整个 500。"""
        os.environ["SCHEDULER_LLM_BASE"] = "https://llm.example.invalid/v4"
        os.environ["SCHEDULER_LLM_KEY"] = "sk-xxxx"

        def boom(*a, **kw):
            raise TimeoutError("模拟超时")

        result = K.answer("filtered_out_count 是什么意思", polish=boom)
        self.assertEqual(result["mode"], "retrieval")
        self.assertTrue(result["degraded"])

    def test_llm_empty_string_is_failure(self):
        os.environ["SCHEDULER_LLM_BASE"] = "https://llm.example.invalid/v4"
        os.environ["SCHEDULER_LLM_KEY"] = "sk-xxxx"
        result = K.answer("filtered_out_count 是什么意思", polish=lambda *a, **kw: "   ")
        self.assertEqual(result["mode"], "retrieval")
        self.assertTrue(result["degraded"])

    def test_real_broken_endpoint_degrades_without_500(self):
        """🔴 验收 2 的真实版：env 指向必拒端口，真走 urlopen 也不抛。"""
        os.environ["SCHEDULER_LLM_BASE"] = "http://127.0.0.1:1"
        os.environ["SCHEDULER_LLM_KEY"] = "sk-xxxx"
        os.environ["no_proxy"] = "127.0.0.1"  # 绕开本机代理，否则拿到的是代理的 502
        self.assertTrue(K.llm_enabled())
        result = K.answer("filtered_out_count 是什么意思")
        self.assertEqual(result["mode"], "retrieval")
        self.assertTrue(result["degraded"])
        self.assertIn("手册原文", result["answer"])

    def test_missing_client_module_degrades(self):
        """🔴 验收 5：b_llm_client 不可导入（对方未交付）→ 纯检索，不抛。"""
        os.environ["SCHEDULER_LLM_BASE"] = "https://llm.example.invalid/v4"
        os.environ["SCHEDULER_LLM_KEY"] = "sk-xxxx"
        # sys.modules 里置 None = 该模块不可导入（import 会抛 ImportError）
        with mock.patch.dict(sys.modules, {"server.b_llm_client": None}):
            result = K.answer("filtered_out_count 是什么意思")
        self.assertEqual(result["mode"], "retrieval")
        self.assertTrue(result["degraded"])
        # 连意图表那条路径也不能受影响
        with mock.patch.dict(sys.modules, {"server.b_llm_client": None}):
            self.assertEqual(K.answer("怎么调课")["mode"], "retrieval")

    def test_no_llm_call_when_not_configured(self):
        """未配置时连 polish 都不该被调用（省一次无意义的 socket 尝试）。"""
        called = []
        result = K.answer("filtered_out_count 是什么意思",
                          polish=lambda *a, **kw: called.append(1))
        self.assertEqual(called, [], "未配置 env 时不该调 polish")
        self.assertEqual(result["mode"], "retrieval")
        self.assertEqual(result["degraded"], False)


# ---------------------------------------------------------------------------
# 三、脱敏
# ---------------------------------------------------------------------------

class TestScrub(unittest.TestCase):
    def test_teacher_and_class_are_replaced(self):
        out = K.scrub("高一(3)班 张老师 想把周三下午的课调开")
        self.assertIn(K.PH_CLASS, out)
        self.assertIn(K.PH_TEACHER, out)
        self.assertNotIn("高一(3)班", out)
        self.assertNotIn("张老师", out)

    def test_object_ids_are_replaced(self):
        out = K.scrub("把 obj_1a2b3c4d5e6f7890 挪到 sch_abcdef1234567890")
        self.assertNotIn("obj_1a2b3c4d5e6f7890", out)
        self.assertNotIn("sch_abcdef1234567890", out)
        self.assertEqual(out.count(K.PH_ID), 2)

    def test_generic_words_are_kept(self):
        """「各位老师」不是具体人 —— 错替会改坏语义，比不替换更糟。"""
        self.assertEqual(K.scrub("各位老师好"), "各位老师好")
        self.assertEqual(K.scrub("本班情况"), "本班情况")

    def test_cn_numeral_class_is_replaced(self):
        """🔴 2026-11-13 外部审查：原实现只认阿拉伯数字，`高一一班` 会漏给模型。"""
        out = K.scrub("高一一班周三下午有冲突吗")
        self.assertIn(K.PH_CLASS, out)
        self.assertNotIn("高一一班", out)

    def test_vocab_replaces_bare_name_and_cn_class(self):
        """裸姓名没有「老师」后缀，正则猜不到 —— 靠库里真实名单精确替换。"""
        vocab = {"王思勋": K.PH_PERSON, "高一一班": K.PH_CLASS}
        out = K.scrub("王思勋负责的高一一班为什么有冲突", vocab=vocab)
        self.assertNotIn("王思勋", out)
        self.assertNotIn("高一一班", out)
        self.assertIn(K.PH_PERSON, out)
        self.assertIn(K.PH_CLASS, out)

    def test_vocab_does_not_touch_ordinary_words(self):
        vocab = {"示例1班": K.PH_CLASS}
        self.assertEqual(K.scrub("怎么调课", vocab=vocab), "怎么调课")

    def test_scrub_applied_before_llm(self):
        """🔴 验收 4：开 LLM 前脱敏 —— 抓真实调用参数，断言不含姓名/班级/id。"""
        saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["SCHEDULER_LLM_BASE"] = "https://llm.example.invalid/v4"
        os.environ["SCHEDULER_LLM_KEY"] = "sk-xxxx"
        captured = {}

        def fake_polish(question, passages, timeout_s=8.0):
            captured["question"] = question
            captured["passages"] = list(passages)
            return "好的"

        try:
            K.answer(
                "高一(3)班 张老师 问：filtered_out_count 是什么意思，obj_1a2b3c4d5e6f7890 呢",
                polish=fake_polish,
            )
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.assertTrue(captured, "应当真的调用了 polish")
        blob = captured["question"] + "\n" + "\n".join(captured["passages"])
        for forbidden in ("高一(3)班", "张老师", "obj_1a2b3c4d5e6f7890"):
            self.assertNotIn(forbidden, blob, f"敏感内容 {forbidden} 不得送入模型")
        self.assertIn(K.PH_CLASS, captured["question"])
        self.assertIn(K.PH_TEACHER, captured["question"])


# ---------------------------------------------------------------------------
# 四、接口层：鉴权 / 参数 / 组织隔离
# ---------------------------------------------------------------------------

def _issue_token(conn, login: str) -> str:
    row = conn.execute(
        "SELECT id, login, role, object_id FROM users WHERE login = ?", (login,)
    ).fetchone()
    assert row is not None, f"账号 {login} 不存在"
    return "Bearer " + auth.issue_token(row["id"], row["login"], row["role"], row["object_id"])


class TestChatAPI(_EnvMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="chat-test-")
        cls.db_path = os.path.join(cls.tmpdir, "demo.db")
        cfg = ServerConfig(db_path=cls.db_path, today=TODAY)
        ensure_database(cfg)
        cls._prev_cfg = config._current
        config.set_current_config(cfg)

        conn = connect(cls.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM objects WHERE kind='group' AND name='高一(1)班'"
            ).fetchone()
            cls.class_id = row["id"]
            cls.admin_token = _issue_token(conn, "admin")
            cls.teacher_token = _issue_token(conn, "t01")
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        if cls._prev_cfg is not None:
            config.set_current_config(cls._prev_cfg)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        self._save_env()
        K.reset_corpus_cache()

    def tearDown(self):
        self._restore_env()
        K.reset_corpus_cache()

    # -- helpers -----------------------------------------------------------
    def _call(self, payload: dict, headers=None):
        import json
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return api_chat.chat("POST", "/api/chat", {}, body, headers or {}, None)

    # -- 200 形状 ----------------------------------------------------------
    def test_ok_shape_contract(self):
        result = self._call({"question": "怎么调课"}, {"authorization": self.teacher_token})
        self.assertIsInstance(result, dict)
        self.assertEqual(set(result), {"answer", "sources", "mode", "degraded"},
                         "响应体必须正好是卡面四个字段")
        self.assertIn(result["mode"], ("llm", "retrieval", "template"))
        self.assertIsInstance(result["degraded"], bool)
        self.assertIsInstance(result["sources"], list)
        self.assertTrue(result["answer"].strip())

    def test_out_of_manual_is_200_not_4xx(self):
        """「答不上来」是正常业务态：200 + mode=template，⛔ 不是错误码。

        2026-11-13 改：文案从「手册里没有这条」换成「暂时答不上来」，
        断言跟着换；语义（200 + template）不变。
        """
        result = self._call({"question": "学校食堂几点开门？"},
                            {"authorization": self.teacher_token})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["mode"], "template")
        self.assertIn("暂时答不上来", result["answer"])

    def test_group_in_scope_ok(self):
        result = self._call({"question": "怎么调课", "group": self.class_id},
                            {"authorization": self.teacher_token})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["mode"], "retrieval")

    def test_group_omitted_ok(self):
        """不传 group 也要能答 —— 否则只想问手册的老师会被挡在门外。"""
        result = self._call({"question": "怎么调课"}, {"authorization": self.teacher_token})
        self.assertIsInstance(result, dict)

    # -- 401 --------------------------------------------------------------
    def test_unauthenticated_401(self):
        status, body = self._call({"question": "怎么调课"}, headers={})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    # -- 账号停用：旧令牌必须失效（2026-11-13 外部审查）--------------------
    def test_disabled_account_invalidates_old_token(self):
        """停用只挡新登录、旧令牌仍能调接口 —— 这是审查实测到的缺陷，此处锁死。"""
        conn = connect(self.db_path)
        try:
            conn.execute("UPDATE users SET status='disabled' WHERE login='t01'")
            conn.commit()
        finally:
            conn.close()
        try:
            status, _body = self._call(
                {"question": "怎么调课"}, {"authorization": self.teacher_token}
            )
            self.assertEqual(status, 401, "账号停用后，停用前签发的令牌必须立刻失效")
        finally:
            conn = connect(self.db_path)
            try:
                conn.execute("UPDATE users SET status='active' WHERE login='t01'")
                conn.commit()
            finally:
                conn.close()
        ok = self._call({"question": "怎么调课"}, {"authorization": self.teacher_token})
        self.assertIsInstance(ok, dict, "账号恢复正常后，令牌应重新可用")

    # -- 脱敏词表来自库里的真实对象名 ---------------------------------------
    def test_name_vocab_comes_from_org_objects(self):
        conn = connect(self.db_path)
        try:
            uid = conn.execute("SELECT id FROM users WHERE login='t01'").fetchone()["id"]
        finally:
            conn.close()
        vocab = api_chat._name_vocab(uid)
        self.assertTrue(vocab, "同组织内应能取到对象名，供问答层精确脱敏")
        self.assertEqual(vocab.get("高一(1)班"), K.PH_CLASS)
        self.assertIn(K.PH_PERSON, set(vocab.values()), "人员姓名应映射到 [人员]")

    # -- 400 --------------------------------------------------------------
    def test_missing_question_400(self):
        status, body = self._call({}, {"authorization": self.teacher_token})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")
        status, body = self._call({"question": "   "}, {"authorization": self.teacher_token})
        self.assertEqual(status, 400)
        status, body = self._call({"question": 123}, {"authorization": self.teacher_token})
        self.assertEqual(status, 400)

    def test_too_long_question_400(self):
        status, body = self._call({"question": "课" * (api_chat.MAX_QUESTION_CHARS + 1)},
                                  {"authorization": self.teacher_token})
        self.assertEqual(status, 400)
        self.assertIn("过长", body["error"]["message"])

    def test_bad_group_type_400(self):
        status, _body = self._call({"question": "怎么调课", "group": 123},
                                   {"authorization": self.teacher_token})
        self.assertEqual(status, 400)

    # -- 403 跨组织 --------------------------------------------------------
    def test_cross_org_group_403(self):
        """🔴 越权访问别组织 → 403（⛔ 不是 200 空结果），与 B3/B4 同口径。"""
        conn = connect(self.db_path)
        try:
            org_b, _root = og.create_organization(conn, "问答测试校B")
            group_b = og.create_object(conn, org_b, "group", "B班")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ApiError) as ctx:
            self._call({"question": "怎么调课", "group": group_b},
                       {"authorization": self.teacher_token})
        self.assertEqual(ctx.exception.status, 403)

    # -- 手册缺失仍 200 ----------------------------------------------------
    def test_corpus_missing_returns_200(self):
        tmp = tempfile.mkdtemp(prefix="chat-nocorpus-api-")
        try:
            with mock.patch.object(K, "repo_root", return_value=tmp):
                K.reset_corpus_cache()
                result = self._call({"question": "怎么调课"},
                                    {"authorization": self.teacher_token})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            K.reset_corpus_cache()
        self.assertIsInstance(result, dict, "手册缺失不得 500，必须回 200 + 说明")
        self.assertIn("手册未就绪", result["answer"])
        self.assertEqual(result["mode"], "template")


if __name__ == "__main__":
    unittest.main(verbosity=2)
