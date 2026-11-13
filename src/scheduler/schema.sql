-- =============================================================================
-- 日程调度辅助系统 · 数据层（B0 批次）
--
-- 设计意图（AI 生成，供队员阅读与答辩讲解）：
--   1. 「对象」与「登录账号」是两层正交概念：
--        objects 表 = 谁可以被调度（学生/班级/教师/教室…）
--        users   表 = 谁可以登录操作（只有真要登录的才有行）
--      学生 1500 人可以在 objects 里全量存在，但不一定要有 users 行。
--   2. 「组织」不进对象图，单独 organizations 表；建组织时自动建一个"全校"根组，
--      年级/班级/社团挂其下。这样"全校活动"只要选根组，不必为组织写特判。
--   3. 组织结构是 DAG（不是单亲树）+ 闭包表：一个学生可以同时属于班级和社团。
--      object_edges 存 (祖先, 后代, 距离)，含自反边（自己到自己，depth=0）。
--   4. 周期日程（课表）与一次性活动（事件）共享底层：
--        schedules 是基础表（共用字段），
--        schedule_periodic / schedule_once 是类型扩展表（特有字段），
--      这是"类表继承"。应用层通过 v_schedule_full 视图查询，不必手写 JOIN。
--      归一化的目标形态统一为「(日期, 当天槽位掩码)」，冲突检测只有一套位运算。
--   5. 存储格式约定：日期/时刻用 ISO 文本（可读、可排序）；
--      掩码用十六进制文本（SQLite 整数放不下 112 位掩码）；
--      单双周用整数 0=每周 / 1=单周 / 2=双周；
--      "第几周"不入库，由应用层从 terms.start_date 推算。
-- =============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 组织：学校。不进对象图。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organizations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- 对象：可被调度的一切。kind 分三类
--   group    = 组（年级/班级/社团/教研组…），组织成 DAG
--   user     = 自然人（教师、学生…）
--   resource = 资源（教室/实验室/操场…）
-- org_id 是冗余字段，用于组织级别的数据隔离与快速过滤。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS objects (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('group', 'user', 'resource')),
    name          TEXT NOT NULL,
    capacity      INTEGER,                       -- 仅 resource 有意义（容纳人数）
    is_root       INTEGER NOT NULL DEFAULT 0 CHECK (is_root IN (0, 1)),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_objects_org_kind ON objects(org_id, kind);

-- ---------------------------------------------------------------------------
-- 闭包表：对象图的传递闭包，含自反边（depth=0）
-- 查询"某组下所有人"= SELECT descendant_id FROM object_edges WHERE ancestor_id=?
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS object_edges (
    ancestor_id   TEXT NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
    descendant_id TEXT NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
    depth         INTEGER NOT NULL CHECK (depth >= 0),
    PRIMARY KEY (ancestor_id, descendant_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_desc ON object_edges(descendant_id);
CREATE INDEX IF NOT EXISTS idx_edges_anc_depth ON object_edges(ancestor_id, depth);

-- ---------------------------------------------------------------------------
-- 身份层：只有真正需要登录的对象才有这一行
-- role 决定权限边界：admin=管理员 / teacher=教师 / terminal=班级终端只读 / student=学生
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    object_id     TEXT NOT NULL UNIQUE REFERENCES objects(id) ON DELETE CASCADE,
    login         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'teacher', 'terminal', 'student')),
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

-- ---------------------------------------------------------------------------
-- 节次配置：槽位不是写死在代码里的，而是这张表动态生成
-- 可以留余量（例如配 7 天 × 12 节 = 84 槽），前端只显示 enabled=1 的部分
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS period_slots (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),  -- 1=周一
    idx         INTEGER NOT NULL CHECK (idx >= 1),
    name        TEXT,
    start_time  TEXT NOT NULL,   -- 'HH:MM'
    end_time    TEXT NOT NULL,   -- 'HH:MM'
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    UNIQUE (org_id, day_of_week, idx)
);
CREATE INDEX IF NOT EXISTS idx_slots_org ON period_slots(org_id);

-- ---------------------------------------------------------------------------
-- 学期：提供"第几周"的推算基准
-- week1_parity: 第 1 周是单周(1)还是双周(2)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS terms (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    start_date   TEXT NOT NULL,   -- ISO 'YYYY-MM-DD'，建议为开学周的周一
    end_date     TEXT NOT NULL,
    week1_parity INTEGER NOT NULL DEFAULT 1 CHECK (week1_parity IN (1, 2))
);
CREATE INDEX IF NOT EXISTS idx_terms_org ON terms(org_id);

-- ---------------------------------------------------------------------------
-- 日历例外：节假日停课 / 调课补课
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_exceptions (
    id      TEXT PRIMARY KEY,
    org_id  TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    date    TEXT NOT NULL,                      -- ISO 'YYYY-MM-DD'
    kind    TEXT NOT NULL CHECK (kind IN ('holiday', 'makeup')),
    note    TEXT,
    UNIQUE (org_id, date)
);

-- ---------------------------------------------------------------------------
-- 日程基础表（类表继承的父类）
-- kind: periodic=长期重复事务（课表）/ once=一次性事务（活动）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schedules (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('periodic', 'once')),
    title      TEXT NOT NULL,
    created_by TEXT REFERENCES objects(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_schedules_org ON schedules(org_id, kind);

-- 周期日程扩展表：一周掩码 + 单双周 + 生效区间
CREATE TABLE IF NOT EXISTS schedule_periodic (
    schedule_id TEXT PRIMARY KEY REFERENCES schedules(id) ON DELETE CASCADE,
    week_parity INTEGER NOT NULL DEFAULT 0 CHECK (week_parity IN (0, 1, 2)),
    slot_bitmap TEXT NOT NULL,     -- 十六进制文本，一周掩码
    valid_from  TEXT NOT NULL,     -- ISO 'YYYY-MM-DD'
    valid_to    TEXT NOT NULL
);

-- 一次性日程扩展表：起止时刻。跨天活动存两条、共享同一 group_id，前端聚合展示
CREATE TABLE IF NOT EXISTS schedule_once (
    schedule_id TEXT PRIMARY KEY REFERENCES schedules(id) ON DELETE CASCADE,
    start_at    TEXT NOT NULL,     -- ISO 'YYYY-MM-DDTHH:MM'
    end_at      TEXT NOT NULL,
    group_id    TEXT
);

-- 参与者：角色分三类
--   target      = 排课的对象（如某个班）
--   teacher     = 授课/负责教师（教师也是 objects 中的 user 行，与 target 同一套逻辑）
--   participant = 活动的参与者对象
CREATE TABLE IF NOT EXISTS schedule_participants (
    schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    object_id   TEXT NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('target', 'teacher', 'participant')),
    PRIMARY KEY (schedule_id, object_id, role)
);
CREATE INDEX IF NOT EXISTS idx_part_obj ON schedule_participants(object_id, role);

-- 占用资源：教室/场地。纳入冲突检测
CREATE TABLE IF NOT EXISTS schedule_resources (
    schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
    PRIMARY KEY (schedule_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_res_res ON schedule_resources(resource_id);

-- ---------------------------------------------------------------------------
-- 统一视图：屏蔽类表继承的 JOIN，应用层只查这一张
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_schedule_full AS
SELECT s.id, s.org_id, s.kind, s.title, s.created_by, s.created_at,
       p.week_parity, p.slot_bitmap, p.valid_from, p.valid_to,
       NULL AS start_at, NULL AS end_at, NULL AS group_id
FROM schedules s
JOIN schedule_periodic p ON p.schedule_id = s.id
UNION ALL
SELECT s.id, s.org_id, s.kind, s.title, s.created_by, s.created_at,
       NULL, NULL, NULL, NULL,
       o.start_at, o.end_at, o.group_id
FROM schedules s
JOIN schedule_once o ON o.schedule_id = s.id;

-- ---------------------------------------------------------------------------
-- 组级授权表（B2 · 权限层）
--   设计意图（AI 生成，供答辩讲解）：
--     1. 「权限按组」而非「全局角色一把梭」——领导身份体现在管的哪个组，
--        不在全局角色（AGENTS §9）。全局角色（users.role）只当天花板。
--     2. 列名是 level 而非 grant——GRANT 是 SQL 保留字（docs/06 §1）。
--     3. level 四档语义（审批流下重定义）：view 看 / propose 提请求 /
--        approve 批下级请求 / own 改组结构+批永久改。
--        「任何人都不直接改数据」由 B8 审批流落地，本表只判能不能看/进组。
--     4. UNIQUE(org_id, group_id, user_id)：一人一组一行，再授不同档 =
--        覆盖（upsert），不并存。多路径取 max 来自对象图 DAG，不来自本表多行。
--     5. 不做负权限 deny（docs/06 §1：DAG 多父下语义不自洽）——收回 = 删行。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS group_roles (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    group_id    TEXT NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    level       TEXT NOT NULL CHECK (level IN ('view', 'propose', 'approve', 'own')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_group_roles_user ON group_roles(user_id);
CREATE INDEX IF NOT EXISTS idx_group_roles_group ON group_roles(group_id);

-- ---------------------------------------------------------------------------
-- S14 埋点事件表（docs/08-试用组织数据口径.md 是字段口径唯一来源）
-- ---------------------------------------------------------------------------
-- 只出聚合数、永不外发；trial_code 为稳定短码（人数去重）、session_id 每会话随机
-- （时长）；payload 只放结构化参数，禁止姓名/班级名/课表内容；org_kind 隔离演示点击。
CREATE TABLE IF NOT EXISTS event_log (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,             -- 秒级 ISO 时间
    trial_code  TEXT NOT NULL DEFAULT '',  -- 稳定短码 T01..；demo/未匹配=''
    session_id  TEXT NOT NULL,             -- 前端每会话随机串
    event       TEXT NOT NULL,             -- task_start/task_success/task_abandon/survey
    payload     TEXT NOT NULL DEFAULT '{}',-- JSON 字符串（结构化参数）
    org_id      TEXT NOT NULL,
    org_kind    TEXT NOT NULL CHECK (org_kind IN ('trial', 'demo')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_event_log_org ON event_log(org_id, org_kind);
CREATE INDEX IF NOT EXISTS idx_event_log_session ON event_log(session_id);

-- ---------------------------------------------------------------------------
-- S17 需求登记表（docs/07 §7：前端只传原话、后端解析、原话入库）
-- ---------------------------------------------------------------------------
-- 🔴 raw_text 原样存储（契约 §7.3）：解析错了可追溯、可人工纠正。
-- parsed 存解析结果 JSON（结构化，仅供追溯/人工纠正，不替代 raw_text）。
CREATE TABLE IF NOT EXISTS demands (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    group_id    TEXT REFERENCES objects(id) ON DELETE SET NULL,
    user_id     TEXT REFERENCES users(id) ON DELETE SET NULL,
    raw_text    TEXT NOT NULL,             -- 用户原话，原样保留
    hard        INTEGER NOT NULL DEFAULT 0, -- 1=硬需求（必须满足） 0=软需求（尽量安排）
    parsed      TEXT NOT NULL DEFAULT '{}', -- 解析结果 JSON 字符串
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_demands_org ON demands(org_id, created_at);

-- ---------------------------------------------------------------------------
-- ===========================================================================
-- ① P0-2 调休 / 临时微调例外表--
-- 与 calendar_exceptions 的分工（两表语义不同，不是替代关系）：
--   「放假」是日历事实（该日全校停课）→ 老表，只服务 kind='holiday'；
--   「调课」是临时调整（该日改按别的课表上）→ 本表。
--
-- 允许一天多行（scope='schedule' 时单条日程 × 单日可有多条例外），
-- 故不设 UNIQUE(org_id, date)——这是与 docs/12 原稿唯一的结构差异（P0-2 卡面要求）。
--
-- mode 语义（P0-2 仅实现 as_of；cancel/move/special 留给 B9）：
--   as_of   = 调休补课：该日按 as_of_dow（1..7，与 period_slots.day_of_week 同口径）
--             的课表展开。🔴 as_of_dow 与 mode='as_of' 必须成对出现，
--             应用层（occupancy.load_exceptions）对 NULL 抛错、不静默。
--   cancel  = 临时取消（B9）：该日不展开 → 槽位释放
--   move    = 临时改点（B9）：该日改用 new_mask（十六进制）
--   special = 换作息（二期）：该日用 set_id 这套作息
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schedule_exceptions (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    scope       TEXT NOT NULL CHECK (scope IN ('day', 'schedule')),
    -- day      : 整天级（调休补课）→ 对应 P0-2
    -- schedule : 单条周期日程 × 单日（临时取消/改点）→ 对应 B9
    target_id   TEXT,              -- scope='schedule' 时为 schedule_id；scope='day' 时 NULL
    date        TEXT NOT NULL,     -- 生效日期（ISO 'YYYY-MM-DD'）
    mode        TEXT NOT NULL CHECK (mode IN ('cancel', 'move', 'as_of', 'special')),
    as_of_dow   INTEGER CHECK (as_of_dow IS NULL OR as_of_dow BETWEEN 1 AND 7),
    new_mask    TEXT,              -- mode='move'：该日改用这个掩码（十六进制）
    set_id      TEXT,              -- mode='special'：这天用哪套作息
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sched_exc_lookup
    ON schedule_exceptions(org_id, date, scope);

-- 🔴 合并说明（2026-10-13）：本处原有两块内容，均为 schema.sql **末尾追加**，
-- 互不重叠，合并时**两块都保留**、顺序按「先 P0-2 后 S18」。
-- ===========================================================================
-- ② S18 需求「调整幅度」档位-- ---------------------------------------------------------------------------
-- 词汇与 /api/resolve 的 mode 同源（docs/07 §6）：
--   minimal  = 默认，行为与没有此字段时完全一致
--   balanced = 中等幅度
--   optimal  = 最大幅度
--
-- 🔴 为什么是旁表而不是 ALTER demands ADD COLUMN level（2026-10-13 实测结论）：
-- ALTER 非幂等，而 init_db 的契约是「可重复调用」（db.py docstring：全部
-- IF NOT EXISTS）——seeds.seed_demo 内部会二次 init_db，schema.sql 里放
-- ALTER 会让所有「init_db + seed_demo」调用链报
-- sqlite3.OperationalError: duplicate column name（实测 5 个既有测试 ERROR）。
-- 旁表 CREATE IF NOT EXISTS 幂等，新库旧库同一套 DDL，无漂移。
--
-- 🔴 旧库兜底（第 7 处静默失效防线）：ensure_database 只在库文件缺失时
-- seed，已存在的 data/*.db（trial.db / demo.db）不会补新表 →
-- 由 b5_kernels.ensure_demands 查 sqlite_master，缺则 init_db 补齐。
--
-- 读法（旧数据没有旁表行 → 默认 minimal，绝不静默给错值）：
--   SELECT COALESCE(dl.level, 'minimal') FROM demands d
--   LEFT JOIN demand_levels dl ON dl.demand_id = d.id
CREATE TABLE IF NOT EXISTS demand_levels (
    demand_id    TEXT PRIMARY KEY REFERENCES demands(id) ON DELETE CASCADE,
    level        TEXT NOT NULL DEFAULT 'minimal'
                 CHECK (level IN ('minimal', 'balanced', 'optimal')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===========================================================================
-- B8 审批流（docs/06 §3/§4 冻结）：change_requests + change_events
-- 🔴 CREATE TABLE IF NOT EXISTS 而非 ALTER：ALTER 非幂等会炸 init_db 二次调用
-- （demand_levels 旁表教训，见本文件 line 298）。
-- 🔴 旧库兜底（第 8 处静默失效防线）：ensure_database 只在库文件缺失时 seed，
-- 已存在的 data/*.db（trial.db / demo.db）不会补新表 →
-- 由 b8_kernels.ensure_change_tables 查 sqlite_master，缺则 init_db 补齐。
CREATE TABLE IF NOT EXISTS change_requests (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    requester_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('once','permanent')),
    op TEXT NOT NULL CHECK (op IN ('create','update','delete')),
    target_type TEXT NOT NULL CHECK (target_type IN ('schedule','group','member')),
    target_id TEXT,
    payload_json TEXT NOT NULL,          -- {before:{...}, after:{...}}
    reason TEXT,
    status TEXT NOT NULL CHECK (status IN
        ('draft','pending','approved','rejected','withdrawn','applied')),
    approver_id TEXT,
    reminder_due TEXT,                -- §5 超时提醒本卡不实现，字段先留
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cr_org_status ON change_requests(org_id, status);

CREATE TABLE IF NOT EXISTS change_events (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,              -- submit/approve/reject/withdraw/apply/fail
    at TEXT NOT NULL DEFAULT (datetime('now')),
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ce_request ON change_events(request_id);

-- ===========================================================================
-- ③ 作息方案（B-1 夏冬作息 · AGENTS.md §3 例外 ⑦）
-- ---------------------------------------------------------------------------
-- 为什么是两张**新表**而不是改 period_slots：
--   period_slots 的 UNIQUE(org_id, day_of_week, idx)（本文件 :94）决定了一个
--   组织只能有一套**默认**节次时间。多套作息（夏季/冬季/考试周）必须并行走
--   本处的新表，period_slots 保持「默认方案」地位不变。
--
-- 为什么位图不用改、历史数据不会坏（`slots.py:24-26`）：
--   MAX_PERIODS_PER_DAY = 16 是**上界**，位号 = (周几-1)*16 + (节次-1) 固定，
--   → 第 k 节永远是第 k 位。夏 8 节 / 冬 7 节 / 考试周 6 节全在同一个 112 位
--   掩码里，「钟点平移」「取消第 8 节」「只上上午 4 节」是同一个模型。
--
-- 🔴 set_slots 语义（定死，不许含糊）：
--   · 展开时**只认 enabled = 1** 的行；
--   · enabled = 0 与**缺行完全等价** —— 都表示「该方案这一天没有这一节」；
--   · idx 上界 = slots.MAX_PERIODS_PER_DAY（16），不是 8 也不是 12。
--
-- 绑定「哪天用哪套」**不新建表**：直接用 schedule_exceptions
--   (scope='day', mode='special', date='YYYY-MM-DD', set_id='set_winter')
--   读路径在 occupancy.load_exceptions → expand_periodic。
--
-- 旧库兜底（第 9 处静默失效防线）：ensure_database 只在库文件缺失时 seed，
-- 已存在的 data/*.db 不会补新表 → occupancy.ensure_period_set_tables 查
-- sqlite_master，缺则 init_db（与 b8_kernels.ensure_change_tables 同范式）。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS period_sets (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,                       -- 「冬季作息」「考试周」
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_period_sets_org ON period_sets(org_id);

CREATE TABLE IF NOT EXISTS set_slots (
    id          TEXT PRIMARY KEY,
    set_id      TEXT NOT NULL REFERENCES period_sets(id) ON DELETE CASCADE,
    day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
    idx         INTEGER NOT NULL CHECK (idx >= 1 AND idx <= 16),
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    UNIQUE (set_id, day_of_week, idx)
);

-- ===========================================================================
-- 例外 ⑧（队长 2026-11-10 批准，卡 rvdL9a）：消解闭环快照
-- ===========================================================================
-- 用途：`POST /api/resolve` 是**原地 UPDATE**（m2.apply_plan 直接改
--   schedule_periodic.slot_bitmap / schedule_once.start_at、end_at），
--   点一次就不可逆。本表在写入前存一份**全量**快照，支撑：
--     · revert  → 按快照逐字段回写，回到应用前
--     · commit  → 固化为正式版本（此后再 revert 一律 409）
--
-- 🔴 为什么存全量而不是增量：
--   消解是迭代过程（max_rounds=24），每一轮改哪几条依赖当时的占用状态，
--   事前算不出改动集合；组织级日程只有几十条，全量快照仅几 KB。
--   「简单且不会漏」优先于「省几 KB」——漏一条就是撤销不干净。
--
-- 🔴 payload 结构（JSON 文本）：
--   {"schedules":[{"id":"sch_x","kind":"periodic","bitmap":"ff0f",
--                  "start_at":null,"end_at":null},
--                 {"id":"sch_y","kind":"once","bitmap":null,
--                  "start_at":"2026-11-11T09:30","end_at":"2026-11-11T10:10"}],
--    "captured_at":"2026-11-11T09:30:00"}
--   `kind` 决定回写哪张表：周期日程写 schedule_periodic.slot_bitmap，
--   一次性活动写 schedule_once.start_at/end_at。
--   （⚠️ 卡面写的 `schedules.start_at` 是笔误：schedules 主表只有 id/org_id/
--     kind/title/created_by/created_at/updated_at，起止时刻在 schedule_once。）
--
-- 🔴 status 三态用 CHECK 锁死，不允许第四种或 NULL：
--   「这版到底是临时还是定稿」不能靠人记。
--
-- 旧库兜底（第 10 处静默失效防线）：ensure_database 只在库缺失时 seed，
-- 已存在的 data/*.db 不会补新表 → b4_kernels.ensure_snapshot_table()
-- 查 sqlite_master，缺则 init_db（与 occupancy.ensure_period_set_tables 同范式）。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS resolve_snapshots (
    id               TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_by       TEXT NOT NULL,              -- users.id
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    status           TEXT NOT NULL CHECK (status IN ('applied', 'reverted', 'committed')),
    payload          TEXT NOT NULL,              -- JSON 全量快照
    applied_count    INTEGER,
    remaining_before INTEGER,
    remaining_after  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_org ON resolve_snapshots(org_id, created_at);

-- ===========================================================================
-- 例外 ⑨（队长 2026-11-13 批准，卡 rTVvb8）：组织公告
--
-- 业务口径：每个组织**恒一条**公告。
--   发布 = upsert（有则 UPDATE 同一 id，无则 INSERT，并把 active 置 1）
--   停用 = 软删（active=0，不删行）；GET 只返回 active=1 的那条。
--
-- 🔴 为什么 UNIQUE (org_id)
--   1) 「每组织一条」是本卡的业务不变量，写进约束而不是靠每个 handler 自觉；
--   2) 它是 `INSERT ... ON CONFLICT(org_id) DO UPDATE` 的冲突目标；
--   3) 软删保留行 → 唯一键始终成立 → 停用后重新发布复用同一 id，
--      不会出现「两条 active=1、GET 取哪条看运气」的歧义；
--   4) UNIQUE 自带索引，不再额外建 idx_ann_org（每组织只有一行）。
--
-- 🔴 时间列用 UTC `datetime('now')`：与全库既有表同口径；服务器换时区 /
--   夏令时不影响排序与审计。接口原样返回，前端标 UTC，⛔ 不在前端解析时区
--   （`new Date('YYYY-MM-DD HH:MM:SS')` 各浏览器按本地时区解释，会显示偏移）。
--
-- 🔴 updated_by 存 users.id，不存 objects.id
--   1) 写入口拿到的 context["user_id"] 就是 users.id，authz.group_roles.user_id
--      也是 users.id —— 存同一主体，写路径不用再做 object → user 反查；
--   2) 与 resolve_snapshots.created_by 的既有口径一致；
--   3) schedules.created_by 存 objects.id 是「归属」语义；本列是「谁改的」
--      审计语义，两类语义不混用。
--   ⛔ 不加 REFERENCES users(id)：账号被删时公告不能跟着消失；
--      JOIN 不到时接口回退显示原始 id。
--
-- 旧库兜底（第 11 处静默失效防线）：ensure_database 只在库文件缺失时 seed，
-- 已存在的 data/demo.db、data/trial.db 不会自动补表 →
-- b_announcement_kernels.ensure_announcements() 查 sqlite_master，缺则 init_db
-- （与 b4_kernels.ensure_snapshot_table 同范式）。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS announcements (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    title       TEXT NOT NULL CHECK (length(trim(title)) > 0),
    body_md     TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    updated_by  TEXT NOT NULL,                  -- users.id（不是 objects.id）
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id)
);
