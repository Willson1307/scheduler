"""B-6（rn6JxF）：问答层三层降级内核 —— 意图表 / 关键词检索 / LLM 润色。

用途与设计意图（答辩口径）：
  1. **AI 只在最外层**，这是本项目对外表述的边界（AGENTS §9）：冲突检测与消解由
     自建求解器完成，LLM 只用于「把手册段落润色成人话」。所以本模块把链路切成三层：

         第 0 层  意图表（手工 25 条，同义词手写）→ 直接返回手册原文
         第 1 层  关键词检索（纯标准库，无向量库）→ top3 段落原文，mode="retrieval"
         第 2 层  LLM 润色（可选，配了 env 才启用）→ mode="llm"

     🔴 **第 0、1 层必须离线可用**：一个模型都没接，问「怎么调课」也要拿到准确答案。
  2. 🔴 **不许编**（比什么都重要）：三层都没命中 → 固定回「手册里没有这条，请找项目
     负责人确认」。⛔ 不推测、不拼凑步骤、不拿邻近段落的只言片语凑一个答案。
     这是本卡最容易做错的地方 —— 一个「看起来很会答」的问答层，比答不出来危险得多。
  3. 🔴 **任何情况不抛、不 500**：文件缺失 → 「手册未就绪」；LLM 挂/超时/未配置 →
     降级到第 1 层且 `mode` 如实写。降级不是失败，**假装成功才是失败**。
  4. 🔴 **安全边界**：key/url 一律不碰（全在 `b_llm_client`，跨人契约见 AGENTS §4.1），
     本文件只判断「有没有配」，不读取其值、不拼 URL、不发请求。
     开 LLM 前由本层做脱敏（`scrub`）：`X 老师` / `X 班` / 对象 id 全部换占位符，
     **不传课表、不传学生名单**（本层结构上就拿不到它们）。
  5. 🔴 **AI 只能回答/解释，不能产出「改哪条日程」**：本模块不读写任何表，
     无法也绝不返回「把 sch_x 挪到周三第 3 节」这类指令；引导用户走现有流程。
  6. 不注册路由（文件名不带 `api_` 前缀，`routes.py` 的 glob 扫不到，
     见 AGENTS §3.1 命名铁律）。只用 Python 标准库。

`mode` 三态口径（🔴 卡面要求必填，前端要能显示「本次回答来自手册原文（未启用 AI）」）：
  - `retrieval`：**回答内容来自手册原文**（第 0 层意图表命中也算这一档）。
    对前端而言「意图表命中」与「关键词命中」是同一件事：都是手册原话、都没过模型，
    所以不拆成两档，避免前端为一个内部实现细节写分支。
  - `template` ：**固定模板文案**，即「手册里没有这条」「手册未就绪」「请先输入问题」。
    刻意与 `retrieval` 分开：不能让「答不出来」看起来像「检索结果」。
  - `llm`      ：第 2 层模型润色成功。

已知边界（如实，不夸大）：
  - 检索是**词面匹配**，不是语义匹配：同义但用词完全不同的问法会落到「没有这条」。
    这是刻意的取舍 —— 宁可说不知道，也不引入向量库/embedding（零依赖 + 断网可跑）。
  - 语料只含 `docs/30` 与 `docs/07` 两个文件（卡面指定）。手册里没有的内容，
    本层一律答「没有这条」，即便系统确实有该能力（如 `/api/slots` 空位查询，
    文档尚未收录）——**能力存在 ≠ 手册里有**，不许拿实现细节冒充手册原文。
"""

from __future__ import annotations

import math
import os
import re

# ---------------------------------------------------------------------------
# 语料与常量
# ---------------------------------------------------------------------------

#: 语料清单：(对外标识, 仓库内相对路径)。顺序即并列时的优先序。
CORPUS_FILES: tuple[tuple[str, str], ...] = (
    ("docs/30", os.path.join("docs", "30-教师试用执行手册.md")),
    ("docs/07", os.path.join("docs", "07-API契约.md")),
)

#: 🔴 检索取前 N 段（卡面写死 top3）。
TOP_K = 3

#: 单段送进 LLM 的最大字符数（控制请求体，防止整节塞进去把上下文撑爆）。
MAX_PASSAGE_CHARS = 900

#: 🔴 答不出来时的固定文案（卡面原话，验收第 3 条逐字比对）。
NO_ANSWER = (
    "手册里没有这条，请找项目负责人确认。\n"
    "（本问答只回答教师试用手册与 API 契约里写明的内容，"
    "手册没写的一律不推测、不编步骤。）"
)

#: 语料缺失时的固定文案（卡面：两文件缺失 → 返回「手册未就绪」，不报 500）。
CORPUS_MISSING = (
    "手册未就绪：服务端没有找到 docs/30 或 docs/07，本问题暂时无法回答。"
    "请找项目负责人确认。"
)

#: 空问题。
EMPTY_QUESTION = "请先输入问题。本问答只回答手册里写明的内容。"

#: 检索命中门槛（明细见 `_score_sections` 注释与「已知边界」）：
#: 判据是「命中了内容词，而不是『怎么』『为什么』这种到处都有的字」。
MIN_RARE_HITS = 2
MIN_WEIGHT_SCORE = 3.0


# ---------------------------------------------------------------------------
# 仓库根
# ---------------------------------------------------------------------------

def repo_root() -> str:
    """仓库根：src/server/b_ai_kernels.py 上溯三级，与 config.repo_root() 同手法。

    🔴 锚定文件位置而非当前工作目录 —— 否则从 `scripts/` 里跑测试会读不到 docs。
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# 语料载入：按 `##` 切段
# ---------------------------------------------------------------------------

#: 二级标题编号：「## 5.5 🔴 故障对照表」→ "5.5"；「## 填写规则」→ 无编号。
_HEADING_NUM = re.compile(r"^\s*#*\s*(?:[^\w\u4e00-\u9fff]*)(\d+(?:\.\d+)*)")


def split_sections(doc_id: str, text: str) -> list[dict]:
    """把一份 Markdown 按 `## ` 二级标题切段，返回 [{doc, title, section, text}]。

    只认二级标题（`##`）：三级标题（`### A. 登录相关`）留在所属二级段内，
    否则「故障对照表」会被切成一堆互不相关的碎片，检索反而更差。

    `section` 是前端 `sources` 里展示的短标，形如 `§5.5`（卡面示例即 `§3`）。
    """
    sections: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            m = _HEADING_NUM.match(heading)
            label = f"§{m.group(1)}" if m else f"§{heading[:12]}"
            current = {"doc": doc_id, "section": label, "title": heading, "lines": [heading]}
            sections.append(current)
        elif current is not None:
            current["lines"].append(line)
    for s in sections:
        s["text"] = "\n".join(s.pop("lines")).strip()
    return [s for s in sections if s["text"]]


_CORPUS: list[dict] | None = None
_CORPUS_LOADED = False


def load_corpus(force: bool = False) -> list[dict] | None:
    """载入并缓存语料（进程内一次）。

    返回 None 表示「手册未就绪」（任一文件读不到）—— 调用方据此回固定文案，
    ⛔ 不是抛异常。只读到一份也不干：半套手册给出的答案缺一半校验，
    比明确说「未就绪」更危险。
    """
    global _CORPUS, _CORPUS_LOADED
    if _CORPUS_LOADED and not force:
        return _CORPUS

    root = repo_root()
    sections: list[dict] = []
    try:
        for doc_id, rel in CORPUS_FILES:
            with open(os.path.join(root, rel), "r", encoding="utf-8") as f:
                sections.extend(split_sections(doc_id, f.read()))
    except OSError:
        _CORPUS = None
        _CORPUS_LOADED = True
        return None

    _CORPUS = sections or None  # 目录能读但切不出段 → 同样算未就绪
    _CORPUS_LOADED = True
    return _CORPUS


def reset_corpus_cache() -> None:
    """清缓存（测试用：模拟语料缺失 / 换语料后重新载入）。"""
    global _CORPUS, _CORPUS_LOADED, _INDEX, _INDEX_SOURCE
    _CORPUS = None
    _CORPUS_LOADED = False
    _INDEX = None
    _INDEX_SOURCE = None


# ---------------------------------------------------------------------------
# 分词：CJK 二元组 + ASCII 词。零依赖，不装分词库（断网可跑）
# ---------------------------------------------------------------------------

_ASCII_TOKEN = re.compile(r"[a-z][a-z0-9_./-]{1,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> set[str]:
    """切词：中文取连续汉字段的二元组，英文取长度≥2 的词。

    为什么用二元组而不是单字：单字（「的」「课」）几乎每段都有，权重全被稀释；
    二元组（「调课」「消解」）已能表达大部分词，且不需要词典/分词器。
    """
    lowered = text.lower()
    tokens = {m.group(0) for m in _ASCII_TOKEN.finditer(lowered)}
    for run in _CJK_RUN.findall(lowered):
        for i in range(len(run) - 1):
            tokens.add(run[i:i + 2])
    return tokens


# ---------------------------------------------------------------------------
# 第 1 层：关键词检索
# ---------------------------------------------------------------------------

def _build_index(sections: list[dict]) -> dict:
    """建倒排统计（文档频率 df），供 IDF 加权。"""
    token_sets = [tokenize(s["text"]) for s in sections]
    df: dict[str, int] = {}
    for ts in token_sets:
        for tok in ts:
            df[tok] = df.get(tok, 0) + 1
    return {"n": len(sections), "token_sets": token_sets, "df": df}


_INDEX: dict | None = None
_INDEX_SOURCE: int | None = None


def _index_for(sections: list[dict]) -> dict:
    """按语料对象身份缓存索引（reset_corpus_cache 后自动重建）。"""
    global _INDEX, _INDEX_SOURCE
    if _INDEX is None or _INDEX_SOURCE != id(sections):
        _INDEX = _build_index(sections)
        _INDEX_SOURCE = id(sections)
    return _INDEX


def _score_sections(question: str, sections: list[dict]) -> list[dict]:
    """给每段打分，返回按分数降序的命中明细。

    打分口径（无向量库，纯词面）：
      - IDF 权重 `log(1 + N/df)`：到处都有的词（「怎么」「为什么」）权重接近 0，
        只在少数段落出现的词（「调休」「埋点」）权重高；
      - `rare_hits`：命中的「稀有词」个数（df ≤ 2）。**这是真正的准入门槛** ——
        一段如果只命中满篇都是的字眼，说明它跟问题没关系，不能拿它作答；
      - `weight`：命中词权重之和，用于排序与次级门槛。

    🔴 门槛设成「至少 2 个稀有词」而不是「分数 > 0」：后者会让任何问题都能匹配到
    某一段，于是「手册里没有的也硬答一句」，正好踩中卡面「不许编」红线。
    """
    index = _index_for(sections)
    n = index["n"]
    df = index["df"]
    q_tokens = tokenize(question)
    if n == 0 or not q_tokens:
        return []

    # 只保留在语料中出现过的词（未出现的词不参与，充当隐式停用词过滤）
    weights = {t: math.log(1.0 + n / df[t]) for t in q_tokens if t in df}

    scored: list[dict] = []
    for i, sec in enumerate(sections):
        sec_tokens = index["token_sets"][i]
        hits = [t for t in weights if t in sec_tokens]
        if not hits:
            continue
        rare = [t for t in hits if df[t] <= 2]
        scored.append({
            "section": sec["section"],
            "doc": sec["doc"],
            "title": sec["title"],
            "text": sec["text"],
            "rare_hits": len(rare),
            "hits": len(hits),
            "weight": sum(weights[t] for t in hits),
            "matched": sorted(rare),
        })

    scored.sort(key=lambda d: (-d["rare_hits"], -d["weight"], -d["hits"]))
    return scored


def retrieve(question: str, top_k: int = TOP_K) -> list[dict]:
    """关键词检索：返回通过门槛的 top-k 段落（未过门槛 → 空列表）。

    ⛔ 空列表就是「手册里没有这条」的唯一信号，调用方不许自己兜底编答案。
    """
    sections = load_corpus()
    if not sections:
        return []
    scored = _score_sections(question, sections)
    if not scored:
        return []
    best = scored[0]
    if best["rare_hits"] < MIN_RARE_HITS or best["weight"] < MIN_WEIGHT_SCORE:
        return []
    # 只带与最佳段同一水平的（稀有词比最佳少 2 个以上 → 明显不相关，不带进来）
    floor = max(MIN_RARE_HITS, best["rare_hits"] - 1)
    picked = [d for d in scored if d["rare_hits"] >= floor][:top_k]
    for d in picked:
        d["text"] = d["text"][:MAX_PASSAGE_CHARS]
    return picked


def _retrieval_answer(hits: list[dict]) -> str:
    """第 1 层作答：把命中的手册原文按段拼出来（不改写、不总结）。

    🔴 必须让用户看出「这是手册原文」：所以有抬头、有出处标注、段落原样贴。
    写明「未启用 AI 润色」是诚信要求 —— 不能让老师以为所有回答都是 AI 生成的。
    """
    blocks = ["以下按手册原文摘录回答（本次未启用 AI 润色）："]
    for h in hits:
        blocks.append(f"【{h['doc']} {h['section']}】\n{h['text']}")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# 第 0 层：意图表（手工同义词，命中即回手册原文，不问模型）
# ---------------------------------------------------------------------------

def _src(doc: str, section: str) -> dict:
    return {"doc": doc, "section": section}


#: 🔴 25 条意图（卡面要求 20–30 条）。
#:
#: 字段口径：
#:   - `strong`  ：**单词即可命中**的判别性说法。写进这里的必须是「出现在问题里就
#:                 基本能确定问的是这件事」的词，宁可少写，也不要把「课」「时间」
#:                 这类通用词塞进来 —— 误命中比漏命中更糟（会给错答案）。
#:   - `keywords`：辅助词，需命中 2 个及以上才算数（避免单个短词误触发）。
#:   - `answer`  ：手册里能支撑的口径。⛔ 不许写手册里没有的步骤 ——
#:                 这层是离线时唯一的答案来源，写虚一句就等于教老师做错事。
#:   - `sources` ：出处（前端要显示「来自哪一节」）。
#:
#: ⚠️ 每条 answer 都刻意**不给操作 ID、不给具体数值以外的推断**：
#:    「AI 只能回答/解释，不能产出改哪条日程」（卡面红线 5）。
INTENTS: tuple[dict, ...] = (
    {
        "id": "reschedule",
        "strong": ["怎么调课", "调课怎么办", "想调课", "课调开", "调一下课", "调课"],
        "keywords": ["调开", "换个时间", "挪到", "改到"],
        "answer": (
            "分两种，别混：\n"
            "① 只影响某一天（这节课取消 / 换个时间）→「临时微调」："
            "支持 cancel（取消）与 move（改点），删掉这条例外就恢复按周期展开，改错能回退。\n"
            "② 改整周的永久课表 → 教师的有效档位是 propose（可提议、不能直接改永久数据），"
            "需由管理员批准，走「提议 → 管理员批准」流程。"
            "页面提示「要审批 / 无权限」是设计如此，不是故障。"
        ),
        "sources": [_src("docs/07", "§11"), _src("docs/30", "§5.5")],
    },
    {
        "id": "cancel_once",
        "strong": ["取消这节课", "临时取消", "取消一节课", "这节课取消"],
        "keywords": ["取消", "不上了"],
        "answer": (
            "一次性取消走「临时微调」：新增一条 mode=cancel 的例外，只影响指定的那一天，"
            "周期课表本身不动；删掉这条例外就恢复按周期展开。"
            "响应里的 effective 字段说明该例外是否真的改变了占用 —— 周六/假期/单双周不匹配时"
            "会写入成功但不生效，此时 effective=false，不是系统坏了。"
        ),
        "sources": [_src("docs/07", "§11")],
    },
    {
        "id": "makeup",
        "strong": ["调休", "补课"],
        "keywords": ["补上", "顺延", "换一天上"],
        "answer": (
            "调休/补课同样走「临时微调」的例外机制（scope=day 的 as_of 模式）。"
            "例外必须被课表展开与冲突校验同时读到 —— 这是本项目的硬约束："
            "只改一处会出现「占用显示空着、排课时按有课处理」。"
        ),
        "sources": [_src("docs/07", "§11")],
    },
    {
        "id": "resolve_how",
        "strong": ["一键消解", "怎么消解", "点了没变化", "点了没反应"],
        "keywords": ["预览", "应用方案", "点应用", "没变化"],
        "answer": (
            "分两步：先点「预览」看方案（只算不动数据），再点「应用」才真正改库。\n"
            "· 预览返回 top-3 方案，每套带 diff_text（改了什么、影响谁）"
            "与 impact（几条日程 / 几个对象）；\n"
            "· 应用后返回 applied / remaining_before / remaining_after / rounds；\n"
            "· 应用有限频：每 token 每分钟 10 次，超限返回 429。\n"
            "点了没变化，多半是只点了预览没点应用。"
        ),
        "sources": [_src("docs/30", "§5.5"), _src("docs/07", "§6")],
    },
    {
        "id": "resolve_need_approval",
        "strong": ["要审批", "需要审批", "无权限", "没有权限"],
        "keywords": ["不能直接改", "凭什么不能改"],
        "answer": (
            "这是设计，不是故障：教师的有效档位是 propose —— 可以提议、不能直接改永久数据。"
            "照「提议 → 管理员批准」流程走，由管理员批准后才生效；"
            "永久类改动走审批，正是为了让「谁能改最终数据」有据可查。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "resolve_remaining",
        "strong": ["冲突没降", "消不掉", "没消完", "消解后还有"],
        "keywords": ["还剩", "没降", "还是冲突"],
        "answer": (
            "属正常：含硬约束的冲突（同一人同一时刻、同一教室）无解，算法会留「消不了」的项。\n"
            "看两个字段讲清楚为什么没消完：termination（no_solution / ok / best_effort，"
            "三种终止态不合并返回）与 filtered_out_count（因不在你的可改范围内、"
            "压根没被尝试求解的冲突处数）。"
        ),
        "sources": [_src("docs/30", "§5.5"), _src("docs/07", "§6")],
    },
    {
        "id": "conflicts_zero",
        "strong": ["看不到冲突", "没有冲突", "冲突为0", "零冲突"],
        "keywords": ["冲突是0", "冲突列表是空"],
        "answer": (
            "先查三种可能：① 本周确实无冲突；② 活动窗口已过期（活动在上一周）；"
            "③ 起服务的日期不对 —— 学期起点锚定 --today 所在周周一，"
            "日期用错会 0 冲突且页面不报错。若周视图本身是空的，先确认选对了组。"
        ),
        "sources": [_src("docs/30", "§5.5"), _src("docs/30", "§1")],
    },
    {
        "id": "empty_groups",
        "strong": ["看不到班级", "组是空的", "下拉框是空的", "没有组可选"],
        "keywords": ["组的列表", "看不到组"],
        "answer": (
            "该账号缺少授权行（group_roles 没有对应记录）。队长查 "
            "`SELECT COUNT(*) FROM group_roles;` 为 0 就是这个问题，"
            "跑 build_trial_org.py 重建即可。老师侧不需要改任何东西。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "login_fail",
        "strong": ["登录失败", "登不进去", "登录不了", "登录不上", "口令错", "密码错"],
        "keywords": ["登不上", "登录报错"],
        "answer": (
            "先按这三条排：① 口令注意大小写，复制时别带首尾空格，让老师重新复制而不是手打；"
            "② 口令确认没错仍失败 → 口令表与服务器不同步，队长跑 "
            "deploy/reset_trial_passwords.py 重发；③ 账号必须带 trial_ 前缀（如 trial_t05），"
            "绝不能用 teacher01 之类。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "login_lock",
        "strong": ["连续失败", "锁住了", "过于频繁", "登录限流", "一分钟后再试"],
        "keywords": ["被锁", "锁多久", "限流"],
        "answer": (
            "有软限制：同一账号 + 同一来源连续失败会锁一段时间，"
            "同一来源短时间请求过多也会被限。做法只有一个 —— 等一会儿再试，"
            "⛔ 不要反复点，越点越锁。\n"
            "对外口径统一为「有软限制，稍等再试」，不要说出具体次数与时长。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "page_not_open",
        "strong": ["页面打不开", "网页打不开", "一直转圈", "连不上服务器"],
        "keywords": ["打不开", "转圈", "别人连不上"],
        "answer": (
            "先判定是服务还是入口：① 服务在跑吗（本机 curl /api/health 应 200）；"
            "② 别人要连必须 `--host 0.0.0.0`，READY 行应显示 host=0.0.0.0 而不是 127.0.0.1；"
            "③ 云服务器要看安全组是否放行 8000；④ 外网是否真通只能手机流量实测 —— "
            "前几项都通了它也可能不通（NAT、运营商策略）。"
        ),
        "sources": [_src("docs/30", "§0.6")],
    },
    {
        "id": "cache_stale",
        "strong": ["缓存", "强刷", "样式错乱", "按钮点不动"],
        "keywords": ["还是旧版", "页面没更新"],
        "answer": (
            "旧缓存：改了 JS 但浏览器还在用旧版。按 Ctrl + F5 强刷，仍不行则清缓存重开。"
            "静态资源都带版本号，版本号一变即强制所有浏览器丢弃旧文件。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "narrow_screen",
        "strong": ["窄屏", "横向滚动", "手机上打开"],
        "keywords": ["手机", "屏幕小"],
        "answer": (
            "窄屏只保证 ≥768px 不崩、不横向滚动。手机 / 小窗出现横向滚动是已知限制，"
            "请老师用电脑浏览器。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "privacy",
        "strong": ["记录我点什么", "我的姓名", "传出去", "删掉我的数据"],
        "keywords": ["隐私", "匿不匿名", "你们能看到"],
        "answer": (
            "不记录姓名 / 班级：埋点只存事件类型与短码，payload 有白名单，中文 / 姓名一律拒收。\n"
            "真实学生课表不会传出去：试用用的是脱敏的「示例学校」组织，不含真实名单；"
            "编号→姓名映射只存纸质本，不入库、不上网。\n"
            "能删：试用结束整库归档后删除。顾虑仍在可当场跑 "
            "`telemetry_report.py --assert-clean` 做隐私自检。"
        ),
        "sources": [_src("docs/30", "§5.5"), _src("docs/30", "§0")],
    },
    {
        "id": "start_server",
        "strong": ["起服务", "启动服务", "三个参数", "怎么启动"],
        "keywords": ["端口", "run_server", "host"],
        "answer": (
            "三个参数缺一不可：`--db data/trial.db`（不带会连到 demo.db，"
            "没有 trial 账号，登录必失败）、`--today 2026-10-12`（不带会 0 冲突且页面不报错）、"
            "`--port 8000`。\n"
            "起对了的标志是 READY 行同时含对的 db= 与 today=。别人要连再加 `--host 0.0.0.0`。"
        ),
        "sources": [_src("docs/30", "§1"), _src("docs/30", "§0.6")],
    },
    {
        "id": "trial_login_name",
        "strong": ["账号是什么", "登录名是什么", "用什么账号"],
        "keywords": ["登录名", "账号怎么给", "编号怎么发"],
        "answer": (
            "账号 = trial_tNN（NN 是编号，如 5 号老师用 trial_t05），"
            "口令见各自的登录卡，一人一份、当面或私发，不发群。"
            "登录名必须带 trial_ 前缀，后端据此推短码并判 org_kind='trial'。"
        ),
        "sources": [_src("docs/30", "§0.5"), _src("docs/30", "§3")],
    },
    {
        "id": "propose_approve",
        "strong": ["谁能批准", "审批流程", "谁来批"],
        "keywords": ["提议", "审批", "批准"],
        "answer": (
            "永久类改动走审批：教师（propose 档，可提议、不可直接改永久数据）提议，"
            "管理员批准后才生效。页面提示「要审批 / 无权限」是设计，不是故障。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "org_isolate",
        "strong": ["一师一组织", "看到别人的", "互相影响"],
        "keywords": ["各看各的", "数据会不会混", "会不会看到其他"],
        "answer": (
            "一师一组织：系统按 org_id 硬隔离，每位老师只看自己的组织，消解互不影响，"
            "看不到彼此的班级和课表。这是设计，不是缺陷 —— "
            "也别跟老师说「冲突是全校的」，会让他以为别人消解了他就没了。\n"
            "验证跑 check_trial_isolation.py，断言要同时比 count 与 raw_count。"
        ),
        "sources": [_src("docs/30", "§0.5")],
    },
    {
        "id": "conflict_why",
        "strong": ["为什么有冲突", "什么叫冲突", "冲突是什么"],
        "keywords": ["硬约束", "软约束", "冲突怎么来"],
        "answer": (
            "冲突 = 同一对象（班级 / 教师 / 教室）在同一时刻被两条日程同时占用。"
            "系统把课表（周期事务）与活动（一次性事务）归一化到同一套时间占用模型后统一检测。"
            "硬冲突（撞课）必须消解；软约束（容量、末节、连堂被拆）只给警告、不阻止，"
            "由用户在方案间权衡。"
        ),
        "sources": [_src("docs/07", "§6")],
    },
    {
        "id": "slot_query",
        "strong": ["有没有空", "空不空", "空位"],
        "keywords": ["哪节空", "有空吗"],
        "answer": (
            "「某天某时段空不空」属「分析安排」类需求（契约里写作 analyze 意图）。\n"
            "⚠️ 但教师试用手册与 API 契约目前都没写明具体的空位查询操作步骤，"
            "这一条手册里没有，请找项目负责人确认入口，不要凭印象操作。"
        ),
        "sources": [_src("docs/07", "§8")],
    },
    {
        "id": "demand_input",
        "strong": ["提需求", "用一句话", "自然语言"],
        "keywords": ["原话", "需求怎么提"],
        "answer": (
            "自然语言需求入口：前端只传原话，解析规则全在后端，改规则不用动前端；"
            "原话原样落库以便追溯、可人工纠正。与现有日程冲突时会明确反馈 "
            "conflicts 与 message，不会静默失败。"
        ),
        "sources": [_src("docs/07", "§7")],
    },
    {
        "id": "telemetry",
        "strong": ["埋点", "怎么统计", "采集数据"],
        "keywords": ["统计数据", "事件记录"],
        "answer": (
            "埋点记录任务发起、成功、放弃三类事件，按编号去重统计人数，"
            "按 5 分钟断线规则剔除挂机时长；数据只出聚合数。"
            "每天收尾跑 telemetry_report.py 看四类数据，并加 --assert-clean 做隐私自检。"
        ),
        "sources": [_src("docs/30", "§5"), _src("docs/30", "§6")],
    },
    {
        "id": "questionnaire",
        "strong": ["问卷", "满意度"],
        "keywords": ["怎么填", "现场填"],
        "answer": (
            "6 题，现场当场填、当场收纸质问卷（纸质原件是真实性物证，答辩可展示）。\n"
            "⚠️ 不要用「整体满意度 1–5 分」当核心题 —— 礼貌性高分经不起追问。"
            "报告必须给分布（n、均值、标准差、各档人数、开放题摘录），不能只给均值；"
            "样本 15 人时不报百分比，改报绝对数。"
        ),
        "sources": [_src("docs/30", "§4")],
    },
    {
        "id": "shutdown",
        "strong": ["关停", "结束试用", "下线实例"],
        "keywords": ["停掉服务", "关掉公网"],
        "answer": (
            "试用结束当日关停公网实例：只改云控制台防火墙（删掉 TCP 8000 入站），"
            "服务本身保持运行，便于随时核查 / 导出。验证要两条：手机流量打不开，"
            "服务器本地 /api/health 仍 200。重开由队长决定。\n"
            "🔴 重置库会清空 event_log 等真实试用数据（章程资格项），导出前严禁重建。"
        ),
        "sources": [_src("docs/30", "§7.5"), _src("docs/30", "§0.5")],
    },
    {
        "id": "export_data",
        "strong": ["导出数据", "四类数据"],
        "keywords": ["导出", "怎么导出"],
        "answer": (
            "四类数据导出必须在关停前完成，且只留聚合；用 telemetry_report.py 产出。"
            "🔴 导出之后才允许重置 / 重建库 —— 重建会清空真实试用数据。"
        ),
        "sources": [_src("docs/30", "§7"), _src("docs/30", "§6")],
    },
)


def match_intent(question: str) -> dict | None:
    """第 0 层：意图表匹配。命中返回意图条目，否则 None。

    判据（刻意保守，宁可漏到第 1 层，也不误命中）：
      - 命中任一 `strong` 词 → 直接命中（如「调课」「一键消解」「有没有空」）；
      - 否则需命中 **2 个及以上** `keywords` 才命中
        （单个短词（「预览」「取消」）太泛，单独出现不足以确定意图）。
      - 多条命中时按「命中词总长度 + 强词加权」取最高分。
    """
    text = (question or "").strip()
    if not text:
        return None
    best: dict | None = None
    best_score = 0
    for item in INTENTS:
        strong_hits = [kw for kw in item["strong"] if kw in text]
        weak_hits = [kw for kw in item["keywords"] if kw in text]
        if not strong_hits and len(weak_hits) < 2:
            continue
        score = (
            sum(len(kw) for kw in strong_hits) * 2
            + len(strong_hits) * 3
            + sum(len(kw) for kw in weak_hits)
        )
        if score > best_score:
            best_score = score
            best = item
    return best


# ---------------------------------------------------------------------------
# 脱敏：开 LLM 前把可识别信息换成占位符
# ---------------------------------------------------------------------------

#: 常见姓氏（覆盖「张老师」「李老师」这类真实称呼；不认识的一律不替换 ——
#: 把「各位老师」错替成「各位[教师]」会改坏语义，比不替换更糟）。
_SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳唐费廉岑"
    "薛雷贺倪汤滕殷罗毕郝邬安常乐于傅皮卞齐康伍余元卜顾孟平黄和穆萧尹"
    "姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵"
)

_TEACHER_RE = re.compile(rf"[{_SURNAMES}][\u4e00-\u9fff]{{0,2}}老师")
_CLASS_RE = re.compile(r"[\u4e00-\u9fff]{0,3}\s*[（(]?\s*\d{1,2}\s*[)）]?\s*班")
_OBJ_ID_RE = re.compile(r"\b(?:obj|grp|g|sch|usr|org|exc|dem)_[0-9a-zA-Z]{6,}\b")
_HEX_ID_RE = re.compile(r"\b[0-9a-f]{16,32}\b")

#: 占位符（对外可见的只有这些，原始称呼永不出本机）。
PH_TEACHER = "[教师]"
PH_CLASS = "[班级]"
PH_ID = "[对象ID]"


def scrub(text: str) -> str:
    """把可能识别到具体老师 / 班级 / 对象的字样换成占位符。

    🔴 卡面红线：开 LLM 前必须脱敏。做法是**白名单式替换**，不是「猜谁是姓名」：
      - 姓氏 + 「老师」（张老师 / 欧阳老师）→ `[教师]`；
      - 带数字的班级（高一(3)班 / 示例1班 / 2 班）→ `[班级]`；
      - 对象 / 日程 id（obj_xxx / sch_xxx / 16~32 位 hex）→ `[对象ID]`。

    ⛔ 不替换的东西及原因：`trial_t01` 这类**试用编号**是假名短码（手册 §5.5-E 明确
    允许出现在埋点里），替换掉反而会让 LLM 看不懂手册原文。学生姓名**根本不进本层**
    —— 本模块结构上只拿得到「用户问题 + 手册段落」，拿不到课表与名单。
    """
    if not text:
        return ""
    out = _TEACHER_RE.sub(PH_TEACHER, text)
    out = _CLASS_RE.sub(PH_CLASS, out)
    out = _OBJ_ID_RE.sub(PH_ID, out)
    out = _HEX_ID_RE.sub(PH_ID, out)
    return out


# ---------------------------------------------------------------------------
# 第 2 层：LLM 润色（可选）
# ---------------------------------------------------------------------------

#: 与 `b_llm_client.polish` 的跨人契约（AGENTS §4.1，签名冻结）。
_LLM_TIMEOUT_S = 8.0


def llm_enabled() -> bool:
    """是否配置了 LLM。🔴 **只判有无，不读取、不使用其值。**

    key/url 的读取与拼装全部在 `b_llm_client`（跨人契约），本层不碰 ——
    这里只是为了让 `degraded` 说实话：「本来能调 AI 但没调成」与
    「本来就没启用 AI」是两件不同的事，前端要能区分。
    """
    return bool(os.environ.get("SCHEDULER_LLM_BASE", "")) and bool(
        os.environ.get("SCHEDULER_LLM_KEY", "")
    )


def _call_polish(question: str, passages: list[str], polish=None) -> str | None:
    """调 LLM 润色。返回 str 或 None（**永不抛**）。

    🔴 `b_llm_client` 可能还不存在（队长文件，跨人并行）—— `ImportError` 必须
    降级为纯检索，不能因为对方没交付就整个问答接口不可用。
    """
    if polish is not None:  # 测试注入
        fn = polish
    else:
        try:
            from . import b_llm_client as llm
        except ImportError:
            return None
        fn = getattr(llm, "polish", None)
        if fn is None:
            return None
    try:
        result = fn(question, passages, timeout_s=_LLM_TIMEOUT_S)
    except Exception:  # noqa: BLE001 —— 任何异常都降级，绝不把 500 抛给前端
        return None
    if not isinstance(result, str) or not result.strip():
        return None
    return result.strip()


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

def answer(question: str, polish=None) -> dict:
    """问答主入口：三层降级，返回 `{answer, sources, mode, degraded}`。

    参数 `polish` 仅用于测试注入（真实调用走 `b_llm_client.polish`）。

    `mode` 与 `degraded` 的口径见模块 docstring；要点复述：

      - `mode`      ：`retrieval`（手册原文，含意图表命中）/ `llm`（模型润色）/
                      `template`（固定文案：没有这条、手册未就绪、空问题）
      - `degraded`  ：`True` = **本可以调 AI 但没调成**；`False` = 本来就没启用 AI
                      （离线基线，不是故障）或 AI 调用成功。

    ⚠️ 回答里**不含任何「改哪条日程」的指令** —— 本层只解释，变更走现有接口。
    """
    corpus = load_corpus()
    if corpus is None:
        return {"answer": CORPUS_MISSING, "sources": [], "mode": "template",
                "degraded": False}

    text = (question or "").strip()
    if not text:
        return {"answer": EMPTY_QUESTION, "sources": [], "mode": "template",
                "degraded": False}

    intent = match_intent(text)
    if intent is not None:
        return {
            "answer": intent["answer"],
            "sources": [dict(s) for s in intent["sources"]],
            "mode": "retrieval",
            "degraded": False,
        }

    hits = retrieve(text)
    if not hits:
        # 🔴 不许编：没命中就说不知道，不拿邻近段落凑一个答案
        return {"answer": NO_ANSWER, "sources": [], "mode": "template", "degraded": False}

    sources = [{"doc": h["doc"], "section": h["section"]} for h in hits]
    enabled = llm_enabled()
    polished = None
    if enabled:
        # 🔴 脱敏在**本层**做（卡面红线 4）：问题与段落一起过 scrub，再交给 b_llm_client
        polished = _call_polish(scrub(text), [scrub(h["text"]) for h in hits], polish=polish)
    if polished:
        return {"answer": polished, "sources": sources, "mode": "llm", "degraded": False}
    return {
        "answer": _retrieval_answer(hits),
        "sources": sources,
        "mode": "retrieval",
        "degraded": enabled,  # 配了却没调成 → 这才是「降级」
    }
