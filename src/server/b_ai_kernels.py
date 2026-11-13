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
    "手册里没有这条，请找现场老师确认。\n"
    "（这个问答只回答教师手册里写明的内容，手册没写的一律不猜、不编步骤。）"
)

#: 语料缺失时的固定文案（卡面：两文件缺失 → 返回「手册未就绪」，不报 500）。
CORPUS_MISSING = (
    "手册未就绪：问答内容暂时没加载上。请找现场老师确认，或刷新页面再试。"
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
            "分两种情况，先看清你要改的是哪一类：\n"
            "① 只是某一天这节课不上、或换一节上 → 用「临时微调」：双击那个格子"
            "（或点格子右上角的「⋯」），选「取消今天这节课」或「改到第 N 节」。"
            "只动这一天，改错了点「撤销」就回到原样。\n"
            "② 要改整周的固定课表 → 你提交的是一条「提议」，"
            "需要管理员批准之后才会真正生效。看到「要审批」是正常的。"
        ),
        "sources": [_src("docs/07", "§11"), _src("docs/30", "§5.5")],
    },
    {
        "id": "cancel_once",
        "strong": ["取消这节课", "临时取消", "取消一节课", "这节课取消"],
        "keywords": ["取消", "不上了"],
        "answer": (
            "双击有课的那个格子 → 选「取消今天这节课」→ 这一天就空了，其它周不受影响。\n"
            "如果提示「已记录，但这一天课表没有变化」，说明这天本来就没有这门课"
            "（比如赶上假期或单双周），不是出错，也不用重复点。"
        ),
        "sources": [_src("docs/07", "§11")],
    },
    {
        "id": "makeup",
        "strong": ["调休", "补课"],
        "keywords": ["补上", "顺延", "换一天上"],
        "answer": (
            "调休、补课按「那一天」单独处理，不要动整周课表：\n"
            "双击那天的格子 → 取消原来那节，或者改到当天实际有课的节次。\n"
            "系统按那天的真实作息算，其它周不受影响。"
        ),
        "sources": [_src("docs/07", "§11")],
    },
    {
        "id": "resolve_how",
        "strong": ["一键消解", "怎么消解", "点了没变化", "点了没反应",
                   "消解没反应", "没消掉", "点了没变"],
        "keywords": ["预览", "应用方案", "点应用", "没变化"],
        "answer": (
            "两步，别只走第一步：\n"
            "① 点「一键消解」先看方案 —— 这一步只算不改，课表不会动；\n"
            "② 确认方案没问题，再点「应用」，这时才真正改课表。\n"
            "点了没反应，多半是只看了预览没点应用。应用后冲突数应该下降。"
        ),
        "sources": [_src("docs/30", "§5.5"), _src("docs/07", "§6")],
    },
    {
        "id": "resolve_need_approval",
        "strong": ["要审批", "需要审批", "无权限", "没有权限"],
        "keywords": ["不能直接改", "凭什么不能改"],
        "answer": (
            "这是设计，不是故障：普通教师可以提议，但不能直接改整周的固定课表。\n"
            "你的提议会出现在管理员那边，他点「批准」之后才生效。\n"
            "急着用的话，先用「临时微调」把那一天调开，不必等审批。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "resolve_remaining",
        "strong": ["冲突没降", "消不掉", "没消完", "消解后还有"],
        "keywords": ["还剩", "没降", "还是冲突"],
        "answer": (
            "有些冲突是消不掉的：同一位老师同一时刻被排了两个班、"
            "或同一间教室被占两次 —— 这类没有解。\n"
            "系统会把能改的都改掉，剩下的原样留着，并告诉你还有多少条没能处理。\n"
            "剩下的需要人工决定谁让路。"
        ),
        "sources": [_src("docs/30", "§5.5"), _src("docs/07", "§6")],
    },
    {
        "id": "conflicts_zero",
        "strong": ["看不到冲突", "没有冲突", "冲突为0", "零冲突"],
        "keywords": ["冲突是0", "冲突列表是空"],
        "answer": (
            "按顺序看三种可能：\n"
            "① 组选错了 —— 左侧「当前组」切换到你要看的那一个；\n"
            "② 这一周确实没有冲突；\n"
            "③ 页面上的日期落在假期或学期外 —— 换到有课的那一周再看。"
        ),
        "sources": [_src("docs/30", "§5.5"), _src("docs/30", "§1")],
    },
    {
        "id": "empty_groups",
        "strong": ["看不到班级", "组是空的", "下拉框是空的", "没有组可选"],
        "keywords": ["组的列表", "看不到组"],
        "answer": (
            "这个账号还没有被授权查看任何班级，所以下拉框是空的。\n"
            "这一步需要管理员开通，你这边刷新页面即可，不用改任何设置。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "login_fail",
        "strong": ["登录失败", "登不进去", "登录不了", "登录不上", "口令错", "密码错"],
        "keywords": ["登不上", "登录报错"],
        "answer": (
            "按这三步排：\n"
            "① 登录名和口令都区分大小写，复制时别带首尾空格，尽量粘贴不要手打；\n"
            "② 还是进不去，就找现场老师换一张登录卡；\n"
            "③ 连续输错会短暂锁定，等一分钟再试，别一直点。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "login_lock",
        "strong": ["连续失败", "锁住了", "过于频繁", "登录限流", "一分钟后再试"],
        "keywords": ["被锁", "锁多久", "限流"],
        "answer": (
            "短时间内连续输错会被暂时锁住，等一分钟再试就行。\n"
            "越反复点，锁得越久。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "page_not_open",
        "strong": ["页面打不开", "网页打不开", "一直转圈", "连不上服务器"],
        "keywords": ["打不开", "转圈", "别人连不上"],
        "answer": (
            "先确认两件事：\n"
            "① 地址是否完整（要带端口号），是不是复制时少了结尾；\n"
            "② 换手机流量再打开一次，排除校园网限制。\n"
            "都不行就找现场老师，可能是服务没启动或网络没放行。"
        ),
        "sources": [_src("docs/30", "§0.6")],
    },
    {
        "id": "cache_stale",
        "strong": ["缓存", "强刷", "样式错乱", "按钮点不动"],
        "keywords": ["还是旧版", "页面没更新"],
        "answer": (
            "页面还是旧样子、按钮点了没反应，先按 Ctrl + F5 强制刷新。\n"
            "还不行就清一下浏览器缓存再打开。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "narrow_screen",
        "strong": ["窄屏", "横向滚动", "手机上打开"],
        "keywords": ["手机", "屏幕小"],
        "answer": (
            "这个页面是给电脑浏览器用的，屏幕宽度建议 1024 以上。\n"
            "手机上打开会挤在一起，请换电脑。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "privacy",
        "strong": ["记录我点什么", "我的姓名", "传出去", "删掉我的数据"],
        "keywords": ["隐私", "匿不匿名", "你们能看到"],
        "answer": (
            "不记录姓名和班级：只统计「点了哪个功能」这类动作，不带任何人的名字。\n"
            "试用用的是虚构的「示例学校」数据，没有真实学生名单。\n"
            "试用结束后整库删除。"
        ),
        "sources": [_src("docs/30", "§5.5"), _src("docs/30", "§0")],
    },
    {
        "id": "start_server",
        "strong": ["起服务", "启动服务", "三个参数", "怎么启动"],
        "keywords": ["端口", "run_server", "host"],
        "answer": (
            "（现场支持用）"
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
            "登录名是 trial_ 开头的编号（比如 5 号老师是 trial_t05），"
            "口令写在各自的登录卡上。\n"
            "一人一份，当面给或单独发，不要发到群里。"
        ),
        "sources": [_src("docs/30", "§0.5"), _src("docs/30", "§3")],
    },
    {
        "id": "propose_approve",
        "strong": ["谁能批准", "审批流程", "谁来批"],
        "keywords": ["提议", "审批", "批准"],
        "answer": (
            "永久性改动要管理员批准：你先「提议」，管理员那边点「批准」，课表才真的变。\n"
            "看到「要审批 / 无权限」是正常流程，不是系统坏了。"
        ),
        "sources": [_src("docs/30", "§5.5")],
    },
    {
        "id": "org_isolate",
        "strong": ["一师一组织", "看到别人的", "互相影响"],
        "keywords": ["各看各的", "数据会不会混", "会不会看到其他"],
        "answer": (
            "每位老师只能看到自己的班级，互相看不到，也不会互相影响。\n"
            "你消解自己的冲突，不会动到别人的课表。"
        ),
        "sources": [_src("docs/30", "§0.5")],
    },
    {
        "id": "conflict_why",
        "strong": ["为什么有冲突", "什么叫冲突", "冲突是什么"],
        "keywords": ["硬约束", "软约束", "冲突怎么来"],
        "answer": (
            "冲突就是同一时间被排了两件事：同一个班两门课、同一位老师两个班、"
            "或者同一间教室被占两次。\n"
            "系统把课表和活动放在同一张时间表上比对，撞上的格子标红。"
        ),
        "sources": [_src("docs/07", "§6")],
    },
    {
        "id": "slot_query",
        "strong": ["有没有空", "空不空", "空位"],
        "keywords": ["哪节空", "有空吗"],
        "answer": (
            "左侧「查空位」：选对象、选时间范围、填要连排几节，点查询就会列出能排的时间。\n"
            "看中某一项，点「按此提议」提交 —— 仍然要管理员批准，课表不会立刻变。"
        ),
        "sources": [_src("docs/07", "§8")],
    },
    {
        "id": "demand_input",
        "strong": ["提需求", "用一句话", "自然语言"],
        "keywords": ["原话", "需求怎么提"],
        "answer": (
            "当前页面还没有「用一句话提需求」的输入框，请不要找它。\n"
            "同样的事用两个入口完成：\n"
            "① 只动某一天 → 双击格子，用「临时微调」；\n"
            "② 要改整周 → 提交「提议」，等管理员批准。"
        ),
        "sources": [_src("docs/07", "§7")],
    },
    {
        "id": "telemetry",
        "strong": ["埋点", "怎么统计", "采集数据"],
        "keywords": ["统计数据", "事件记录"],
        "answer": (
            "（现场支持用）"
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
            "试用结束会请老师填一张 6 题的纸质问卷，当场填、当场收。\n"
            "只问使用感受，不涉及学生信息。"
        ),
        "sources": [_src("docs/30", "§4")],
    },
    {
        "id": "shutdown",
        "strong": ["关停", "结束试用", "下线实例"],
        "keywords": ["停掉服务", "关掉公网"],
        "answer": (
            "（现场支持用）"
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
            "（现场支持用）"
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
_TITLE_RE = re.compile(rf"[{_SURNAMES}][\u4e00-\u9fff]{{0,2}}(?:主任|校长|同学)")
_CLASS_RE = re.compile(r"[\u4e00-\u9fff]{0,3}\s*[（(]?\s*\d{1,2}\s*[)）]?\s*班")
#: 中文数字班名（高一一班 / 高二(3)班 / 示例一班）—— 2026-11-13 补：
#: 原实现只认阿拉伯数字，`高一一班` 会原样漏给模型（外部审查发现）。
_CN_CLASS_RE = re.compile(
    r"(?:初|高|小)[一二三四五六七八九十]{1,2}\s*[（(]?\s*[一二三四五六七八九十\d]{0,2}\s*[)）]?\s*班"
    r"|[\u4e00-\u9fff]{0,2}[一二三四五六七八九十]{1,2}班"
)
_OBJ_ID_RE = re.compile(r"\b(?:obj|grp|g|sch|usr|org|exc|dem)_[0-9a-zA-Z]{6,}\b")
_HEX_ID_RE = re.compile(r"\b[0-9a-f]{16,32}\b")

#: 占位符（对外可见的只有这些，原始称呼永不出本机）。
PH_TEACHER = "[教师]"
PH_CLASS = "[班级]"
PH_ID = "[对象ID]"
PH_PERSON = "[人员]"
PH_PLACE = "[场所]"

#: 对象 kind → 占位符（供 `scrub(vocab=...)` 使用）。
VOCAB_PLACEHOLDER = {"user": PH_PERSON, "group": PH_CLASS, "resource": PH_PLACE}


def _apply_vocab(text: str, vocab: dict[str, str] | None) -> str:
    """按「已知对象名」精确替换（最长优先）。

    🔴 2026-11-13 补：正则只能猜「姓氏+老师」「数字班名」这类**格式**，
    裸姓名（如「王思勋负责的…」）和中文数字班名（「高一一班」）会漏。
    真实名字本来就在库里，由调用方（`api_chat`）取出来传进来即可精确命中，
    既不误伤普通词，也不依赖猜。
    """
    if not vocab:
        return text
    # 最长优先：避免「高一(1)班」被「一班」先切一刀
    for name in sorted(vocab, key=len, reverse=True):
        if len(name) >= 2 and name in text:
            text = text.replace(name, vocab[name])
    return text


def scrub(text: str, vocab: dict[str, str] | None = None) -> str:
    """把可能识别到具体老师 / 班级 / 对象的字样换成占位符。

    🔴 卡面红线：开 LLM 前必须脱敏。两层做法：
      ① **精确层**：调用方传入 `vocab`（库里真实的对象名 → 占位符），最长优先替换。
         这一层才靠得住 —— 名单本来就在库里，不需要猜；
      ② **格式层**（无 vocab 时的兜底）：姓氏 + 「老师/主任/校长/同学」、
         阿拉伯数字或中文数字班名、对象 / 日程 id（obj_xxx / 16~32 位 hex）。

    ⛔ 不替换的东西及原因：`trial_t01` 这类**试用编号**是假名短码（手册 §5.5-E 明确
    允许出现在埋点里），替换掉反而会让 LLM 看不懂手册原文。
    """
    if not text:
        return ""
    out = _apply_vocab(text, vocab)
    out = _TEACHER_RE.sub(PH_TEACHER, out)
    out = _TITLE_RE.sub(PH_TEACHER, out)
    out = _CLASS_RE.sub(PH_CLASS, out)
    out = _CN_CLASS_RE.sub(PH_CLASS, out)
    out = _OBJ_ID_RE.sub(PH_ID, out)
    out = _HEX_ID_RE.sub(PH_ID, out)
    return out


# ---------------------------------------------------------------------------
# 第 2 层：LLM 润色（可选）
# ---------------------------------------------------------------------------

#: 与 `b_llm_client.polish` 的跨人契约（AGENTS §4.1，签名冻结）。
_LLM_TIMEOUT_S = 8.0

#: 意图表命中时的润色超时：只是把速查答案说顺，不值得让老师等 8 秒。
_LLM_INTENT_TIMEOUT_S = 5.0


def llm_enabled() -> bool:
    """是否配置了 LLM。🔴 **只判有无，不读取、不使用其值。**

    key/url 的读取与拼装全部在 `b_llm_client`（跨人契约），本层不碰 ——
    这里只是为了让 `degraded` 说实话：「本来能调 AI 但没调成」与
    「本来就没启用 AI」是两件不同的事，前端要能区分。
    """
    return bool(os.environ.get("SCHEDULER_LLM_BASE", "")) and bool(
        os.environ.get("SCHEDULER_LLM_KEY", "")
    )


def _call_polish(question: str, passages: list[str], polish=None,
                 timeout_s: float = _LLM_TIMEOUT_S) -> str | None:
    """调 LLM 润色。返回 str 或 None（**永不抛**）。

    🔴 `b_llm_client` 可能还不存在（队长文件，跨人并行）—— `ImportError` 必须
    降级为纯检索，不能因为对方没交付就整个问答接口不可用。
    🔴 `timeout_s` 可按调用方要求收紧：意图表命中时只是「把速查答案说顺一点」，
    不值得让老师等 8 秒，调用方传 5 秒。
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
        result = fn(question, passages, timeout_s=timeout_s)
    except Exception:  # noqa: BLE001 —— 任何异常都降级，绝不把 500 抛给前端
        return None
    if not isinstance(result, str) or not result.strip():
        return None
    return result.strip()


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

def answer(question: str, polish=None, vocab: dict[str, str] | None = None) -> dict:
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
        # 🔴 2026-11-13 修：原先意图命中**直接返回、根本不调模型** —— 结果是
        #    老师问到的常见问题永远标「手册原文（未启用 AI）」，看起来像 AI 坏了。
        #    现在命中后也交给模型说顺一遍（资料 = 意图答案本身，事实不外扩），
        #    模型不可用/超时就原样返回速查文案，只是 mode 如实写 retrieval。
        base = intent["answer"]
        sources = [dict(s) for s in intent["sources"]]
        if llm_enabled():
            polished = _call_polish(
                scrub(text, vocab), [scrub(base, vocab)], polish=polish,
                timeout_s=_LLM_INTENT_TIMEOUT_S,
            )
            if polished:
                return {"answer": polished, "sources": sources,
                        "mode": "llm", "degraded": False}
            # 配了却没调成：degraded 如实为 True，让前端能说「AI 这次没响应」
            return {"answer": base, "sources": sources,
                    "mode": "retrieval", "degraded": True}
        return {
            "answer": base,
            "sources": sources,
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
        # 🔴 脱敏在**本层**做（卡面红线 4）：问题与段落一起过 scrub，再交给 b_llm_client。
        #    `vocab` 由 api_chat 从库里取真实对象名传入 —— 正则猜不准裸姓名与中文数字班名，
        #    名单本来就在库里，精确替换才不会漏（2026-11-13 外部审查补）。
        polished = _call_polish(
            scrub(text, vocab),
            [scrub(h["text"], vocab) for h in hits],
            polish=polish,
        )
    if polished:
        return {"answer": polished, "sources": sources, "mode": "llm", "degraded": False}
    return {
        "answer": _retrieval_answer(hits),
        "sources": sources,
        "mode": "retrieval",
        "degraded": enabled,  # 配了却没调成 → 这才是「降级」
    }
