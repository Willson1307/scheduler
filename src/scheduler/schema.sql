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
