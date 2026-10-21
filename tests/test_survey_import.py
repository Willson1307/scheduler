"""纸质问卷录入通路测试（import_survey / survey_responses / docs/32 口径）。

口径依据：docs/08-试用组织数据口径.md §4（问卷章节）+ docs/32-试用问卷录入与统计口径.md。

覆盖四条主路径（卡面要求）：
  1. 正常路径：合法录入文件 → 导入 → 统计正确；
  2. 非法值：明确报错（指明哪份哪题）、零写入，绝不静默丢弃；
  3. 幂等：同一文件导入两次统计不变；同编号跨文件覆盖、同文件内重复报错；
  4. 隐私：泄漏词表 / 登录名 / 结构化字段中文一律拒绝。

🔴 测试不触碰 data/demo.db 与 data/trial.db：一律用临时目录 + 内存库。
（AGENTS 铁律：要测写路径先用 backup() 复制到临时目录；本测试的写路径全部
落在新库/内存库上，等效于「复制出来的库」。）

只用 Python 标准库。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import import_survey  # noqa: E402
import telemetry_report  # noqa: E402  # 词表单一来源，测试同源核对
from scheduler.db import connect  # noqa: E402

VALID_SURVEY = {
    "survey_version": 1,
    "responses": [
        {"trial_code": "T03", "q1": "yes", "q2": "unsure", "q3": 2, "q4": 4,
         "q5": "conflict list takes too long to load",
         "q6": "remember last selected class"},
        {"trial_code": "T07", "q1": "no", "q2": "no", "q3": 0, "q4": 2},
    ],
}


def _write(tmpdir: str, name: str, doc: dict) -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    return path


def _entry(**overrides) -> dict:
    e = {"trial_code": "T01", "q1": "yes", "q2": "yes", "q3": 1, "q4": 5}
    e.update(overrides)
    return e


class TestValidate(unittest.TestCase):
    """校验规则（docs/32 §2.3 / §3）：非法值逐条报错，绝不静默丢弃。"""

    def test_valid_file_has_no_problems(self):
        self.assertEqual(import_survey.validate_entries(VALID_SURVEY["responses"]), [])

    def test_q4_out_of_range_points_to_sheet_and_question(self):
        entries = [_entry(trial_code="T03", q4=9)]
        problems = import_survey.validate_entries(entries)
        self.assertTrue(any("T03" in p and "q4" in p and "9" in p for p in problems),
                        msg=problems)

    def test_q1_chinese_is_invalid_enum(self):
        # 结构化字段零中文：纸质上的「是」必须按对照表转录成 yes（docs/32 §2.3）
        entries = [_entry(trial_code="T05", q1="是")]
        problems = import_survey.validate_entries(entries)
        self.assertTrue(any("q1" in p and "yes/unsure/no" in p for p in problems),
                        msg=problems)

    def test_q3_out_of_enum(self):
        entries = [_entry(q3=7)]
        problems = import_survey.validate_entries(entries)
        self.assertTrue(any("q3" in p and "0/1/2/3" in p for p in problems), msg=problems)

    def test_missing_required_question(self):
        entries = [_entry(trial_code="T02")]
        del entries[0]["q3"]
        problems = import_survey.validate_entries(entries)
        self.assertTrue(any("q3" in p for p in problems), msg=problems)

    def test_unknown_key_rejected(self):
        entries = [_entry(trial_code="T02", q7="yes")]
        problems = import_survey.validate_entries(entries)
        self.assertTrue(any("未知字段" in p and "q7" in p for p in problems), msg=problems)

    def test_bad_trial_code_rejected(self):
        entries = [_entry(trial_code="王老师")]
        problems = import_survey.validate_entries(entries)
        self.assertTrue(any("trial_code" in p and "短码" in p for p in problems), msg=problems)

    def test_duplicate_code_within_one_file_rejected(self):
        entries = [_entry(trial_code="T03"), _entry(trial_code="T03", q4=1)]
        problems = import_survey.validate_entries(entries)
        self.assertTrue(any("重复" in p and "T03" in p for p in problems), msg=problems)

    def test_open_answer_leak_word_rejected(self):
        # 开放题允许中文，但命中泄漏词表即拒绝（docs/32 §3 规则 2）
        entries = [_entry(trial_code="T04", q5="八年级9班的朋友说界面太乱")]
        problems = import_survey.validate_entries(entries)
        self.assertTrue(any("T04" in p and "q5" in p and "泄漏词表" in p for p in problems),
                        msg=problems)

    def test_open_answer_login_rejected(self):
        entries = [_entry(trial_code="T04", q6="trial_t01 帮我排的课表")]
        problems = import_survey.validate_entries(entries, logins={"trial_t01"})
        self.assertTrue(any("q6" in p and "登录名" in p and "trial_t01" in p for p in problems),
                        msg=problems)

    def test_leak_word_list_is_single_source(self):
        # 词表必须 import 自 telemetry_report（单一词表源，docs/32 §3），不得自抄一份
        self.assertIs(import_survey.LEAK_WORDS, telemetry_report.LEAK_WORDS)


class TestLoadFile(unittest.TestCase):
    """JSON / CSV 双格式读取。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_csv_roundtrip_matches_json(self):
        csv_path = os.path.join(self.tmp.name, "s.csv")
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("trial_code,q1,q2,q3,q4,q5,q6\n")
            f.write("T03,yes,unsure,2,4,conflict list slow,remember class\n")
            f.write("T07,no,no,0,2,,\n")
        entries = import_survey.load_file(csv_path)
        self.assertEqual(entries[0]["trial_code"], "T03")
        self.assertEqual(entries[0]["q3"], 2)  # CSV 数字串容差转 int
        self.assertEqual(entries[0]["q5"], "conflict list slow")
        self.assertEqual(entries[1]["q5"], "")   # 空开放题
        self.assertEqual(import_survey.validate_entries(entries), [])

    def test_csv_bad_header_rejected(self):
        csv_path = os.path.join(self.tmp.name, "bad.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            f.write("code,q1,q2,q3,q4,q5,q6\nT03,yes,yes,1,5,,\n")
        with self.assertRaises(import_survey.SurveyImportError):
            import_survey.load_file(csv_path)

    def test_unsupported_extension_rejected(self):
        with self.assertRaises(import_survey.SurveyImportError):
            import_survey.load_file("surveys.txt")

    def test_unsupported_version_rejected(self):
        path = _write(self.tmp.name, "v2.json", {"survey_version": 2, "responses": []})
        with self.assertRaises(import_survey.SurveyImportError):
            import_survey.load_file(path)


class TestImportAndStats(unittest.TestCase):
    """正常路径 + 幂等 + 零写入保证 + 统计口径（docs/08 §4.2 / docs/32 §6）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)
        self.path = _write(self.tmp.name, "s.json", VALID_SURVEY)

    def test_import_then_stats_correct(self):
        import_survey.import_file(self.path, self.conn)
        stats = import_survey.survey_stats(self.conn)
        self.assertEqual(stats["n"], 2)
        self.assertEqual(stats["q1_dist"], {"yes": 1, "unsure": 0, "no": 1})
        self.assertEqual(stats["q2_dist"], {"yes": 0, "unsure": 1, "no": 1})
        self.assertEqual(stats["q3_dist"], {0: 1, 1: 0, 2: 1, 3: 0})
        self.assertEqual(stats["q4"]["mean"], 3.0)  # (4+2)/2
        self.assertEqual(stats["q4"]["std"], 1.0)   # 总体标准差
        self.assertEqual(stats["q4"]["distribution"], {1: 0, 2: 1, 3: 0, 4: 1, 5: 0})
        self.assertEqual(stats["q4"]["ge4_count"], 1)
        self.assertEqual(stats["q5_excerpts"],
                         [{"trial_code": "T03", "text": "conflict list takes too long to load"}])
        self.assertEqual(stats["q6_excerpts"],
                         [{"trial_code": "T03", "text": "remember last selected class"}])
        self.assertTrue(stats["sample_limit"])  # n=2 < 30

    def test_idempotent_double_import(self):
        import_survey.import_file(self.path, self.conn)
        import_survey.import_file(self.path, self.conn)  # 同文件第二次导入
        stats = import_survey.survey_stats(self.conn)
        self.assertEqual(stats["n"], 2)  # 不变：覆盖而非追加
        rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM survey_responses").fetchone()["c"]
        self.assertEqual(rows, 2)

    def test_reimport_overwrites_same_code(self):
        import_survey.import_file(self.path, self.conn)
        fixed = json.loads(json.dumps(VALID_SURVEY))
        fixed["responses"][0]["q4"] = 5  # T03 修正 4 → 5
        path2 = _write(self.tmp.name, "s_fixed.json", fixed)
        import_survey.import_file(path2, self.conn)
        stats = import_survey.survey_stats(self.conn)
        self.assertEqual(stats["n"], 2)          # 仍是 2 份
        self.assertEqual(stats["q4"]["mean"], 3.5)  # (5+2)/2：以最新一次为准

    def test_invalid_file_imports_nothing(self):
        import_survey.import_file(self.path, self.conn)
        before = import_survey.survey_stats(self.conn)
        bad = {"survey_version": 1,
               "responses": [{"trial_code": "T09", "q1": "yes", "q2": "yes",
                              "q3": 1, "q4": 99}]}
        path_bad = _write(self.tmp.name, "bad.json", bad)
        with self.assertRaises(import_survey.SurveyImportError) as ctx:
            import_survey.import_file(path_bad, self.conn)
        self.assertTrue(any("q4" in p and "T09" in p for p in ctx.exception.problems))
        after = import_survey.survey_stats(self.conn)
        self.assertEqual(after, before)  # 零写入：校验失败前一份数据原样

    def test_privacy_violation_blocks_import(self):
        leak = {"survey_version": 1,
                "responses": [{"trial_code": "T04", "q1": "yes", "q2": "yes",
                               "q3": 1, "q4": 5, "q5": "嘉祥的老师说好用"}]}
        path_leak = _write(self.tmp.name, "leak.json", leak)
        with self.assertRaises(import_survey.SurveyImportError) as ctx:
            import_survey.import_file(path_leak, self.conn)
        self.assertTrue(any("泄漏词表" in p and "q5" in p for p in ctx.exception.problems))
        self.assertEqual(import_survey.survey_stats(self.conn)["n"], 0)  # 整文件拒绝

    def test_check_dry_run_writes_nothing(self):
        result = import_survey.import_file(self.path, self.conn, dry_run=True)
        self.assertEqual(result["checked"], 2)
        self.assertEqual(import_survey.survey_stats(self.conn)["n"], 0)  # 未写库

    def test_sample_limit_disappears_at_30(self):
        big = {"survey_version": 1,
               "responses": [_entry(trial_code=f"T{i:02d}") for i in range(1, 31)]}
        path_big = _write(self.tmp.name, "big.json", big)
        import_survey.import_file(path_big, self.conn)
        stats = import_survey.survey_stats(self.conn)
        self.assertEqual(stats["n"], 30)
        self.assertFalse(stats["sample_limit"])  # ≥30 才解除「不报百分比」限制

    def test_alignment_warning_for_unknown_code(self):
        # event_log 不存在（新库）→ 不该崩；存在但缺该编号 → warning
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS event_log (id TEXT, ts TEXT, trial_code TEXT, "
            "session_id TEXT, event TEXT, payload TEXT, org_id TEXT, org_kind TEXT)")
        self.conn.execute("INSERT INTO event_log (trial_code) VALUES ('T03')")
        self.conn.commit()
        result = import_survey.import_file(self.path, self.conn)
        warned = set(result["warnings"])
        # 用前缀判（不能 "T03" in w —— T07 的警文案里带示例「T03/T30」）
        self.assertFalse(any(w.startswith("⚠️ T03") for w in warned))  # 有埋点 → 不警
        self.assertTrue(any(w.startswith("⚠️ T07") for w in warned))   # 查无埋点 → 警告


class TestReportOutput(unittest.TestCase):
    """合并报告：埋点四类 + 问卷各题分布，一条命令全出（docs/32 §5/§6）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "report.db")
        conn = connect(self.db)
        import_survey.import_file(_write(self.tmp.name, "s.json", VALID_SURVEY), conn)
        conn.close()

    def test_merged_report_has_both_sections(self):
        merged = import_survey.merged_report(self.db)
        self.assertIn("telemetry", merged)   # 埋点四类（复用 telemetry_report.report）
        self.assertIn("survey", merged)
        for key in ("participants", "duration", "completion", "satisfaction"):
            self.assertIn(key, merged["telemetry"])
        self.assertEqual(merged["survey"]["n"], 2)

    def test_printed_report_contains_all_four_metrics_and_survey(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            import_survey.print_merged_report(self.db)
        out = buf.getvalue()
        for label in ("试用人数", "平均使用时长", "任务完成率", "满意度"):
            self.assertIn(label, out)
        for q in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6"):
            self.assertIn(q, out)
        self.assertIn("样本量提示", out)          # n=2 < 30 必须提示（docs/08 §4.2）
        self.assertIn("不报百分比", out)
        # n<30 时问卷区块不得出现任何百分数（只有绝对数 + 分布）
        survey_part = out.split("纸质问卷报告", 1)[1]
        self.assertIsNone(re.search(r"\d+(\.\d+)?%", survey_part), msg=survey_part)
        self.assertIn("[T03] conflict list takes too long to load", out)  # 开放题摘录

    def test_empty_survey_prints_hint(self):
        empty_db = os.path.join(self.tmp.name, "empty.db")
        connect(empty_db).close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            import_survey.print_merged_report(empty_db)
        out = buf.getvalue()
        self.assertIn("回收份数 n      ：0", out)
        self.assertIn("满意度           ：暂无数据（n=0）", out)  # 埋点侧满意度原样（能力不退化）


class TestCli(unittest.TestCase):
    """CLI 子命令（check / import / report）端到端，用子进程真实跑。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "cli.db")
        connect(self.db).close()  # 先建空库文件：CLI 对不存在的 --db 会拒绝（防打错路径）
        self.good = _write(self.tmp.name, "good.json", VALID_SURVEY)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(_ROOT, "scripts", "import_survey.py"), *args],
            capture_output=True, text=True, cwd=_ROOT, timeout=120)

    def test_cli_check_then_import_then_report(self):
        r = self._run("check", "--file", self.good, "--db", self.db)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("校验通过", r.stdout)
        r = self._run("import", "--file", self.good, "--db", self.db)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("已导入 2 份", r.stdout)
        r = self._run("report", "--db", self.db)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        for label in ("试用人数", "平均使用时长", "任务完成率", "满意度", "Q1", "Q4"):
            self.assertIn(label, r.stdout)

    def test_cli_invalid_import_exits_1_with_details(self):
        bad = {"survey_version": 1,
               "responses": [{"trial_code": "T09", "q1": "yes", "q2": "yes",
                              "q3": 1, "q4": 9}]}
        path_bad = _write(self.tmp.name, "bad.json", bad)
        r = self._run("import", "--file", path_bad, "--db", self.db)
        self.assertEqual(r.returncode, 1)
        self.assertIn("T09", r.stderr)   # 哪一份
        self.assertIn("q4", r.stderr)    # 哪一题
        self.assertIn("未入库", r.stderr)  # 整文件拒绝
        # 失败后库里必须什么都没有（表都还没建 = 0 行）
        conn = connect(self.db)
        try:
            import_survey.ensure_survey_table(conn)
            n = conn.execute("SELECT COUNT(*) AS c FROM survey_responses").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
