"""纸质问卷 → 结构化录入 → 统计报告（试用满意度主渠道）。

用法：
    python scripts/import_survey.py check   --file surveys.json [--db data/trial.db]
    python scripts/import_survey.py import --file surveys.json [--db data/trial.db]
    python scripts/import_survey.py report [--db data/trial.db]

口径唯一依据：docs/08-试用组织数据口径.md §4（问卷章节）。
格式与统计口径的详细定义见 docs/32-试用问卷录入与统计口径.md。

设计要点（答辩口径）：
  1. 独立表 survey_responses（本脚本自建，幂等）：不写进 schema.sql（他人独占
     文件，领域层红线），也不复用 event_log——其 payload 白名单只认 score 1..5，
     行为化答案与开放题放不进去；且问卷要按 trial_code 幂等覆盖，生命周期与
     append-only 事件流完全不同（docs/32 §4 有完整四条理由）。
  2. 单一口径源：泄漏词表 import 自 telemetry_report.LEAK_WORDS、四类埋点聚合
     import 自 telemetry_report.report()，本脚本一律不自算第二份（防漂移）。
  3. 两阶段导入：先全量校验（格式 + 隐私词表），任一份有问题则整文件拒绝，
     绝不静默丢弃；错误逐条指明「第几份 / 编号 / 哪题 / 收到什么值」。
  4. 幂等：同一编号跨文件重导 → 覆盖（重录修错）；同文件内重复编号 → 报错
     （大概率转录笔误 T03/T30）。行为确定，写死在 docs/32 §2.4。

只用 Python 标准库。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "src"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scheduler.db import connect  # noqa: E402
import telemetry_report  # noqa: E402  # 单一来源：LEAK_WORDS 与 report()

DEFAULT_DB = os.path.join(_ROOT, "data", "trial.db")

#: 泄漏词表（单一来源，import 而非复制——防两处漂移；docs/32 §3）
LEAK_WORDS = telemetry_report.LEAK_WORDS

#: 样本量门槛：n < 30 时不报百分比，只报绝对数 + 分布（docs/08 §4.2）
SAMPLE_SIZE_MIN = 30

#: 短码规则：与 telemetry_report 的 trial_code 短码同口径（T01..T999）
_TRIAL_CODE_RE = re.compile(r"^T\d{1,3}$")

_Q12_ENUM = ("yes", "unsure", "no")
_Q12_CN = {"yes": "是", "unsure": "不确定", "no": "否"}
_Q3_VALUES = (0, 1, 2, 3)
_Q4_MIN, _Q4_MAX = 1, 5
_OPEN_MAX_CHARS = 500
_ENTRY_KEYS = ("trial_code", "q1", "q2", "q3", "q4", "q5", "q6")
_CSV_HEADER = list(_ENTRY_KEYS)

#: 中文（开放题允许，结构化字段零中文由格式校验保证）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

#: 纸质问卷六题的中文标签（报告展示用；题号对应 docs/08 §4.2）
_QUESTION_LABELS = {
    "q1": "Q1 下周还要排课，愿意用本系统代替现有方式",
    "q2": "Q2 会主动向同组老师推荐",
    "q3": "Q3 「一键消解」方案实际采用了几个",
    "q4": "Q4 操作是否顺畅（不需要人教就会用）",
    "q5": "Q5 最想吐槽的一个功能点（开放）",
    "q6": "Q6 最想改进的一点（开放）",
}

_DDL = """
CREATE TABLE IF NOT EXISTS survey_responses (
    trial_code  TEXT PRIMARY KEY,
    q1          TEXT NOT NULL,
    q2          TEXT NOT NULL,
    q3          INTEGER NOT NULL,
    q4          INTEGER NOT NULL,
    q5          TEXT,
    q6          TEXT,
    imported_at TEXT NOT NULL
)
"""


class SurveyImportError(ValueError):
    """录入文件校验失败。problems 为逐条错误文本（含编号与题号），整文件拒绝。"""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("录入文件校验失败：\n  - " + "\n  - ".join(self.problems))


# ---------------------------------------------------------------------------
# 建表（幂等，先例：telemetry.ensure_event_log）
# ---------------------------------------------------------------------------


def ensure_survey_table(conn: sqlite3.Connection) -> None:
    """确保 survey_responses 表存在（幂等）。表结构见 docs/32 §4。"""
    conn.execute(_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# 读取录入文件（JSON / CSV → 统一的 entry dict 列表）
# ---------------------------------------------------------------------------


def _coerce_int(value, key: str) -> tuple[object, str | None]:
    """q3/q4 容差转换：int 直接用；纯数字字符串转 int；其余报错。"""
    if isinstance(value, bool):
        return value, f"{key} 必须是整数（收到 {value!r}）"
    if isinstance(value, int):
        return value, None
    if isinstance(value, str) and re.match(r"^\d+$", value.strip()):
        return int(value.strip()), None
    return value, f"{key} 必须是整数（收到 {value!r}）"


def load_file(path: str) -> list[dict]:
    """读录入文件，返回 entry 列表（不做校验，校验在 validate_entries）。

    - .json：顶层 {"survey_version": 1, "responses": [ {...}, ... ]}
    - .csv ：表头固定 _CSV_HEADER，一行一份问卷，utf-8-sig（兼容 Excel BOM）
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, "r", encoding="utf-8-sig") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            raise SurveyImportError(["JSON 顶层必须是对象（收到 %s）" % type(doc).__name__])
        version = doc.get("survey_version", 1)
        if version != 1:
            raise SurveyImportError([f"不支持的问卷格式版本 survey_version={version!r}（当前只支持 1）"])
        responses = doc.get("responses")
        if not isinstance(responses, list):
            raise SurveyImportError(["JSON 缺少 responses 数组"])
        entries = []
        for i, item in enumerate(responses, 1):
            if not isinstance(item, dict):
                raise SurveyImportError([f"第 {i} 份不是对象（收到 {type(item).__name__}）"])
            entries.append(dict(item))
        return entries
    if ext == ".csv":
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            if [h.strip() for h in header] != _CSV_HEADER:
                raise SurveyImportError(
                    [f"CSV 表头必须是 {','.join(_CSV_HEADER)}（收到 {','.join(header)}）"])
            entries = []
            for i, row in enumerate(reader, 1):
                if all((v or "").strip() == "" for v in row.values()):
                    continue  # 空行跳过
                e = {}
                for k in _ENTRY_KEYS:
                    v = row.get(k)
                    if isinstance(v, str):
                        v = v.strip()
                        if k in ("q3", "q4") and re.match(r"^\d+$", v):
                            v = int(v)  # CSV 数字串容差转 int，与 JSON 同构
                    e[k] = v
                entries.append(e)
        return entries
    raise SurveyImportError([f"不支持的文件扩展名 {ext!r}（只支持 .json / .csv）"])


# ---------------------------------------------------------------------------
# 校验（格式 + 隐私词表；两阶段导入的第一阶段）
# ---------------------------------------------------------------------------


def _privacy_problems(text: str, *, idx: int, code: str, key: str, logins: set) -> list[str]:
    """开放题隐私自检：泄漏词表 + 登录名。返回问题文本列表（docs/32 §3）。"""
    problems = []
    hits = [w for w in LEAK_WORDS if w in text]
    if hits:
        problems.append(
            f"第 {idx} 份（{code}）{key}：命中泄漏词表 {hits} —— "
            f"疑似含校名/年级/班级/姓名等，请回纸质转录环节改写脱敏后重导")
    for login in sorted(logins):
        if login and login in text:
            problems.append(f"第 {idx} 份（{code}）{key}：含登录名 {login!r} —— 禁止入库")
    return problems


def validate_entries(entries: list[dict], *, logins: set | None = None) -> list[str]:
    """全量校验，返回问题文本列表（空列表 = 全部合法）。

    结构化字段零中文（docs/32 §3 规则 1）；开放题允许中文但过泄漏词表（规则 2）。
    """
    logins = logins if logins is not None else set()
    problems: list[str] = []
    seen: dict[str, int] = {}
    for idx, e in enumerate(entries, 1):
        # 未知字段（拼错的键）一律报错，防「写错题号→该题被静默忽略」
        unknown = sorted(set(e) - set(_ENTRY_KEYS))
        if unknown:
            problems.append(f"第 {idx} 份：含未知字段 {unknown}（合法字段 {list(_ENTRY_KEYS)}）")

        code = e.get("trial_code")
        code_s = code.strip() if isinstance(code, str) else code
        if not isinstance(code_s, str) or not _TRIAL_CODE_RE.match(code_s):
            problems.append(
                f"第 {idx} 份：trial_code 必须是短码 T01..T999（收到 {code!r}）")
            code_s = "<invalid>"
        elif code_s in seen:
            problems.append(
                f"第 {idx} 份：编号 {code_s} 与第 {seen[code_s]} 份重复 —— "
                f"同一文件内重复编号应报错（大概率转录笔误，docs/32 §2.4）")
        else:
            seen[code_s] = idx

        # q1 / q2：三值枚举（是/不确定/否 → yes/unsure/no，docs/32 §2.3）
        for key in ("q1", "q2"):
            v = e.get(key)
            if not isinstance(v, str) or v.strip() not in _Q12_ENUM:
                problems.append(
                    f"第 {idx} 份（{code_s}）{key}（{_QUESTION_LABELS[key]}）："
                    f"必须是 yes/unsure/no（收到 {v!r}）")
        # q3：0..3
        v3, err = _coerce_int(e.get("q3"), "q3")
        if err:
            problems.append(f"第 {idx} 份（{code_s}）{err}")
        elif v3 not in _Q3_VALUES:
            problems.append(
                f"第 {idx} 份（{code_s}）q3（{_QUESTION_LABELS['q3']}）："
                f"必须是 0/1/2/3（收到 {v3!r}）")
        # q4：1..5
        v4, err = _coerce_int(e.get("q4"), "q4")
        if err:
            problems.append(f"第 {idx} 份（{code_s}）{err}")
        elif not (_Q4_MIN <= v4 <= _Q4_MAX):
            problems.append(
                f"第 {idx} 份（{code_s}）q4（{_QUESTION_LABELS['q4']}）："
                f"必须是 1..5 分（收到 {v4!r}）")
        # q5 / q6：开放题，可空；限长 + 隐私词表
        for key in ("q5", "q6"):
            v = e.get(key)
            if v is None or (isinstance(v, str) and v.strip() == ""):
                continue
            if not isinstance(v, str):
                problems.append(
                    f"第 {idx} 份（{code_s}）{key}：必须是文本或留空（收到 {type(v).__name__}）")
                continue
            if len(v) > _OPEN_MAX_CHARS:
                problems.append(
                    f"第 {idx} 份（{code_s}）{key}：超过 {_OPEN_MAX_CHARS} 字"
                    f"（当前 {len(v)} 字）——请摘录要点而非全文照抄")
            problems.extend(_privacy_problems(v, idx=idx, code=code_s, key=key, logins=logins))
    return problems


def _known_logins(conn: sqlite3.Connection) -> set:
    """users 表存在则返回登录名集合（开放题隐私兜底），否则空集（纯问卷库场景）。"""
    try:
        return {r["login"] for r in conn.execute("SELECT login FROM users").fetchall()}
    except sqlite3.OperationalError:
        return set()


def _known_trial_codes(conn: sqlite3.Connection) -> set | None:
    """event_log 已有 trial_code 集合；表不存在返回 None（跳过对齐检查）。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'event_log'"
    ).fetchone()
    if row is None:
        return None
    return {r[0] for r in conn.execute("SELECT DISTINCT trial_code FROM event_log")}


# ---------------------------------------------------------------------------
# 导入（两阶段：全量校验 → 写库）
# ---------------------------------------------------------------------------


def import_file(path: str, conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """导入录入文件。返回 {"imported": n, "warnings": [...]}。

    - 校验失败抛 SurveyImportError（整文件拒绝、零写入）；
    - 幂等：同编号覆盖（INSERT OR REPLACE），docs/32 §2.4；
    - warnings：trial_code 不在 event_log 的编号（转录笔误 / 未触发埋点，不阻断）。
    """
    entries = load_file(path)
    problems = validate_entries(entries, logins=_known_logins(conn))
    if problems:
        raise SurveyImportError(problems)

    # 对齐 warning（不阻断）：编号不在 event_log → 可能转录笔误或未触发埋点
    known = _known_trial_codes(conn)
    warnings: list[str] = []
    if known is not None:
        for e in entries:
            code = e["trial_code"].strip()
            if code not in known:
                warnings.append(
                    f"⚠️ {code} 不在 event_log —— 可能是转录笔误（如 T03/T30），"
                    f"或该教师填了问卷但未触发埋点，请人工核对")
    if dry_run:
        return {"imported": 0, "warnings": warnings, "checked": len(entries)}

    ensure_survey_table(conn)
    now = datetime.now().isoformat(timespec="seconds")
    imported = 0
    for e in entries:
        code = e["trial_code"].strip()
        conn.execute(
            "INSERT OR REPLACE INTO survey_responses "
            "(trial_code, q1, q2, q3, q4, q5, q6, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                code,
                e["q1"].strip(),
                e["q2"].strip(),
                _coerce_int(e["q3"], "q3")[0],
                _coerce_int(e["q4"], "q4")[0],
                (e.get("q5") or None) if isinstance(e.get("q5"), str) and e.get("q5").strip() else None,
                (e.get("q6") or None) if isinstance(e.get("q6"), str) and e.get("q6").strip() else None,
                now,
            ),
        )
        imported += 1
    conn.commit()
    return {"imported": imported, "warnings": warnings}


# ---------------------------------------------------------------------------
# 统计（docs/08 §4.2：每题给分布，绝不止均值；n<30 不报百分比）
# ---------------------------------------------------------------------------


def survey_stats(conn: sqlite3.Connection) -> dict:
    """纸质问卷统计。只出聚合数与编号+开放题原文（已过词表），无任何姓名字段。"""
    ensure_survey_table(conn)
    rows = conn.execute(
        "SELECT trial_code, q1, q2, q3, q4, q5, q6 FROM survey_responses "
        "ORDER BY trial_code"
    ).fetchall()
    n = len(rows)
    q4_scores = [r["q4"] for r in rows]
    mean = round(sum(q4_scores) / n, 2) if n else None
    std = (
        round(math.sqrt(sum((s - sum(q4_scores) / n) ** 2 for s in q4_scores) / n), 2)
        if n >= 2 else None  # 总体标准差（docs/32 §6）；n<2 无意义不报
    )
    return {
        "n": n,
        "q1_dist": {k: sum(1 for r in rows if r["q1"] == k) for k in _Q12_ENUM},
        "q2_dist": {k: sum(1 for r in rows if r["q2"] == k) for k in _Q12_ENUM},
        "q3_dist": {k: sum(1 for r in rows if r["q3"] == k) for k in _Q3_VALUES},
        "q4": {
            "n": n,
            "mean": mean,
            "std": std,
            "distribution": {i: sum(1 for s in q4_scores if s == i) for i in range(_Q4_MIN, _Q4_MAX + 1)},
            "ge4_count": sum(1 for s in q4_scores if s >= 4),
        },
        "q5_excerpts": [{"trial_code": r["trial_code"], "text": r["q5"]} for r in rows if r["q5"]],
        "q6_excerpts": [{"trial_code": r["trial_code"], "text": r["q6"]} for r in rows if r["q6"]],
        "sample_limit": 0 < n < SAMPLE_SIZE_MIN,  # docs/08 §4.2：<30 无统计效力
    }


def print_survey_section(stats: dict) -> None:
    """打印纸质问卷区块（docs/08 §4.2 + docs/32 §6 口径）。"""
    print("=" * 60)
    print("纸质问卷报告（满意度主渠道 · docs/08 §4 / docs/32）")
    print("=" * 60)
    n = stats["n"]
    if n == 0:
        print("回收份数 n      ：0（尚未导入问卷，导入命令见 docs/32 §5）")
        print("=" * 60)
        return
    print(f"回收份数 n      ：{n}（survey_responses 按编号去重）")
    if stats["sample_limit"]:
        print(
            f"⚠️ 样本量提示    ：n={n} < {SAMPLE_SIZE_MIN}，不具统计效力；"
            f"以下全部为绝对数与分布，不报百分比（docs/08 §4.2：主动说明比被问出来安全）")
    for key, dist_key in (("q1", "q1_dist"), ("q2", "q2_dist")):
        d = stats[dist_key]
        parts = " / ".join(f"{_Q12_CN[k]} {d[k]} 人" for k in _Q12_ENUM)
        print(f"{_QUESTION_LABELS[key]}：{parts}")
        if stats["sample_limit"]:
            top = max(_Q12_ENUM, key=lambda k: d[k])
            print(
                f"   → 表述：「{n} 人中 {d[top]} 人回答「{_Q12_CN[top]}」」"
                f"（绝对数 + 分布，勿用百分比）")
    d3 = stats["q3_dist"]
    print(
        f"{_QUESTION_LABELS['q3']}："
        + " / ".join(f"用 {k} 个 {d3[k]} 人" for k in _Q3_VALUES))
    q4 = stats["q4"]
    std_txt = "-" if q4["std"] is None else f"{q4['std']}"
    print(
        f"{_QUESTION_LABELS['q4']}：n={n}，均值 {q4['mean']}，"
        f"标准差 {std_txt}（总体标准差，n<2 不报）")
    print(
        "   分布："
        + " / ".join(f"{i} 分×{q4['distribution'][i]} 人" for i in range(1, 6)))
    print(f"   ≥4 分：{q4['ge4_count']} 人" + (
        "（n<30：占比仅供内部参考，不进对外材料）" if stats["sample_limit"] else ""))
    for key, excerpts in (("q5", stats["q5_excerpts"]), ("q6", stats["q6_excerpts"])):
        print(f"{_QUESTION_LABELS[key]}：")
        if excerpts:
            for ex in excerpts:
                print(f"   [{ex['trial_code']}] {ex['text']}")
        else:
            print("   （无作答）")
    print("=" * 60)


# ---------------------------------------------------------------------------
# 合并报告（一条命令打出四类埋点指标 + 问卷各题分布）
# ---------------------------------------------------------------------------


def merged_report(db_path: str) -> dict:
    """返回埋点四类聚合 + 问卷统计的完整 dict（不打印；给测试与 API 化留口）。"""
    conn = connect(db_path)
    try:
        agg = telemetry_report.report(db_path)  # 单一口径源（内部自己开关连接）
        stats = survey_stats(conn)
    finally:
        conn.close()
    return {"telemetry": agg, "survey": stats}


def print_merged_report(db_path: str) -> None:
    """一条命令的完整出口：埋点四类（复用 telemetry_report 打印）+ 纸质问卷区块。

    🔴 刻意借用 telemetry_report._print_report（私有函数）而不是自己再写一遍
    四类指标的打印：同一口径只实现一份，杜绝两处格式漂移（docs/32 §7）。
    """
    merged = merged_report(db_path)
    telemetry_report._print_report(merged["telemetry"])
    print()
    print_survey_section(merged["survey"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="纸质问卷录入与统计（docs/32；口径依据 docs/08 §4）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check", help="只校验不落库（dry-run）")
    p_check.add_argument("--file", required=True, help="录入文件（.json / .csv）")
    p_imp = sub.add_parser("import", help="校验并导入（幂等：同编号覆盖）")
    p_imp.add_argument("--file", required=True, help="录入文件（.json / .csv）")
    p_rep = sub.add_parser("report", help="合并报告：埋点四类 + 问卷各题分布")
    for p in (p_check, p_imp, p_rep):
        p.add_argument("--db", default=DEFAULT_DB, help="数据库路径（默认 data/trial.db）")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"库不存在：{args.db}", file=sys.stderr)
        return 1

    if args.cmd == "report":
        print_merged_report(args.db)
        return 0

    conn = connect(args.db)
    try:
        result = import_file(args.file, conn, dry_run=(args.cmd == "check"))
    except SurveyImportError as exc:
        print("❌ 校验未通过，整份文件未入库（一条都不写）：", file=sys.stderr)
        for p in exc.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"❌ 文件无法解析：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if args.cmd == "check":
        print(f"✅ 校验通过：{result['checked']} 份问卷全部合法（未写库）")
        for w in result.get("warnings", []):
            print(w)
        return 0

    print(f"✅ 已导入 {result['imported']} 份问卷（重复编号覆盖，docs/32 §2.4）")
    for w in result["warnings"]:
        print(w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
