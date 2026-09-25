# 项目指令（AGENTS.md）

> 本文件是**给 AI 与队员共同遵守的硬约束**。任何会话打开本仓库都会读到它。
> 配套《构建计划 v4.7（🛑 暂停开工中）》：**队长本机** `C:/Users/41271/.workbuddy/plans/electric-pulse-darwin-4_-j6-kG.md`
> （队员无此路径也无妨——**执行口径以任务卡为准**，计划只作背景，冲突时以本文件为准）。
> 冲突时**以本文件为准**（计划会变，硬约束不变）。

---

## 1. 项目一句话

把课表（周期事务）与活动（一次性事务）归一化到同一套时间占用模型，
用**自建约束求解器**做统一冲突检测与一键消解；按组做四档权限；自然语言入口默认本地解析。
**全程本地运行、断网可用。**

---

## 2. 技术栈硬约束（🔴 违反即返工）

- **Python 标准库 + SQLite，零第三方依赖**。不得引入 FastAPI / Flask / requests / 任何需要 pip 安装的包。
- **必须断网可跑**（赛事章程硬要求）。`requirements.txt` 保持**全注释**，仅作占位说明。
- HTTP 层用 `http.server`（`ThreadingHTTPServer`），**不用 FastAPI**。
- 领域层写成与框架无关的纯函数，将来换框架只替换适配层。

---

## 3. 领域层红线（🔴 最高优先级）

- `src/scheduler/` 下的**现有文件只读**，一行都不得改。
- **唯一例外**：新增 `src/scheduler/authz.py` + 在 `schema.sql` **末尾追加** `group_roles` 建表语句
  （任务卡 T2a，走 E2 流程，**需队长批准**，且完成后 **31 项原有测试必须全绿**）。
- `tests/test_b0.py`、`tests/test_b4.py` 只读——这是回归底线。

**✅ 已批准的 E2 例外（队长 2026-09-25 批准，可直接实施）**：

| 范围 | 用途 | 执行人 | 条件 |
|---|---|---|---|
| `occupancy.py` 的周期展开逻辑 | 支持 `schedule_exceptions`（临时取消/改点 + 调休 `as_of`） | Ryan | 31 项老测试全绿 + 新增用例；改动仅限**读取例外的展开分支**，不得改位号公式 |
| `schema.sql` 末尾追加 | `schedule_exceptions` 表、`data_revision` 表 | Ryan | 纯追加，不动已有表 |
| **`m2.py` 新增函数**（多方案 top-N） | `GET /api/resolve` 返回 top-3 | Ryan | 🔴 **只许新增函数，现有任何一行都不得改**；`resolve()` / `resolve_and_apply()` 的行为必须**逐字节不变**（31 项老测试 + `smoke.py` 全绿） |

> 🔴 例外表必须被 `expand_periodic()` **和** `hard_violations()` **同时读取**。
> 只改一处 = "占用显示空着、排课时按有课处理"，把本项目要解决的核心缺陷重新引入。
- 任何时候 `python -m unittest discover -s tests` 出现失败，若涉及领域层 → **立即 revert**，不排查不停留。

---

## 4. 文件所有权（三人并行 · 唯一写者原则）

| 负责人 | 独占文件 / 目录 |
|---|---|
| **Ryan_L_F** | `src/scheduler/authz.py` + `schema.sql` 末尾追加<br>`src/server/auth.py`、`api_login.py`、`api_groups.py`<br>`src/server/api_objects.py`、`api_schedules.py`、`api_conflicts.py`、`api_resolve.py`<br>`src/server/api_demands.py`、`api_nl.py`、`nl/**`<br>`tests/test_authz.py`、`test_server_auth.py`、`test_server_groups.py`、`test_server_api.py`、`test_server_resolve.py`、`test_nl.py` |
| **独此一嘉** | `src/server/config.py`、`http_api.py`、`routes.py`、`static.py`、`telemetry.py`<br>`web/**`<br>`scripts/doctor.py`、`run_server.py`、`smoke.py`、`telemetry_report.py`<br>`deploy/**`、`docs/05-*`、`PLAN.md`<br>`tests/test_server_core.py`、`test_telemetry.py` |
| **王思勋（队长）** | 文档：`docs/01-项目描述-可直接复制.md`、`docs/06-权限模型.md`、`docs/08-试用组织数据口径.md`、`docs/09-夏冬作息方案.md`、README 进度表<br>**叶子实现**：`scripts/build_trial_org.py`、`tests/test_contract.py`<br>生成物（不入 git）：`data/trial.db`、`data/scrub.salt`<br>⚠️ **队长只接叶子任务**，见下方三条判据 |

**给队长分配任务的三条判据**（2026-09-25 定）：

1. **叶子任务** —— 不被别人依赖，也不依赖别人的产出不在关键路径上。
2. **不在自己的抽检范围内** —— 若产出物恰好会被自己抽检，该核验环节换人交叉执行（例：试用组织数据的脱敏核验由 Ryan 跑）。
3. **必须有独占文件** —— 文件名错开，绝不与他人同目录。

> ⚠️ 因此 `scripts/doctor.py`、`smoke.py`、`telemetry_report.py` 仍属独此一嘉，**队长不要建同名文件**。

**两条跨人约定（必须遵守）**：

1. **禁止手工改 `routes.py`** —— 它是**约定式自动注册**（扫描 `src/server/api_*.py`，导出 `ROUTES` 即挂载）。
   新增 API **只需加文件**。谁手工往里加注册行，就破坏了三人并行的基础。
2. **`handle(method, path, query, body, headers, context=None)` 签名已在 T1 写死**，
   `context=None` 表示未登录，鉴权由 Ryan 的装饰器统一返回 401。**此签名不得改。**

---

## 5. 分支（每人只在自己的分支上推，`main` 只接受 PR）

```
feat/server-core     独此一嘉  S1–S4
feat/authz-domain    Ryan      T2a 领域层（不依赖服务层，可立即开工）
feat/server-authz    Ryan      T2b 登录鉴权 + /api/groups
feat/server-api      Ryan      S6–S9 + S18
feat/web             独此一嘉  S10–S12
feat/nl              Ryan      S17 + S19 + S20
feat/ops             独此一嘉  S13 + S14
feat/deploy          独此一嘉  S15 + S16
```

---

## 6. 提交规范

```bash
# 三连绿（缺一不可）
python -m unittest discover -s tests
python scripts/doctor.py        # S1 后
python scripts/smoke.py         # S13 后

git commit -m "S9: /api/resolve 可动集合过滤"   # 必须以 S#n: 开头
git tag step/S9
```

- commit 正文必须写清**用途与设计意图**（便于队员理解与答辩）。
- PR 描述必须注明：对应任务卡号、**AI 生成的部分**、用途与设计意图。
- `feat/authz-domain` 触碰领域层，**merge 前必须队长批准 + 31 项老测试全绿**。
- ✅ **GitHub 连接器已连接（2026-09-25 晚起）**：PR / Issue 直接用连接器操作，不再需要退回 gh CLI。
- ⚠️ **若 `git push` 失败，最多重试 2 次再判断**：偶发不通属正常现象，**多数情况重试即成功**
  （2026-09-25 当天多次重试均推送成功）。连续失败时先照样做提交与 tag，
  再用连接器建 PR，或由队长在本机推送。
  > 此条替代了 v1 的「不要反复重试 / 沙箱对 github.com 不可达」——那条已过时。

---

## 7. 数据合规（🔴 不可协商）

- **真实学生名单、真实班级名、真实课表：不得出校、不得上公网**（校规）。
- 公网部署**只放脱敏的「试用组织」数据**；10/18 试用结束即关停。
- **发给外部大模型（GLM）的内容只能是用户指令文本本身**，
  严禁携带课表、班级名、姓名、联系方式。默认假定外部会留存一切。
- 埋点（`event_log`）只出聚合数；`user_ref` 用**登录时生成的随机串**，不用 `sha256(login+salt)`。
- 埋点数据**永不外发**。

---

## 8. 前端约束

- 原生 JS，**无构建步骤**，**零 `https://` 外链**（断网必须能跑）。
- **禁用 `innerHTML`**，动态文本一律 `textContent`（防存储型 XSS）。
- 字号要**1 米可读**（现场是展台答辩，评委站着看）。
- 前端**不做位运算**，mask 由服务端给。
- 🔴 **静态资源必须带版本号**（`app.js?v=20261008`），版本号由 `config.py` 统一导出；
  `static.py` 加 `Cache-Control: no-cache` + `ETag`。
  > 不加会导致「改了 JS 但老师浏览器还在用旧版」——现场改样式时必踩，且极难排查。
- **不做 Service Worker / PWA**（MVP）。断网要求靠「本地部署 + 无外链」满足，不靠缓存机制。
- 数据变更同步见 **`docs/11-客户端更新与数据同步.md`**：MVP 用轮询（10 秒），**不做 WebSocket**。

---

## 9. 权限模型速查（不要让 AI 自由发挥）

- **四档**：`view` → `edit`（改自己参与的）→ `manage`（改本组任何人的）→ `own`（含授权他人）。
- **有效授权** = 沿被操作对象（或当前组）的**所有祖先组含自身**，取该用户的最高 `level`。
- **全局角色是天花板**：`admin`=own（全组织短路）｜`teacher`=own｜`terminal`=view｜`student`=view。
- **多路径取 `max`**，不做就近优先。
- **不做负权限 / deny**（DAG 多父下语义不自洽）。收回权限 = 删掉那一行。
- 授权表列名是 `level` 而**不是 `grant`**——`GRANT` 是 SQL 保留字。

---

## 9c. 约束定性表（🔴 别把警告做成硬约束）

队长 2026-09-25 定：**只要不违反硬约束，一律给警告 + 给方案，让用户选。**

| 约束 | 定性 | 违反时 |
|---|---|---|
| 教师 / 教室 / 班级撞课 | 🔴 **硬** | 一律排除，绝不出方案 |
| **教室容量不足** | ⚠️ **软（警告）** | 计入代价、UI 提示，**不阻止**（队长 2026-09-25 降级） |
| 单日课时上限 | ⚠️ 软 | 计入代价 |
| 主科排末节 | ⚠️ 软 | 计入代价 |
| 连堂被拆散 | ⚠️ 软 | 计入代价 |

- ⚠️ **`capacity IS NULL` 时不检查也不告警**（教室未填容量）。
- 硬冲突 UI 用 🔴，警告用 ⚠️，**必须分级显示**；只写日志等于没做。
- 理由：容量数据未必准确，若做成硬约束会导致**无解**，现场演示风险大。

---

## 9b. 槽位配置约定（🔴 别踩）

- 位串容量 **7 天 × 16 节 = 112 位**；每天格数上限 16（`slots.py: MAX_PERIODS_PER_DAY`）。
- 🔴 **不许改 `MAX_PERIODS_PER_DAY`** —— 位号公式依赖它，改了所有已存掩码全部错位、必须迁移。
  要扩容只改 `period_slots` 表内容，或改 `setup_default_slots()` 的入参，**不改代码常量**。
- 🔴 **周六周日必须 `enabled = 1`**（队长 2026-09-25 选 A：全场景可用）。
  `mask_for_range()` 只遍历启用槽位，周末 `enabled = 0` 会让周末活动映射出空掩码，
  冲突检测看不见它 —— 等于把本项目要解决的核心缺陷在周末场景复发。
  周末**显示与否由前端决定**，不要用 `enabled` 控制显示。
- 默认建表是 `periods_per_day = 10`；若作息含早读 + 晚自习，初始化时传入 12–13。

---

## 10. 已知限制（诚实写进文档，别吹）

- ~~暂不支持调课补课（`makeup` 已定义未实现）~~
  ✅ **2026-09-25 队长批准纳入 MVP**（理由：「不做效果很差」）。
  与临时微调**共用 `schedule_exceptions` 表**（`scope='day'` + `mode='as_of'`），
  E2 例外已批准（见 §3），方案见 `docs/12`。
  ⚠️ **实现完成前，对外文案仍不得宣称支持**。
- 跨天活动需拆成两条 `once`，且必须同组。
- 非整节（如 14:15–15:00）安排支持有限，按连续区间处理。
- **不做实时推送 / 多人协同编辑**（MVP）。数据同步为 10 秒轮询，见 `docs/11`。
- ~~暂不支持临时微调（某周某节课临时取消 / 改点）~~
  ✅ **2026-09-25 队长批准纳入 MVP**（理由：「否则会非常难用」）。
  做 `cancel` + `move` 两种 `mode`；**对调 / 换作息留二期**。见 `docs/12` + 契约 §11。
  ⚠️ 实现完成前本条仍然成立。
- **两条课对调**（A 与 B 互换）MVP 不做 —— 引导用户做两次 `move`。

---

## 11. 遇到不确定的事

- **不要编造校方规章、校历、人数等信息**——不确定的一律标为"待确认"。
- **不要为了赶工砍硬约束**（零依赖、断网可跑、领域层红线、数据合规）。
- 缓冲只有约 1 天，**该砍的先砍 S20（GLM 适配器）**，不要砍主线。
- 拿不准就停下来问队长，不要猜。
