# API 契约（**v1.0 已冻结**）

> **用途**：冻结 6 个接口的 URL / 方法 / 请求体 / 响应体 / 错误码。
> **负责人**：张晋嘉（独此一嘉 · **后端**，2026-09-30 分工对调后归后端）。
> ✅ **状态：已冻结 v1.0**（2026-10-01 晚，队长逐项拍板 8 处待填，全文件 `待填` 归零）。
> 前端可照本契约用 mock JSON 开工，不必等后端。
> 🔴 **改契约＝改两边**：任何字段变更必须更新本文件并在群里说明，**不允许口头改**。
> 🔴 **留灵活性是硬要求**（队长 2026-10-01）：见文末 **§12 灵活性与向前兼容**，
> 前端**必须**按该节实现"遇到未知值不崩"，否则后续任何扩展都会变成返工。
> **为什么必须先冻结它**：前端（T4）要用 **mock JSON** 并行开发，不必等后端 T3 完成；
> 契约不冻结，三人并行就是空谈。

## 填写规则

1. 每个接口写清：**路径、方法、请求体字段（含类型）、响应体字段（含类型）、可能的错误码**。
2. 字段一旦定稿，**变更必须更新本文件并在群里说明**，不允许口头改。
3. 拿不准的字段**不许瞎填**（前端会照着写死），必须在字段旁写明疑问并找队长拍板。

> ⚠️ 本文件**刻意不出现** `TBD` /「待填」这类字面量（包括规则说明里也不写），
> 否则自动化检查会把规则说明误判成未填项。历史用法见 §变更记录。

## 通用约定

- 认证：`Authorization: Bearer <token>`；未登录统一 **401**（响应体**不带** `WWW-Authenticate` 头）。
- 当前组：`?group=<group_id>` 或 `X-Group` 请求头；缺省走隐式模式（对所有祖先组取最高 level）。
- 统一错误体：`{"error":{"code":"...","message":"..."}}`
- 拒绝语义：**越权访问具体资源 → 404**（不泄露资源是否存在）｜**档位不足 → 403**｜**参数不合法 → 400**
- 日期用 ISO（`2026-09-23`），槽位掩码用十六进制字符串，周次奇偶用整数 0/1/2。

---

## 1. POST /api/login

| 项 | 内容 |
|---|---|
| 请求 | `{"login": "admin", "password": "admin123"}` |
| 响应 200 | `{"token": "...", "user": {"id","login","global_role"}, "default_group_id": "..."}` |
| 响应 401 | `{"error":{"code":"bad_credentials","message":"登录名或密码错误"}}` |
| 响应 429 | **（2026-10-25 升级）**双层限频：① `(login, 客户端IP)` 组合锁——同组合连续失败 5 次 → 只锁该组合 60 秒，`{"error":{"code":"locked",...}}`；② 单 IP 60 秒内第 31 次起登录请求 → `{"error":{"code":"rate_limited",...}}` + `Retry-After: 60` 响应头。别人（别的 IP）登录同一账号不受影响。IP 仅内存限流，不落库、不进埋点/日志/响应体 |

🔴 **响应中绝不回传 `password_hash`。**

✅ **已定（队长 2026-10-01）**：`default_group_id` ＝ **该用户所在的最高组**。

**精确定义（按此实现）**：

1. 取该用户**有授权**（`level ≥ view`）的**全部组**；
2. 在其中取**沿对象图祖先链层级最高**的那个（最靠近根，即到根 `depth` 最小）；
   → 管理员 → 根组「全校」；年级组长 → 该年级；任课教师 → 其所属最高层级组。
3. **并列时**（多个组层级相同）依次比较：① 权限档位 `level` 高的优先；② `group_id` 字典序小的优先。
   → 目的：**保证结果确定**，避免每次登录显示的组不一样。

⚠️ 若队长本意是「权限最高」而非「层级最高」，请纠正——这两者在跨年级任课教师身上会不同。

---

## 2. GET /api/groups

| 项 | 内容 |
|---|---|
| 请求 | 无参 |
| 响应 200 | `[{"group_id","name","level","member_count","parent_name"}]` |
| 说明 | **只返回我有授权（含继承）的组**；未授权的组**根本不列出**（不是灰掉） |

✅ **已定（队长 2026-10-01）：要**。`member_count` 本期实现，组列表显示人数。
（因此 §7.4 泄压阀第 2 顺位「砍 member_count」**不再生效**，改由 §7.4 后续顺位顶上。）

---

## 3. GET /api/objects?group=

| 项 | 内容 |
|---|---|
| 请求 | `group`（可选） |
| 响应 200 | `[{"id","kind","name","parent_ids":[...]}]` |
| 说明 | 只返回**当前组闭包内**的对象 |

✅ **已定（队长 2026-10-03 修正）**：`kind ∈ {group, user, resource}` —— **只有三个值**。
（依据：`schema.sql` 中 `objects.kind` 的 CHECK 约束与注释均为三值，`organization` 是**独立表** `organizations`，不进 `objects`。）

> 🔴 **勘误说明（2026-10-03）**：本节原写「四值（含 organization）」并标注「按原本定义」，**该结论有误** ——
> 它把"`schema.sql` 一致"当成了依据，但实际 `schema.sql:36` 注释与 `schema.sql:45` CHECK 都只允许三值。
> 若后端按四值实现并写入 `kind="organization"`，会被 CHECK 约束直接拒绝（`IntegrityError`）。
> **正确做法：组织本身不进 `objects`，其对应的"根组"以 `kind="group"` 出现在 `/api/groups` 结果中。**

⚠️ 将来若新增取值 → 属**非破坏性变更**（须同步改 `schema.sql` CHECK），前端按 §12 用默认样式降级渲染。

---

## 4. GET /api/schedules?group=&week=

| 项 | 内容 |
|---|---|
| 请求 | `group`（可选）、`week`（ISO 日期，缺省为本周） |
| 响应 200 | `{"objects":[...], "days":["2026-09-21",...], "grid": {"<object_id>|<date>": "<mask_hex>"}}` |
| 说明 | **前端只渲染，不做位运算**；mask 必须与 `slots.decode_mask` 一致 |

✅ **已定（队长 2026-10-01）**：

- **grid key 格式＝竖线分隔**：`"<object_id>|<date>"`，例 `"obj_3b7|2026-10-05"`。
  🔴 前端按 `|` 拆分；**后端不得改用其他分隔符**（改了页面全空）。
  ⚠️ `object_id` 本身**不含** `|`（建对象时约束），保证拆分无歧义。
- **`week` 缺省＝本周**，以**周一为一周起点**（ISO）；`week` 传该周内任一日期即可，服务端自行归到周一。

---

### 4.1 GET /api/schedules/detail?group=&object_id=&date=（2026-11-13 新增，卡 rYoMa7）

> 只读接口。**不改 §4 的任何字段与行为**——§4 已冻结，契约测试裸调它。

| 项 | 内容 |
|---|---|
| 请求 | `group`（可选，同 §4 口径）、`object_id`（必填）、`date`（必填，ISO `YYYY-MM-DD`） |
| 响应 200 | `{"object_id":"obj_x","date":"2026-09-09","day_of_week":3,"periods":[...],"cells":[...]}` |
| 401 | 未登录（`@require_auth`，嵌套错误体） |
| 403 | `group` 越权（`resolve_authorized_org`，与 §4 同源） |
| 404 | `object_id` 不存在 **或** 不属于该组织 |
| 400 | `object_id` / `date` 缺失，`date` 格式非法（含 `20260909` 无连字符、`2026-02-30` 不存在） |

**为什么要有它**：§4 的 `grid` 值是**按位或后**的掩码，拿不到 `schedule_id`。
前端双击一格要挂「取消 / 改点」例外时，不知道该取消哪条日程；「改到第 N 节」
也拿不到**当天实际生效**的节次清单（换作息那天节次会变）。

响应体：

```json
{"object_id": "obj_772ed3e4d9bd4e05",
 "date": "2026-09-09",
 "day_of_week": 3,
 "periods": [{"idx": 1, "name": "第1节", "start": "08:00", "end": "08:45", "enabled": true}],
 "cells": [{"idx": 1, "mask": "0x000002",
            "schedules": [{"id": "sch_a761a1b784be4d23", "title": "（冲突演示）高一(1)班 物理",
                           "kind": "periodic", "role": "target"},
                          {"id": "sch_ce36b60ceea44a97", "title": "全年级大会",
                           "kind": "once", "role": "participant"}]}]}
```

> **`periods[].new_mask`（2026-11-13 追加，纯新增字段）**：这一节作为 `move` 目标时
> 应传的单日掩码，格式与 `cells[].mask` 完全一致（`0x%06x`，第 1 节 = `0x000002`）。
> 前端「改到第 N 节」直接取它，⛔ 不要自己 `1 << N` 算 —— 空白节次在 `cells` 里
> 没有对应值，这个字段就是为了让「mask 由服务端给」这条约束在改点场景也成立。

**五条口径（写死，不得自改）**：

| # | 口径 | 理由 |
|---|---|---|
| 1 | `cells[].schedules` 恒为**数组** | 一格被多条日程同时占用**就是冲突本身**。给单对象等于替前端静默挑一条，取消掉一条后另一条还在撞，老师以为调好了 |
| 2 | 每条 schedule 必带 `kind` | §11 的 `POST /api/exceptions` 对 `kind != 'periodic'` 直接 400。once 活动（如「全年级大会」）挂双击必炸，前端靠这个字段决定能不能点 |
| 3 | `object_id` 不属于该组织 → **404** | 返回 200 + 空 `cells` 等于告诉前端「这天没课」，是静默失效。404 也不泄漏别组织是否有该 id |
| 4 | `mask` = **单日掩码**（1-based，第 N 节 → 位 N，`0x000002` = 第 1 节） | 与 §4 的 `grid`、§11 的 `new_mask` 同一口径。`OccupancyEntry.mask` 是**周位序**（周三第 1 节 = `0x100000000`），直接透出会与 grid 对不上、页面整体错位且不报错 |
| 5 | `cells` **只给有占用的节次**；空白节次不出现在 `cells` 里，但仍出现在 `periods` 里 | 「有这一节、只是空着」与「这天没这一节」是两件事，不能混 |

**数据来源（同源，不另写一套展开）**：

| 字段 | 来源 |
|---|---|
| `cells` | `occupancy.build_occupancy(conn, org_id, date, date)` → 按 `object_id + date` 过滤 → `decode_mask` 拆位聚合 |
| `periods` | `load_exceptions` → `effective_slot_config(exc, cfg, iso)` → `enabled_slots(dow)` |
| `day_of_week` | **生效**周几：调休日取 `as_of_dow`，否则取 `date.isoweekday()` |
| 403 | `b3_kernels.resolve_authorized_org`，与 §4 完全同源 |

🔴 两条容易走偏的地方：

1. **`periods` 不许直接查 `period_slots` 或 `set_slots`**。真相链是
   `period_slots`（默认作息）→ `period_sets/set_slots`（special 日换作息）
   → `effective_slot_config` 决定当天用哪套。硬编码 1..10 或查单表，
   会在换作息那天给出**不存在的节次**（排不进去、接口 200、零报错）。
   只给 `enabled = 1` 的节次，与 `SlotConfig.day_mask()` 同一份过滤，
   所以 `periods` 的 idx 集合恒等于「占用掩码可能命中的位」。
2. **`day_of_week` 与 `periods` 必须同源**。调休日（周六按周一上课）若用
   `isoweekday()` 取节次，就会拿周六的槽位去配周一的占用 → periods 与 cells
   对不上、页面错位且不报错。`expand_periodic` 用的是 `as_of_dow`，本接口照抄。

**只读保证**：不写任何表、不新增 POST。`cells` 排序为 `idx` 升序、
`schedules` 按 `(id, role)` 全序 ⇒ 同一请求两次调用逐字一致
（日程 id 是 uuid，不定序会让同格两条日程顺序随机）。

---

### 4.2 POST /api/chat 的 `rule` 字段 + POST /api/resolve/explain（2026-11-15 纯追加，卡 rHrY9D）

> 🔴 本节是 **v1.1 追加**，只新增字段与新路由，**不改动上面任何一条既有约定**。
> `mode` 取值仍是 `llm` / `retrieval` / `template` 三态，没有新增枚举。

**（1）POST /api/chat 响应新增两个字段（只增不改）**

```json
{"answer":"...","sources":[{"doc":"docs/30","section":"§3"}],
 "mode":"llm","degraded":false,
 "grounded":true,
 "rule":{"intent_id":"conflict_why","slots":{"object":"高一(1)班","query":"conflicts"},
         "source":"model"}}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `grounded` | bool | 恒 `true`。保留字段，只为兼容前端已有判断；含义是「本次答案的事实由后端查得，不是模型自由生成」 |
| `rule.intent_id` | string \| null | 命中的意图 id（取意图表 29 条之一）。答不上来时可为 `null` |
| `rule.slots` | object | 解析出的槽位，最多 4 个键：`object` / `date` / `period` / `query`（`query` ∈ `conflicts`/`slots`/`schedules`/`howto`，全部只读） |
| `rule.source` | `model` \| `keyword` | 排查钥匙：`model` = 模型解析并通过三道校验；`keyword` = 关键词/检索兜底 |

链路（两道门槛，任一不过即回退关键词，⛔ 不返 5xx）：

1. **schema**：`intent_id` 在白名单；`slots` 只有 4 个键；`query` 在只读枚举内；类型正确。
2. **取值**：`object` 必须对得上库内真实对象名；`date` 落在本周扫描窗口；`period` 在当天启用槽位内。
3. **红线**：不得出现 `sch_`/`obj_` 等内部 ID；`reply` 不得含执行动作词；`slots` 不得带 `action` 等动作键。

六条 `no_polish` 意图（`resolve_how` / `login_fail` / `propose_approve` /
`how_to_use` / `what_is` / `count_meaning`）命中即返回写死文本，**不进模型**。

**（2）POST /api/resolve/explain?snapshot_id=snap_x（新增只读路由）**

| 项 | 内容 |
|---|---|
| 鉴权 | `Bearer`，未登录 401；`group` 越权 403；快照不属于本组织 → 404 |
| 请求 | query `snapshot_id`（必填，缺失 400）；body 可空 |
| 响应 200 | `{"snapshot_id":"snap_x","explanation":"...","source":"llm"\|"template","degraded":false}` |
| 只读 | 不写任何表；事实 = 快照（消解前）与当前库逐字段 diff |
| 失败 | 模型未配置 / 超时 / 数字不接地 → `source="template"` + `degraded=true`，**仍 200** |

`explanation` 上限 120 字（提示词约束 + 服务端硬截断）。模型输出里的每个阿拉伯数字
必须出现在输入事实中，否则整条丢弃改用模板句 —— 防「23 处」被写成「12 处」。

调用约定：前端**先渲染** `POST /api/resolve` 的结果（数字与条目），再异步请求本接口补一句解释；
3 秒拿不到就继续显示模板句。⛔ 不允许同步等待模型把结果区卡住。

---

## 5. GET /api/conflicts?group=

| 项 | 内容 |
|---|---|
| 请求 | `group`（可选） |
| 响应 200 | `{"count": 23, "items":[{"a":{...},"b":{...},"date":"2026-09-23","slots":"0x0006","readable":[{"day":3,"period":1}]}]}` |
| 说明 | 🔴 **必须服务端聚合**：按「哪两条日程 × 哪天 × 哪几节」聚合，否则同班 40 名学生会把一处冲突刷成 40 条 |

✅ **已定（队长 2026-10-01）：数据给全**。`a` / `b` 结构相同，字段一律齐全：

```json
{
  "schedule_id": "sch_x",
  "object_id":   "obj_3b7",
  "object_name": "高一1班",
  "object_kind": "group",
  "title":       "数学",
  "date":        "2026-10-07",
  "slots":       "0x0006",
  "readable":    [{"dow":3,"period":1},{"dow":3,"period":2}]
}
```

**为什么要给全**（少一个就会返工）：
- 缺 `object_name`/`title` → 前端显示不出「数学」两个字；
- 缺 `schedule_id`/`object_id` → 点了「消解」没法告诉后端改哪一条；
- 缺 `readable` → 前端要自己解掩码（**禁止**，AGENTS §8：前端不做位运算）。

⚠️ `slots` 是十六进制**不定长**字符串（AGENTS §9b），前端**只透传展示**，禁止做字符串比较。

### 5.1 `activity_window` —— 一次性活动窗口哨兵（2026-10-27 新增，§12.3 非破坏性加字段）

响应体在 `window` 之外再给一个对象：

```json
"activity_window": {
  "has_once": true,
  "count": 1,
  "earliest": "2026-09-09",
  "latest":   "2026-09-09",
  "gap_days": 47,
  "advisory": "一次性活动 2026-09-09 ~ 2026-09-09 全部早于扫描窗口 …（相差 47 天）…"
}
```

| 字段 | 语义 |
|---|---|
| `has_once` | 本组织是否存在一次性活动（活动都由 `schedule_once` 承载） |
| `count` / `earliest` / `latest` | 一次性活动的**条数**与**日期区间**（取 `start_at` 的日期部分） |
| `gap_days` | 活动区间与扫描窗口的**间距**：`0` = 有重叠（扫得到）；`>0` = 完全不重叠，单位天 |
| `advisory` | `None` = 正常；**字符串 = 需要人看一眼**（含实际日期、相差天数、两条修法） |

**解决的问题**：活动日期被钉在过去的某一周，而窗口跟着 `today` 走 → 冲突数骤降、
**接口 200 且零报错**。实测（2026-10-26 同一份试用库）：`--today 2026-10-14` → `raw_count=30`；
`--today 2026-11-11` → `raw_count=3`。演示库同理：裸起在 2026-11-16 只有 1 处（带 `--demo` 是 23 处）。

**判据（写死，勿改）**：`gap_days > 7` 才给 `advisory`。7 天的由来：窗口 = `today` 所在周的
周一~周日，窗口内日期与 `today` 的距离必 ≤ 6 天，7 天是宽松上界；超过它活动必然扫不到。
`0 < gap_days ≤ 7` 属窗口边缘的正常抖动，不告警（避免噪音把真告警淹掉）。

**消费方**：`deploy/trial_ready.py` ③ 已把它当硬判据（`advisory` 非空即判失败）；
前端目前未消费该字段（`web/app.js` 不渲染 `warnings`，同理也不读此字段）——属**已知未做**，
不在本契约的必做范围内。

---

## 6. GET|POST /api/resolve?group=&mode=

| 项 | 内容 |
|---|---|
| GET（预览） | 返回 **top-3 方案**；经可动集合过滤后为空 → 返回**空数组 + "无可行方案"**，**绝不 500** |
| POST（应用） | 执行 `resolve_and_apply()`（迭代：找冲突→应用最优→重扫） |
| 请求体（POST） | `{"plan_index": 0}` |
| 响应 200 | `{"applied":N,"remaining_before":23,"remaining_after":0,"rounds":3,"filtered_out_count":2}` |
| 限频 | 每 token 每分钟 10 次，超限 **429** |
| `mode` | `minimal`（**默认，行为与改动前完全一致**）/ `balanced` / `optimal` |

### 6.0 `filtered_out_count` 语义（🔴 队长 2026-10-03 拍板，此前只有示例值、缺定义）

> 起因：后端做 B4 时发现契约只给了 `"filtered_out_count":2` 这个示例数字，
> 没说它数的是什么。**缺定义比定义错更糟** —— 前端会照着写死，后端会各自猜。

**语义：一键消解（POST）过程中，因「不在可动范围内」而**从未被尝试求解**的冲突处数。**

口径（三条都要满足才算 1）：

1. 该冲突在扫描窗口内**确实存在**（`find_conflicts` 能扫出来）
2. 它涉及的对象里**至少有一个**不在本次请求的可动集合内 → **求解器根本没碰它**
3. 它**未计入** `remaining_after` 的「已尝试但无解」部分

| 字段 | 关系 |
|---|---|
| `remaining_before` | 消解前 `find_conflicts` 的总数 |
| `applied` | 实际被应用并成功改动的方案数 |
| `rounds` | 迭代轮数（`resolve_and_apply` 的 `max_rounds` 上限内） |
| `remaining_after` | 消解后残留冲突数 |
| **`filtered_out_count`** | **其中因不可动而压根没被尝试的 = `remaining_after` - 「尝试过但无解」** |

**为什么必须有这个字段（不是为了凑数）**：
`termination` 只能告诉你「没全消完」（`best_effort`），**不能告诉你为什么**。
现场答辩时如果界面上只显示「还剩 3 处冲突」，评委问「为什么消不掉」，
答不出来就直接暴露「系统在静默失败」——**这与 P0-1 是同一类病**。
前端应把它显示成「其中 N 处因不在你的可改范围，未尝试」。

**B4 实现口径（可直接落地，不许自行改定义）**：

```python
# resolve() 每处冲突都 append（实测确认），不管有没有方案 → 口径可从返回值直接数
results = resolve(conn, org_id, d0, d1, cfg=cfg, max_plans=1)
filtered_out_count = sum(1 for r in results if not r.plans)   # 无方案 = 被过滤或无解
```

⚠️ **B4 的已知简化**：`context=None`（未登录）时**可动集合 = 全可动**，
此时 `filtered_out_count` 恒为 0。B2 上线后接入真实权限过滤，该字段才有非零值。
**这个字段必须先占位返回 0，不许省略字段** —— 省略会让前端写死成"永远为 0"。

### 6.1 GET 预览的响应体（🔴 队长 2026-09-25 批准「多方案选择」进 MVP）

```json
{
  "plans": [
    {
      "index": 0,
      "cost": 3.5,
      "diff_text": "改数学课（影响 2 个班）",
      "impact": {"schedule_count": 1, "object_count": 2},
      "moves": [
        {"schedule_id":"sch_x","title":"高二1班 数学",
         "from":{"date":"2026-09-23","slots":"0x0002"},
         "to":  {"date":"2026-09-23","slots":"0x0008"}}
      ],
      "warnings": [
        {"type":"capacity","message":"实验室A 容量 40，容纳不下 48 人"}
      ]
    }
  ],
  "termination": "ok"      // ok | no_solution | best_effort（见 6.3）
}
```

**三条硬要求**（少一条前端就没法用）：

1. **`diff_text` 必填** —— 人话说明「改了什么、影响谁」，否则用户无从比较。
2. **`impact` 必填** —— 改动几条日程 / 涉及几个对象，量化影响面。
3. **🔴 方案必须去重**：top-3 里若两个方案的 `date + slots` 完全相同，只保留一个。
   `_candidate_masks` 生成的候选高度相似（都挪到相邻节次），不去重会让三个方案看起来是同一个。

N = **3**（MVP）。少于 3 看不出选择，多于 3 决策瘫痪。

### 6.2 `warnings`：软约束违规（容量等）**一律警告，不阻止**

队长 2026-09-25 定：**只要不违反硬约束，一律给警告 + 给方案，让用户选。**
`warnings` 与硬冲突的区分：

| | 硬冲突 | 警告（warning） |
|---|---|---|
| 含义 | 必须解决（撞课） | 不理想但可接受（容量、末节、连堂被拆） |
| UI | 🔴 必须消解 | ⚠️ 提示，由用户在方案间权衡 |
| 求解 | 一律排除 | 计入代价，不排除 |

⚠️ **`capacity IS NULL`（教室未填容量）时不检查也不告警**，否则满屏噪音。

### 6.3 `termination`：三种终止态必须可区分（A5）

`no_solution`（无可行方案）/ `ok`（已全部消解）/ `best_effort`（已尽力但仍有残留）
**不得合并返回**，否则现场失败时界面只能"什么都没发生"，讲不出所以然。

---

## 7. POST /api/demands（S17 新增）

| 项 | 内容 |
|---|---|
| 请求 | `{"raw_text":"下周三下午实验室A我要用，必须安排","group":"...", "hard":true}` |
| 响应 200 | `{"ok":true,"demand_id":"dem_x","parsed":{...},"raw_text":"下周三下午实验室A我要用，必须安排","conflicts":[...],"message":"该需求与现有 3 条课表冲突，无法完全满足，已避开 2 条"}` |
| 说明 | 🔴 **绝不静默失败**：与现有日程冲突时必须明确反馈 |

✅ **已定（队长 2026-10-01）：前端传原话、后端解析、原话入库。**

1. **前端只传用户原话** `raw_text`，**不做任何预处理/结构化**（不自己拆时间、不自己猜对象）。
2. **解析规则全部在后端**（`nl/**`，张晋嘉）——前端永远不知道规则长什么样，改规则不用动前端。
3. **原话必须原样存储**：响应体回显 `raw_text`，落库保留原文。
   → 理由① 解析错了可追溯、可人工纠正；② 答辩时能拿出"用户输入了什么、系统怎么理解的"对照。
4. `hard` 表示"必须满足"（硬需求）/`false` 为"尽量安排"（软需求）。
5. 🔴 **发给外部大模型的内容只能是这段 `raw_text` 本身**（AGENTS §7 数据合规），
   严禁附上课表、班级名、姓名、联系方式。

---

## 8. POST /api/nl（S19 新增）

| 项 | 内容 |
|---|---|
| 请求 | `{"text":"把周三第1节的物理调到周三第3节","group":"..."}` |
| 响应 200 | `{"intent":"reschedule","display":"把周三第1节的物理调到周三第3节","params":{...},"source":"local\|cache\|cloud","validated":true}` |
| 说明 | 🔴 `validated` 必须为 true 才展示——**所有解析结果必须回送求解器校验** |
| 说明 | `source=cloud` 仅在本地层解析不了时出现；失败自动静默回落 `local` |

✅ **已定（队长 2026-10-01）**：自然语言是**让用户用得更方便**的入口，
功能**包括但不限于**「解析规则」与「分析安排」——**定位是入口层，不是单一功能**。

**`intent` 枚举（v1.0，5 种）**：

| 值 | 含义 | 对应能力 |
|---|---|---|
| `reschedule` | 调课（改某条日程的时间） | 解析规则 |
| `add_once` | 加一次性活动 | 解析规则 |
| `cancel` | 临时取消（对应 `schedule_exceptions` 的 `cancel`） | 解析规则 |
| `trigger_m2` | 触发一键消解 | 分析安排 |
| `analyze` | 查询 / 分析安排（"这周谁最满""周三下午空不空"） | 分析安排 |

🔴 **扩展性三条（队长要求"留足够灵活性"）**：

1. **这不是封闭清单**——后续可加 `swap`（两节课对调）、`move_period`（整周平移）等，属非破坏性变更。
2. **前端不得内置枚举白名单**：遇到未知 `intent` → 按 §12 降级处理（展示 `display` 文案，不崩、不空白）。
3. **`display` 由后端给**：人话描述"系统理解成了什么"，**前端不自己维护 意图→文案 映射表**。
   → 这样以后新增一种意图，**前端零改动**。

---

## 9. GET /api/health

| 项 | 内容 |
|---|---|
| 响应 200 | `{"ok":true}` |
| 说明 | 🟡 **只返回这一句**，不带版本号/路径（公网下避免指纹识别） |

---

## 10. POST /api/ping（S14 埋点）

| 项 | 内容 |
|---|---|
| 请求 | `{"session_id":"...","event":"...","payload":{...},"group":"...?"}` |
| 鉴权 | 必须登录（Bearer token），未登录 401；`group` 越权 403 |

🔴 **本接口的口径以 `docs/08-试用组织数据口径.md` 为准**（该文件是数据口径的唯一来源）。
此处只列要点，与 docs/08 冲突时以 docs/08 为准：

- **事件三元组（缺一不可）**：`task_start` / `task_success` / `task_abandon`
  → 缺 `task_start` 就算不出任务完成率的分母。
- **秒级时间戳**，不是 60 秒粒度；**不依赖 logout**（关浏览器不发）。
- `trial_code` 由**后端从登录名推导**（`trial_tNN`→`TNN`），前端不传（docs/08 §9.2）；
  `session_id` 每会话随机（用于时长）。
- ~~前端每 60 秒打一次~~ **已作废**：60 秒粒度 + 挂机计入会虚高，改用 docs/08 §3.3 的
  「相邻事件间隔 ≤ 5 分钟」有效时长规则。

---

## 11. POST|DELETE|GET /api/exceptions（S21 临时微调 · 队长 2026-09-25 批准；B9 2026-10-14 补 GET）

| 项 | 内容 |
|---|---|
| POST（新增例外） | `{"schedule_id":"sch_x","date":"2026-09-23","mode":"cancel\|move","new_mask":"0x0008"}` |
| DELETE（撤销例外） | `?id=exc_x` → 该日恢复按周期规则展开 |
| GET（列出未撤销例外） | `?schedule_id=sch_x` → `{"count":n,"items":[{...}]}` |
| 响应 200（POST） | `{"ok":true,"exception_id":"exc_x","affected_dates":["2026-09-23"],"mode":"cancel","schedule_id":"sch_x","effective":true}` |
| 响应 200（DELETE） | `{"ok":true,"exception_id":"exc_x","affected_dates":["2026-09-23"]}` |
| 响应 200（GET） | `{"count":2,"items":[{"id","schedule_id","title","date","mode","new_mask","readable","note","created_at"}]}` |
| 权限 | 登录 + 组织隔离；越权 → 404 |

**B9 实现口径（2026-10-14，队长裁定）**：

| 项 | v1.0 原文 | B9 落地 | 理由 |
|---|---|---|---|
| 权限档 | 「需 `edit` 档」 | 登录 + 组织隔离 | 代码里没有 `edit` 档（docs/06 四档是 view/propose/approve/own，teacher 天花板就是 propose）。真正卡「谁能批准」的是审批流（docs/06 已冻结、**已进本契约 §12.4**），不是本接口。 |
| 请求字段名 | `schedule_id` | `schedule_id`（兼容 `target_id`，两者都给且不一致 → 400） | docs/12 的示例用过 `target_id`；静默选一个会让前端以为传的那个生效了。 |
| 掩码口径 | 未写明 | 出入参 = **单日掩码**（1-based，`0x000002`=第1节）；入库 = **周掩码裸 hex**（与 `slot_bitmap` 同口径） | 与 `/api/schedules` grid 出参一致；两套位序混用会让页面整体错位且不报错（本项目最贵的坑）。 |
| `effective` | 无此字段 | 新增（§12.3 非破坏性加字段） | 「这条例外声称影响哪一天」与「实际改变了占用」是两件事：周六/假期/单双周不匹配时写入成功但不生效，响应体如实说 `false`，不假装成功。 |
| 重复例外 | 未写明 | 同 (日程, 日期) 已有 cancel/move → **409** 并带上已有 id | 不拦会静默失效（cancel 先命中、move 永远不生效）或让全组织 `/api/conflicts` 500（两条 move 掩码矛盾 → `load_exceptions` 抛 ValueError）。 |
| `mode='special'` | 未开放 | 仍不开放（400） | 没有「换一套作息」的前端与规则支撑前，开放它等于开放一条没有语义的写入路径。 |

🔴 **两条实现约束**（v1.0 原文，B9 已逐条落实）：

1. 例外必须被 `expand_periodic()` **和** `hard_violations()` **同时读取**——
   只改一处会让"占用显示空着、排课时按有课处理"，把本项目要解决的核心缺陷重新引入。
   B9 的落点：`build_occupancy` 在展开之后逐日应用（cancel/move），
   `hard_violations` 经 `ctx.occupancy` 自动同步；**消解器 `m2.schedule_masks`
   也必须应用**——否则老师取消某天后，求解器仍以为该日程占着原槽位，
   对一个已经不存在的冲突提案「把这条日程挪走」。
2. **DELETE 即撤销**（对应 V10）—— 微调改错了必须能回退，否则教师不敢用。
   例外行一删，占用逐位恢复 `expand_*` 的原始结果。

表结构见 `docs/12`（`schedule_exceptions`，`scope=day` 的 `as_of` 模式同时解决调休 A1）。
`new_mask` 校验链（全部 400，绝不静默兜底）见 `src/server/b9_kernels.py` 的
`parse_api_mask` docstring；`scope='schedule' 且 target_id 为 NULL` /
`mode='move' 且 new_mask 为 NULL` / 多条 move 掩码矛盾 → `load_exceptions`
抛 `ValueError`（P0-3 语义：配置错误宁可报错）。

---

## 12. 灵活性与向前兼容（🔴 队长 2026-10-01：给后续拓展留足空间）

### 12.1 三条总原则

1. **只增不减**：v1.0 之后只允许**加字段、加枚举值**，不改已有字段名/类型/语义。
2. **后端给文案，前端不维护映射表**——新增一种意图或类型时，**前端零改动**。
3. **前端宽容解析，后端严格产出**：前端对未知内容降级不崩；后端对自己的产出负责到底。

### 12.2 前端必须做到的五条（写进 F 卡验收项）

| # | 规则 | 禁止的反例 |
|---|---|---|
| 1 | 响应里**多出未知字段** → 忽略，不报错 | 严格校验 JSON schema，多一个字段就抛错 |
| 2 | **未知枚举值** → 用后端给的 `display` 文案降级显示，**不空白、不崩** | `switch(intent)` 没有 default 分支 |
| 3 | **数组长度不写死**（`plans` 现在 3，将来可能 5）→ 按 `length` 渲染 | 写死 `for(i=0;i<3;i++)` |
| 4 | **不内置校验白名单**，校验在后端 | 前端拒绝"不认识的 kind"直接不渲染 |
| 5 | **掩码只透传展示**，不做字符串比较、不做位运算 | `slots === "0x0006"`；自己解码掩码（AGENTS §8/§9b） |

### 12.3 后端扩展时的三条纪律

1. 新增字段 = **非破坏性变更**，不动已有字段；
2. 新增请求参数**一律可选**，缺省走旧行为；
3. 新增枚举值**必须同时给出 `display` 文案**（否则前端第 2 条无从降级）。

### 12.4 已预留的扩展位（将来要加时不用改架构）

| 可能的扩展 | 预留方式 |
|---|---|
| 每天节数超过 16（掩码变宽） | 掩码**一律整数比较**（AGENTS §9b），宽度不进代码 |
| 新增自然语言意图（两节课对调、整周平移） | `intent` 非封闭清单 + `display` 后端给 |
| 新增对象类型 | `kind` 新增值 → 前端默认样式降级 |
| 方案数量变化 | 前端按数组长度渲染，不写死 3 |
| **审批流接口**（`change_requests`） | ✅ **已进本契约**（B8，2026-10-16）：`src/server/api_approvals.py` 导出 6 条 `/api/approvals` 路由（submit·list·approve·reject·withdraw·events）。🔴 路由精确匹配、**无 path 参数**（`http_api.py:110`），id 一律 **query 传**：`POST /api/approvals/approve?id=cr_x`（**不是** `/api/approvals/<id>/approve`） |
| 偏好权重 / M1 全局排课（当前暂缓） | 走新增可选参数，不动现有 `resolve` 行为 |

### 12.5 版本号

**契约 v1.0（2026-10-01 冻结）**。此后每次变更递增版本号、更新本文件、**并在群里说明**——不允许口头改。

---

## 13. GET | POST | DELETE /api/announcements（2026-11-13 新增，卡 rTVvb8）

> 组织公告：管理员在网页上编辑一条公告，本组织所有已登录用户都能看到。
> **纯新增接口，不改 §1–§12 的任何字段与行为。**
> 🔴 路由**精确匹配、无 path 参数**（`http_api.py:129`），id 一律走 **query**：
> `DELETE /api/announcements?id=ann_x`，⛔ 不是 `/api/announcements/<id>`。

| 项 | 内容 |
|---|---|
| 权限 | **READ** = 登录即可（`view` 档）｜**WRITE/DELETE** = `own` 档（「修改组」），不足 **403** |
| 作用域 | **组织级**：公告只在本组织可见；跨组织的 `group` → 403，跨组织的 `id` → 404 |
| 数量 | 每组织**恒一条**（`UNIQUE(org_id)`）：`POST` 是 upsert（覆盖同一条，id 不变） |
| 当前组 | `group` 缺省 = 用户默认组（同 §4 口径）。⚠️ 多组织管理员建议**显式传 group**——缺省值在跨组织并列时不保证固定组织 |
| 未登录 | **401** `{"error":{"code":"unauthorized","message":"未登录或令牌无效"}}` |

### 13.1 GET /api/announcements?group=

| 项 | 内容 |
|---|---|
| 请求 | `group`（可选，query） |
| 响应 200 | `{"ok":true,"announcement":{"id","title","body_md","updated_at","updated_by_name"}}` |
| 响应 200（无公告/已停用） | `{"ok":true,"announcement":null}` |
| 403 | `group` 越权（`B3.resolve_authorized_org`） |

```json
{"ok": true,
 "announcement": {"id": "ann_3f2a1b4c5d6e7f80",
                  "title": "本周三教研活动",
                  "body_md": "## 安排\n- 14:00 开始\n- 地点：阶梯教室",
                  "updated_at": "2026-11-13 12:34:56",
                  "updated_by_name": "系统管理员"}}
```

**三条口径（写死，不得自改）**：

| # | 口径 | 理由 |
|---|---|---|
| 1 | 无公告 → `announcement: null`（HTTP **200**），⛔ 不是 404、不是 `{}` | 前端必须能区分「没有公告」与「接口失败」；404 会被当成错误态 |
| 2 | `body_md` 是 **Markdown 原文**，后端**不转义、不 strip** | 原样存原样还；防 XSS 在前端（`textContent` / `mdToDom()`，⛔ 禁 `innerHTML`） |
| 3 | `updated_at` 是 **UTC**（`datetime('now')`），原样返回 | 与全库同口径；⛔ 前端不得用 `new Date('YYYY-MM-DD HH:MM:SS')` 猜时区 |

⛔ 不返回 `org_id` / `active` / `updated_by` / `created_at`：作用域由请求的 `group` 决定，
前端不该拿 `org_id` 去跨组；其余是内部审计列。

### 13.2 POST /api/announcements

| 项 | 内容 |
|---|---|
| 请求体 | `{"group": "obj_x"（可选）, "title": "标题", "body_md": "Markdown 原文"}` |
| 响应 200 | `{"ok":true,"created":true,"announcement":{…}}`（`created:false` = 覆盖已有公告） |
| 400 | `title` 缺失/空白/非字符串/超 200 字；`body_md` 非字符串；`group` 非字符串；JSON 不合法 |
| 403 | 档位不足（教师天花板 = `propose`）或 `group` 越权 |

- `group` 放在**请求体**里（与 §7 `POST /api/demands` 同风格）；`DELETE` 没有 body，只能走 query。
- 覆盖时 `announcement.id` 不变；已停用的公告再次发布**自动复活**（`active` 置回 1）。
- `title` 入库前 `strip()`；`body_md` **原样入库**。

### 13.3 DELETE /api/announcements?group=&id=

| 项 | 内容 |
|---|---|
| 请求 | `id`（必填，query）、`group`（可选，query） |
| 响应 200 | `{"ok":true,"id":"ann_x","active":false,"already_inactive":false}` |
| 响应 200（重复停用） | `{…,"already_inactive":true}` —— **幂等**，双击/重试不报错 |
| 400 | 缺 `id` |
| 403 | 档位不足或 `group` 越权 |
| 404 | `id` 不存在**或属于别的组织**（不泄漏「这个 id 在别处存在」） |

- 停用是**软删**：行保留、`active=0`，停用后 `GET` 返回 `announcement: null`。
- 写路径的 SQL 带 `org_id`，所以拿别组织的 id 来停用影响行数必然是 0 → 404。

---

## 变更记录

| 日期 | 变更 | 谁改的 |
|---|---|---|
| 2026-09-25 | 建骨架（字段标 `待填`，由 T1 补齐） | 队长（AI 辅助） |
| 2026-09-25 晚 | v4.7：补充 §6.1 多方案响应体（top-3 + 去重 + diff_text）、§6.2 warnings 定性、§6.3 终止态三态；§10 埋点改为三元组并改以 docs/08 为准；新增 §11 临时微调接口 | 队长（AI 辅助） |
| **2026-10-01 晚** | **v1.0 冻结**：队长逐项拍板 8 处待填（§1 最高组／§2 要人数／§3 kind 按原定义／§4 竖线+本周／§5 字段给全／§7 传原话+后端解析+存原话／§8 intent 五值含 analyze）；**新增 §12 灵活性与向前兼容** | 队长拍板，AI 起草 |
| 2026-10-27 | §5 新增 `activity_window`（一次性活动窗口哨兵，§12.3 非破坏性加字段，不动既有字段）；`deploy/trial_ready.py` ③ 同步把它当硬判据 | 审查方（待队长在群里说明） |
| 2026-11-13 | **新增 §13 组织公告**（`GET\|POST\|DELETE /api/announcements`，卡 rTVvb8）：每组织恒一条（upsert）、`own` 档可写、组织隔离、`announcement: null` 语义、`already_inactive` 幂等、`body_md` 原样不转义。纯追加，§1–§12 未改 | 队长（AI 辅助） |
