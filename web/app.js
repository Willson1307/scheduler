/* =============================================================================
 * 课表活动调度辅助系统 · 前端（F1 骨架）
 * -----------------------------------------------------------------------------
 * 归属：web/**（前端独占）。🔴 本文件不碰 src/**、tests/**、scripts/**、static.py。
 *
 * 本文件回答答辩时最容易被追问的三件事：
 *
 * 1)「为什么不用框架、不用 CDN？」
 *    现场必须断网能跑（赛事章程硬要求）。任何外链在断网时都会崩，
 *    所以只用原生 HTML/CSS/JS + 相对路径，零构建、零第三方依赖。
 *
 * 2)「前端会不会又踩掩码位序的坑？」（F7 起，见 dayMaskPeriods）
 *    不会。历史 bug 的根因是「周位序 vs 单日掩码」两套位序混用、整页错一位
 *    且不报错。而 /api/schedules 的 grid 值**已是后端归一化后的单日 1-based
 *    掩码**（period N → bit N；后端 api_schedules 已执行
 *    day_mask_hex(m2.day_slots_mask(decode_mask(mask)))）。前端只在唯一一个
 *    受控辅助函数 dayMaskPeriods() 里做 `v & (1 << p)` 反解节次，**绝不**碰
 *    周位序、不做字符串比较、不自己拼 hex。要课程名/可读节次仍由后端在
 *    readable 里给（契约 §5），本文件不另行解码。
 *
 * 3)「23 这个数字写在哪？」
 *    哪里都不写。count / raw_count / remaining_before / remaining_after
 *    全部从响应里读（契约 §5、§6）。前端没有任何一处硬编码冲突数，
 *    也不拿它做判断——换一组数据，界面照样正确。
 *
 * 数据来源开关：USE_MOCK。true＝读下方 mock（结构与 docs/07 契约 v1.0 逐字段一致），
 * false＝打真实接口。交互代码一行都不用动——这是本文件 mock 与真实响应结构
 * 必须严格对齐的原因。
 *
 * F3b（2026-10-10）切到真实接口时新增的唯一一件事：登录。
 * B3/B4 接入鉴权后（P0 r13kE9），未登录一律 401（docs/07 通用约定），所以取数前
 * 必须先 POST /api/login 拿 token，之后所有请求带 Authorization: Bearer <token>。
 * 登录栏只在真实接口模式显示；token 只存内存不落盘（演示系统最小实现，刷新需重登）。
 * ========================================================================== */
'use strict';

/* =============================================================================
 * 0. 数据来源开关
 * ========================================================================== */
const USE_MOCK = false;   // F3b：真实接口（mock 分支保留，供离线兜底演示随时切回）

/* =============================================================================
 * 1. Mock 数据（严格对齐 docs/07-API契约.md v1.0 的响应结构）
 * -----------------------------------------------------------------------------
 * 对照条目：
 *   §2  GET  /api/groups    → [{group_id,name,level,member_count,parent_name}]
 *   §3  GET  /api/objects   → [{id,kind,name,parent_ids}]
 *   §4  GET  /api/schedules → {objects,days,grid}，grid 键= "<object_id>|<date>"
 *   §5  GET  /api/conflicts → {count,raw_count,items[{a,b,date,slots,readable}]}
 *   §6  POST /api/resolve   → {applied,remaining_before,remaining_after,rounds,
 *                             filtered_out_count,termination,snapshot_id}
 *       POST /api/resolve/revert?id=  → {reverted,snapshot_id,conflicts_after}
 *       POST /api/resolve/commit?id=  → {committed,snapshot_id}（仅 admin/own）
 *       GET  /api/resolve/snapshots   → {snapshots:[…最近 20 条…]}
 *
 * 🔴 count 与 raw_count 语义不同，切勿混用：
 *    count    = 聚合后的冲突**组**数（演示库 3）
 *    raw_count= 聚合前的原始冲突**条**数（演示库 23）
 *    界面上「共消解 N 处冲突」用 raw_count；「有 N 组冲突待处理」用 count。
 * ========================================================================== */

/** 演示库学期第 1 周（周一=2026-09-07）。真实环境由 /api/schedules 的 days 决定。 */
const MOCK_DAYS = ['2026-09-07', '2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11'];

/** 周三=2026-09-09：演示库的冲突全部密集落在这一天（docs/17 §1.2）。 */
const MOCK_CONFLICT_DATE = '2026-09-09';

/**
 * 组列表（契约 §2）。🔴 只列有授权的组；未授权的组根本不列出（不是灰掉）。
 * member_count = 该组后代中 kind='user' 的人数（api_groups.list_groups 的口径），
 * 所以「全校」是 1 管理员 + 9 教师 + 30 学生 = 40，班级各10。
 * parent_name = 最近父组（depth=1 且为 group），根组为 null。
 */
const MOCK_GROUPS = [
  { group_id: 'g_all',  name: '全校',      level: 'own',     member_count: 40, parent_name: null },
  { group_id: 'g_c1',   name: '高一(1)班', level: 'own',     member_count: 10, parent_name: '全校' },
  { group_id: 'g_c3',   name: '高一(3)班', level: 'view',    member_count: 10, parent_name: '全校' },
];

/**
 * 对象清单。构成与演示库逐项对齐，合计 **48 个**
 * （与 README / 报名材料 / scripts/reset_demo.py 的「对象数：48」一致，
 *   答辩现场数字必须对得上）：
 *   根组 1 + 管理员 1 + 班级 3 + 学生 3×10=30 + 教师 9 + 教室 4 = 48
 *
 * ⚠️ 假设说明：现场演示脚本 §3.2 的动线提到「切到另一个组（班级/社团）」以体现
 *    多父模型，但 reset_demo 走的 seed_demo() 并未创建社团对象。故mock 不凭空
 *    造社团——造出来会让「48」对不上。社团/多父由后端真实数据决定，
 *    前端不内置任何对象或层级（契约 §12.2 第4 条）。
 */
const MOCK_OBJECTS = (function buildObjects() {
  const list = [];
  list.push({ id: 'g_all',  kind: 'group', name: '全校', parent_ids: [] });
  list.push({ id: 'u_admin', kind: 'user', name: '系统管理员', parent_ids: ['g_all'] });
  const classIds = ['g_c1', 'g_c2', 'g_c3'];
  const classNames = ['高一(1)班', '高一(2)班', '高一(3)班'];
  classIds.forEach(function (cid, i) {
    list.push({ id: cid, kind: 'group', name: classNames[i], parent_ids: ['g_all'] });
  });
  // 9 名教师（挂在根组下，与 seed_demo 一致）
  for (let k = 1; k <= 9; k++) {
    list.push({
      id: 'u_t' + String(k).padStart(2, '0'),
      kind: 'user',
      name: '教师' + String(k).padStart(2, '0'),
      parent_ids: ['g_all'],
    });
  }
  // 30 名学生（每班 10 名—— students_per_class 必须为 10，否则冲突数与材料对不上）
  for (let c = 1; c <= 3; c++) {
    for (let s = 1; s <= 10; s++) {
      list.push({
        id: 'u_s' + c + String(s).padStart(2, '0'),
        kind: 'user',
        name: '学生' + c + '-' + String(s).padStart(2, '0'),
        parent_ids: ['g_c' + c],
      });
    }
  }
  // 4 间教室（kind=resource，契约 §3 三值之一）
  for (let r = 1; r <= 4; r++) {
    list.push({
      id: 'r_' + String(r).padStart(2, '0'),
      kind: 'resource',
      name: '教室' + String(r).padStart(2, '0'),
      parent_ids: ['g_all'],
    });
  }
  return list;
})();

/**
 * 掩码字面量池。
 * 🔴 这里**故意**只放写好的十六进制串、不做任何按位计算——
 * 让「前端不碰位运算」这条约束在代码里一眼可查（答辩时可直接指认）。
 * 真实响应里这些串由后端 slots.mask_to_hex() / api_resolve._slots_hex() 产出，
 * 长度不定（AGENTS §9b），前端照单透传。
 */
const MOCK_MASKS = ['0x0006', '0x000a', '0x000c', '0x0102', '0x0201', '0x0000', '0x0018'];

/**
 * 某个对象某天的占用掩码（仅供 mock 造数据）。
 * 🔴🔴 本函数是**唯一**出现过按位运算的地方，且只用于「把字符串散列成一个下标」，
 *    绝不参与掩码的解析或比较——它连掩码本身都还没碰到。
 *    之所以连这一行也尽量写短：答辩时我们要能指着整个文件说「前端不解码掩码」，
 *    任何 >> 都会让人误以为这里在算位。
 *    等价写法（不用位运算）：h = (h * 31 + code) % 1000003。
 */
function mockMask(objectId, date) {
  const s = objectId + '|' + date;
  let h = 0;
  for (let i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) % 1000003; }
  return MOCK_MASKS[h % MOCK_MASKS.length];
}

/**
 * 演示库冲突（契约 §5 结构，字段给全）。
 * 口径：聚合后 **3 组**；原始冲突 **23 处** = 教师冲突 1 + 高一(1)班学生 11 + 高一(2)班学生 11。
 * 这两数字写在数据里、由 readCounts 读出，前端代码里不做任何 23/3 的判断。
 * 全部落在 2026-09-09（周三）——演示库的学期起点固定为 2026-09-07（docs/17 §1.2）。
 */
const MOCK_CONFLICT_ITEMS = [
  // 组 1：教师冲突——同一位教师同一节被排进两个班（seed_demo 故意制造的撞车）
  {
    a: {
      schedule_id: 'sch_phy_c1', object_id: 'u_t04', object_name: '教师04', object_kind: 'user',
      title: '高一(1)班 物理', date: MOCK_CONFLICT_DATE, slots: '0x0002',
      readable: [{ dow: 3, period: 1 }],
    },
    b: {
      schedule_id: 'sch_phy_c2', object_id: 'u_t04', object_name: '教师04', object_kind: 'user',
      title: '高一(2)班 物理', date: MOCK_CONFLICT_DATE, slots: '0x0002',
      readable: [{ dow: 3, period: 1 }],
    },
    date: MOCK_CONFLICT_DATE, slots: '0x0002', readable: [{ dow: 3, period: 1 }],
  },
  // 组 2：高一(1)班 学生冲突——课表（周期）与全年级大会（一次性）同框撞车
  {
    a: {
      schedule_id: 'sch_phy_c1', object_id: 'g_c1', object_name: '高一(1)班', object_kind: 'group',
      title: '高一(1)班 物理', date: MOCK_CONFLICT_DATE, slots: '0x0002',
      readable: [{ dow: 3, period: 1 }],
    },
    b: {
      schedule_id: 'sch_evt_all', object_id: 'g_all', object_name: '全校', object_kind: 'group',
      title: '全年级大会', date: MOCK_CONFLICT_DATE, slots: '0x0006',
      readable: [{ dow: 3, period: 1 }, { dow: 3, period: 2 }],
    },
    date: MOCK_CONFLICT_DATE, slots: '0x0002', readable: [{ dow: 3, period: 1 }],
  },
  // 组 3：高一(2)班 学生冲突——同上
  {
    a: {
      schedule_id: 'sch_phy_c2', object_id: 'g_c2', object_name: '高一(2)班', object_kind: 'group',
      title: '高一(2)班 物理', date: MOCK_CONFLICT_DATE, slots: '0x0002',
      readable: [{ dow: 3, period: 1 }],
    },
    b: {
      schedule_id: 'sch_evt_all', object_id: 'g_all', object_name: '全校', object_kind: 'group',
      title: '全年级大会', date: MOCK_CONFLICT_DATE, slots: '0x0006',
      readable: [{ dow: 3, period: 1 }, { dow: 3, period: 2 }],
    },
    date: MOCK_CONFLICT_DATE, slots: '0x0002', readable: [{ dow: 3, period: 1 }],
  },
];

/** mock 运行期状态：消解后冲突归零，用来演示「前后对比」。 */
const mockState = { resolved: false };

/**
 * 按组过滤 mock 冲突（真实接口由服务端按 group 过滤）。
 *
 * 🔴 raw_count 用「数据表」而不是「算式」：演示库口径是
 *    全校 = 教师冲突 1 + 高一(1)班学生 11 + 高一(2)班学生 11 = 23 处原始冲突。
 *    写成下面这张表而不是 `length === 3 ? 23 : ...`，是为了让「23」只作为
 *    **数据**出现一次，且不与任何条件判断绑死——换一组数据只需改这张表。
 */
const MOCK_RAW_COUNT_BY_GROUP = {
  g_all: 23,   // 1 + 11 + 11
  g_c1: 12,    // 教师 1 + 本班学生 11
  g_c2: 12,
  g_c3: 0,     // 本周无冲突
};

function mockConflictsFor(groupId) {
  if (mockState.resolved) {
    return { count: 0, raw_count: 0, items: [] };
  }
  let items = MOCK_CONFLICT_ITEMS;
  // 班级视角只看得到与自己相关的条目；组级（全校）看得到全部。
  // 高一(3)班本周无冲突 —— 用来演示「切组后冲突数变化」，也顺带证明
  // 页面不是写死一个数字。
  if (groupId === 'g_c1') items = [MOCK_CONFLICT_ITEMS[0], MOCK_CONFLICT_ITEMS[1]];
  else if (groupId === 'g_c2') items = [MOCK_CONFLICT_ITEMS[0], MOCK_CONFLICT_ITEMS[2]];
  else if (groupId === 'g_c3') items = [];
  const raw = MOCK_RAW_COUNT_BY_GROUP[groupId];
  return {
    count: items.length,
    // 未知组 → raw_count 缺省为 null，前端会显示「接口未提供」而不是编一个数
    raw_count: raw === undefined ? null : raw,
    items: items,
  };
}

/** mock 路由：路径 → 响应体。结构与契约逐字段一致。 */
function mockRoute(pathname, method) {
  if (pathname === '/api/groups' && method === 'GET') {
    return { status: 200, body: MOCK_GROUPS };
  }
  if (pathname === '/api/objects' && method === 'GET') {
    return { status: 200, body: MOCK_OBJECTS };
  }
  if (pathname === '/api/schedules' && method === 'GET') {
    const grid = {};
    if (!mockState.resolved) {
      MOCK_OBJECTS.forEach(function (o) {
        if (o.kind !== 'group') return;
        MOCK_DAYS.forEach(function (d) {
          grid[o.id + '|' + d] = mockMask(o.id, d);
        });
      });
    }
    return {
      status: 200,
      body: {
        objects: MOCK_OBJECTS,
        days: MOCK_DAYS.slice(),
        grid: grid,
      },
    };
  }
  if (pathname === '/api/conflicts' && method === 'GET') {
    //🔴 注意是 currentGroupId() 带括号——取当前组 id 的值。
    //    漏括号等于把函数本身传进去，groupId 恒为 undefined → 永远返回全校数据，
    //    表现为「切组后冲突数不变」，且不报错（与本项目吃过的那类静默 bug 同类）。
    return { status: 200, body: mockConflictsFor(currentGroupId()) };
  }
  if (pathname === '/api/resolve' && method === 'POST') {
    const before = mockConflictsFor(currentGroupId());
    mockState.resolved = true;
    return {
      status: 200,
      body: {
        applied: before.count ? 2 : 0,
        remaining_before: before.raw_count,
        remaining_after: 0,
        rounds: 3,
        filtered_out_count: 0,
        termination: 'ok',
        // 闭环（卡 rvdL9a）：mock 也给 snapshot_id，让撤销/固化按钮在离线兜底时可用
        snapshot_id: 'snap_mock',
      },
    };
  }
  if (pathname === '/api/resolve/revert' && method === 'POST') {
    mockState.resolved = false;
    const c = mockConflictsFor(currentGroupId());
    return {
      status: 200,
      body: {
        reverted: true, snapshot_id: 'snap_mock', conflicts_after: c.raw_count,
        already_reverted: false, restored: 2, missing: 0,
      },
    };
  }
  if (pathname === '/api/resolve/commit' && method === 'POST') {
    return { status: 200, body: { committed: true, snapshot_id: 'snap_mock' } };
  }
  if (pathname === '/api/resolve/snapshots' && method === 'GET') {
    return { status: 200, body: { snapshots: [] } };
  }
  return { status: 404, body: { error: { code: 'not_found', message: 'mock 未覆盖该路径：' + pathname } } };
}

/* =============================================================================
 * 2. 接口访问层
 * -----------------------------------------------------------------------------
 * 🔴 零容忍空白页：任何失败（404/500/网络错误/结构不对）都必须变成一条
 *    明确的错误提示，绝不静默返回空数据让人误以为「没有冲突」。
 *    契约 §通用约定：错误体形如 {"error":{"code","message"}}；
 *    但 B1 的 ApiError 产出的是扁平 {"error":"..."}——两种都兼容读。
 * ========================================================================== */

function currentGroupId() { return state.groupId; }

/** F3b：真实接口统一带 Bearer token（docs/07 通用约定）；mock 模式无 token，不加。 */
function authHeaders(base) {
  const h = base || {};
  if (state.token) h['Authorization'] = 'Bearer ' + state.token;
  return h;
}

/**
 * 401 统一处理：清 token、弹出登录栏、给出可读错误。
 * 🔴 绝不静默当成「空数据」——那会把「没登录」伪装成「没有冲突」。
 */
function handle401(body) {
  state.token = '';
  state.user = null;
  showLogin('登录已过期或未登录（HTTP 401），请重新登录。');
  throw new Error(extractMessage(body) || 'HTTP 401 未登录');
}

/** 取查询串里的 group（可空＝隐式模式，走服务端默认）。 */
function withGroup(params) {
  const p = params || {};
  if (state.groupId) p.group = state.groupId;
  return p;
}

async function apiGet(pathname, params) {
  if (USE_MOCK) {
    const r = mockRoute(pathname, 'GET');
    if (r.status !== 200) throw new Error(extractMessage(r.body) || ('HTTP ' + r.status));
    return r.body;
  }
  const url = new URL(pathname, window.location.origin);
  Object.keys(params || {}).forEach(function (k) {
    if (params[k] !== '' && params[k] != null) url.searchParams.set(k, params[k]);
  });
  const res = await fetch(url.toString(), {
    method: 'GET',
    headers: authHeaders({ Accept: 'application/json' }),
  });
  const body = await readBody(res);
  if (!res.ok) {
    if (res.status === 401) handle401(body);   // handle401 内部必 throw
    const err = new Error(extractMessage(body) || ('HTTP ' + res.status + ' ' + res.statusText));
    err.status = res.status;   // F7：审批动作要能区分 403/404/409，如实提示
    throw err;
  }
  return body;
}

async function apiPost(pathname, params, payload) {
  if (USE_MOCK) {
    const r = mockRoute(pathname, 'POST');
    if (r.status !== 200) throw new Error(extractMessage(r.body) || ('HTTP ' + r.status));
    return r.body;
  }
  const url = new URL(pathname, window.location.origin);
  Object.keys(params || {}).forEach(function (k) {
    if (params[k] !== '' && params[k] != null) url.searchParams.set(k, params[k]);
  });
  const res = await fetch(url.toString(), {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json', Accept: 'application/json' }),
    body: JSON.stringify(payload || {}),
  });
  const body = await readBody(res);
  if (!res.ok) {
    if (res.status === 401) handle401(body);   // handle401 内部必 throw
    const err = new Error(extractMessage(body) || ('HTTP ' + res.status + ' ' + res.statusText));
    err.status = res.status;   // F7：审批动作要能区分 403/404/409，如实提示
    throw err;
  }
  return body;
}

/** 读响应体：后端可能返回 JSON，也可能返回 HTML 错误页，两种都要给出可读信息。 */
async function readBody(res) {
  const text = await res.text();
  if (!text) return null;
  try { return JSON.parse(text); } catch (e) {
    throw new Error('响应不是合法 JSON（HTTP ' + res.status + '）：' + text.slice(0, 200));
  }
}

/** 兼容嵌套 {"error":{"code","message"}} 与扁平 {"error":"..."} 两种错误体。 */
function extractMessage(body) {
  if (!body || typeof body !== 'object') return null;
  const e = body.error;
  if (typeof e === 'string') return e;
  if (e && typeof e === 'object' && typeof e.message === 'string') return e.message;
  return null;
}

/* =============================================================================
 * 2b. 埋点（S14：POST /api/ping）
 * -----------------------------------------------------------------------------
 * 🔴 事件三元组缺一不可：task_start（完成率分母）/ task_success（分子）/ task_abandon。
 *    缺 task_start 就永远算不出任务完成率的分母——这是赛事必交的四类数据之一
 *    （docs/08 §3.2）。
 * 🔴 合规：payload 只放结构化参数（数值 / 枚举），严禁姓名、班级名、登录名
 *    （docs/08 §6）。trial_code 与 org_kind 由后端从登录名推导，前端绝不传。
 *    这里传的 group 是组 ID（如 g_c1），不是组名。
 * fire-and-forget：打点失败静默，绝不阻断主流程（埋点是观测，不是业务）。
 * ========================================================================== */

/** 会话级随机串（每次打开页面生成一个，docs/08 §3.1，供后端算平均使用时长）。 */
function newSessionId() {
  if (window.crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return 's-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

/**
 * 上报一条埋点事件。返回 Promise 但不 await（调用方不必等它）。
 * 🔴 三种情况不打点：mock 模式（无后端）、未登录（后端要求鉴权 401）。
 */
function ping(event, payload) {
  if (USE_MOCK) return Promise.resolve();
  if (!state.token) return Promise.resolve();
  const body = { event: event, session_id: state.sessionId, payload: payload || {} };
  if (state.groupId) body.group = state.groupId;
  return fetch('/api/ping', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json', Accept: 'application/json' }),
    body: JSON.stringify(body),
    keepalive: true,   // 关页时 pagehide 里的上报也能送达
  }).catch(function () { /* 打点失败静默：埋点不得影响主流程 */ });
}

/** task_start 已发、尚未 success/abandon 时进入「进行中」，关页或超时要补 abandon。 */
function markTaskActive() {
  state.taskActive = true;
  state.taskStartedAt = Date.now();
  clearTimeout(state.taskTimer);
  // 90 秒无操作兜底 → task_abandon（docs/08 §3.2 的「超时」触发，说明剩下的去哪了）
  state.taskTimer = setTimeout(function () {
    if (state.taskActive) {
      state.taskActive = false;
      ping('task_abandon', { reason: 'timeout' });
    }
  }, 90000);
}

/** 任务已出结果（成功或出错都算有结论），清「进行中」标记与超时定时器。 */
function markTaskDone() {
  state.taskActive = false;
  clearTimeout(state.taskTimer);
}

/* =============================================================================
 * 3. 宽容解析层（契约 §12.2 五条）
 * -----------------------------------------------------------------------------
 * ① 未知字段 → 忽略（本文件从不校验 schema，从不因多出字段抛错）
 * ② 未知枚举 → 降级显示，不空白不崩
 * ③ 数组长度 → 一律读 .length，不写死 3 / 5 / 10
 * ④ 不内置校验白名单（如不因 kind 陌生就不渲染）
 * ⑤ 掩码 → 只透传
 * ========================================================================== */

/** 取字符串字段，缺失/类型不符返回 fallback（宽容，不抛错）。 */
function str(obj, key, fallback) {
  if (obj && typeof obj === 'object') {
    const v = obj[key];
    if (typeof v === 'string' && v !== '') return v;
    if (typeof v === 'number' && isFinite(v)) return String(v);
  }
  return fallback === undefined ? '' : fallback;
}

/** 取整数，缺失/非数返回 null（调用方决定怎么降级，绝不猜）。 */
function intOrNull(obj, key) {
  if (obj && typeof obj === 'object') {
    const v = obj[key];
    if (typeof v === 'number' && isFinite(v)) return Math.trunc(v);
    if (typeof v === 'string' && /^-?\d+$/.test(v)) return parseInt(v, 10);
  }
  return null;
}

/** 取数组，非数组返回空数组（不崩）。长度由调用方读 .length。 */
function arr(obj, key) {
  if (obj && typeof obj === 'object' && Array.isArray(obj[key])) return obj[key];
  return [];
}

/** readable 里读星期几。契约 §5 两处示例字段名不一致（day / dow），两种都读。 */
function readableDow(r) {
  if (!r || typeof r !== 'object') return null;
  const v = (r.dow !== undefined && r.dow !== null) ? r.dow : r.day;
  return typeof v === 'number' && isFinite(v) ? v : null;
}

function readablePeriod(r) {
  if (!r || typeof r !== 'object') return null;
  const v = r.period;
  return typeof v === 'number' && isFinite(v) ? v : null;
}

/** 星期几 → 中文。1=周一。超出 1..7 不编造，直接显示原始数字（§12.2 第 2 条）。 */
const DOW_CN = ['', '一', '二', '三', '四', '五', '六', '日'];
function dowLabel(dow) {
  if (dow === null) return '未知星期';
  const cn = DOW_CN[dow];
  return cn ? '周' + cn : ('第' + dow + '天');
}

/** 终止态 → 中文（契约 §6.3）。未知值原样显示，绝不合并成一句话。 */
const TERMINATION_CN = {
  ok: '已全部消解',
  no_solution: '无可行方案',
  best_effort: '已尽力，仍有残留',
};
function terminationLabel(t) {
  if (typeof t !== 'string' || t === '') return '接口未返回终止态';
  return TERMINATION_CN[t] || ('未知终止态：' + t);
}

/** 对象种类 → 样式类。🔴 未知 kind 落 .k-other 默认样式，不拦截渲染（§12.2 第 4 条）。 */
function kindClass(kind) {
  const map = { group: 'k-group', user: 'k-user', resource: 'k-resource' };
  return map[kind] || 'k-other';
}

function kindLabel(kind) {
  const map = { group: '组', user: '人', resource: '场地' };
  return map[kind] || str(kind, '', '未知') || '未知';
}

/**
 * 冲突计数读取。
 * 🔴 count 与 raw_count 含义不同，物理分开返回，调用方不许混用：
 *    groups = 聚合后的冲突组数；raw = 聚合前的原始条数。
 * raw_count 不在契约 §5 的示例响应里（示例只给了 count），所以缺失时返回 null
 * 而不是拿 count顶替——宁可显示「接口未提供」，也不编一个数出来。
 */
function readCounts(body) {
  const items = arr(body, 'items');
  let groups = intOrNull(body, 'count');
  if (groups === null) groups = items.length;   // count 缺失 → 用实际条数（仍是组数语义）
  const raw = intOrNull(body, 'raw_count');     // 缺失就是 null，不猜
  return { groups: groups, raw: raw, items: items };
}

/* =============================================================================
 * 4. DOM 小工具
 * -----------------------------------------------------------------------------
 * 🔴 只用 textContent，绝不写 innerHTML（AGENTS §8 防存储型 XSS）。
 *    组名 / 人名 / 课程名全部是用户可见文本，一律走 opts.text。
 * ========================================================================== */

function el(tag, opts) {
  const node = document.createElement(tag);
  if (opts) {
    if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
    if (opts.cls) node.className = opts.cls;
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (k) { node.setAttribute(k, opts.attrs[k]); });
    }
  }
  return node;
}

function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
}

/**
 * 就地显示/隐藏一条文本说明。msg 为空则清空并隐藏。
 * 🔴 节点自身的 class 由 HTML 决定，本函数不改class —— 所以同一个函数
 *    可以安全地用在 .banner（错误条）和 .note（口径说明）两种元素上。
 */
function showBanner(node, msg) {
  if (!node) return;
  if (!msg) { node.hidden = true; clear(node); return; }
  clear(node);
  node.appendChild(el('span', { text: msg }));
  node.hidden = false;
}

/* =============================================================================
 * 5. 渲染：周视图
 * -----------------------------------------------------------------------------
 * 数据来源与边界（重要，答辩会问）：
 *   · 表格尺寸 ← days.length × 节次数（不写死 5 / 10）
 *   · 占用格（主线）← /api/schedules 的 grid["<object_id>|<date>"]，经
 *     dayMaskPeriods() 反解单日掩码（bit N = 第 N 节）→ 画进格子；
 *     格子文字 = 查看对象名（/api/schedules 不返回课程名，契约 §4）
 *   · 冲突格（叠加层）← /api/conflicts 的 readable 定位，只在已有占用格上标红，
 *     不决定格子是否存在
 *   · 「每日占用」芯片 ← grid 的十六进制串原样透传（renderMasks，不反解）
 *
 * 🔴 关于节次数：契约未给节次列表字段，AGENTS §1b 又要求「每天几节」属规则数据化。
 *    这里的处理是——若响应里带了 periods 数组就按它的 .length 渲染，
 *    否则退回显示用的兜底值 DEMO_PERIODS（演示库建表是 10 节）。
 *    换成 12 节的作息库不需要改这里，后端把 periods 给我即可。
 * ========================================================================== */
const DEMO_PERIODS = 10;

/**
 * 把 grid 里的单日掩码 hex 反解成「哪些节被占用」。
 * 🔴 F7 起前端唯一的掩码解码点：/api/schedules 的 grid 值已是后端归一化后的
 *    「单日 1-based 掩码」——第 N 节 → bit N（1<<N），即 0x2=第1节、0x6=第1+2节。
 *    这是 m2.day_slots_mask()（后端做 `1 << period`）的逆运算。
 *    用 0-based（`1 << (p-1)`）或直接 format(mask,'x') 会「整页错一位且不报错」
 *    （AGENTS §10.0 第 2 处静默失效）。
 * @param hex  形如 "0x000006" 的十六进制串（宽容：缺失/非法 → 空结果，不抛错）
 * @param limit 渲染的节次数（nPeriods，可能来自 periods.length，不写死 10）
 */
function dayMaskPeriods(hex, limit) {
  const out = [];
  if (typeof hex !== 'string' || !hex) return out;
  let v = parseInt(hex.replace(/^0x/i, ''), 16);
  if (!isFinite(v)) v = 0;
  for (let p = 1; p <= limit; p++) if (v & (1 << p)) out.push(p);
  return out;
}

function renderWeek(schedules, conflicts) {
  const box = $('weekBox');
  const errBox = $('weekErr');
  clear(box);

  const days = arr(schedules, 'days');
  if (!days.length) {
    showBanner(errBox, '周视图数据里没有 days 字段（契约 §4），无法确定本周日期。请检查后端响应。');
    return;
  }
  state.days = days;
  showBanner(errBox, null);

  const periods = arr(schedules, 'periods');
  const nPeriods = periods.length ? periods.length : DEMO_PERIODS;

  // 表格：thead = 天，tbody 每行 = 一节
  const table = el('table', { cls: 'grid' });

  const thead = el('thead');
  const htr = el('tr');
  htr.appendChild(el('th', { text: '节次' }));
  days.forEach(function (d, i) {
    // 🔴 days 的元素是「日期字符串本身」，不是对象——不能套 str() 取字段（会恒取到 fallback）
    //    列标题的星期由位置推：days[0] 是周一（契约 §4「以周一为一周起点」）
    const dateText = (typeof d === 'string' && d) ? d : '（日期缺失）';
    htr.appendChild(el('th', { text: dowLabel(i + 1) + ' ' + dateText }));
  });
  thead.appendChild(htr);
  table.appendChild(thead);

  // ---- 主线：以 schedules.grid 为准，反解出「查看对象」每天的占用节次 ----
  // 🔴 格子是否存在只由 grid 决定；conflicts 只做叠加，不得决定格子是否存在。
  const grid = (schedules && typeof schedules.grid === 'object' && schedules.grid) ? schedules.grid : {};
  const objId = state.objectId;
  const objName = objectName(objId);
  const occ = new Map();   // key = "dayIndex|period" → true（该格有课/活动）
  days.forEach(function (d, di) {
    const hex = grid[objId + '|' + d];
    if (typeof hex !== 'string' || !hex) return;
    dayMaskPeriods(hex, nPeriods).forEach(function (p) {
      occ.set(di + '|' + p, true);
    });
  });

  // ---- 叠加层：冲突只按 (日期, 节次) 定位，供下面已占用的格子标红 ----
  const hits = new Map();
  arr(conflicts, 'items').forEach(function (it) {
    const when = conflictPlacement(it, days, nPeriods);
    if (!when) return;
    // 🔴 readable 为空 → periods 为空 → 不落格，但列表里那条冲突照样显示。
    //    绝不因为「定不到位」而少报一条冲突（静默漏报比报错更危险）。
    when.periods.forEach(function (p) {
      const k = when.dayIndex + '|' + p;
      const cell = hits.get(k) || { day: when.date, texts: [] };
      cell.texts.push(sideTitle(it.a) + ' × ' + sideTitle(it.b));
      hits.set(k, cell);
    });
  });

  // ---- 空态兜底：该对象本周无任何占用 → 明确文案，不留白 ----
  if (!occ.size) {
    box.appendChild(el('div', {
      cls: 'empty',
      text: (objId ? '本周暂无排课（' + (objName || objId) + '）' : '本周暂无排课（未选择查看对象）') + '。',
    }));
    box.appendChild(renderMasks(schedules, days));
    return;
  }

  const tbody = el('tbody');
  for (let p = 1; p <= nPeriods; p++) {
    const tr = el('tr');
    tr.appendChild(el('th', { text: '第' + p + '节' }));
    for (let di = 0; di < days.length; di++) {
      const td = el('td');
      const k = di + '|' + p;
      if (!occ.has(k)) { tr.appendChild(td); continue; }
      const hit = hits.get(k);
      td.className = hit ? 'occ hit' : 'occ';
      td.appendChild(el('span', { cls: 'who', text: objName || '（占用）' }));
      if (hit) {
        // 同一格可能撞多条，去重后逐条列出（.length 驱动，不写死条数）
        const seen = [];
        hit.texts.forEach(function (t) { if (seen.indexOf(t) < 0) seen.push(t); });
        seen.forEach(function (t) { td.appendChild(el('span', { cls: 'vs', text: t })); });
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  box.appendChild(table);

  box.appendChild(renderMasks(schedules, days));
}

/** 「每日占用」：把 grid 里的掩码原样透传出来给人看。 */
function renderMasks(schedules, days) {
  const wrap = el('div', { cls: 'masklist' });
  const grid = (schedules && typeof schedules.grid === 'object' && schedules.grid) ? schedules.grid : {};
  const objId = state.objectId;
  const objName = objectName(objId);
  wrap.appendChild(el('span', { text: '每日占用（' + (objId ? objName + ' / ' + objId : '未选对象') + '）：' }));

  if (!objId) {
    wrap.appendChild(el('span', { cls: 'chip', text: '请先在上方选择要查看的对象' }));
    return wrap;
  }
  //🔴 逐格取 grid["<object_id>|<date>"] 并原样显示。键的拼接是契约 §4 规定的
  //   竖线分隔；🔴 但绝不解析这个值——不做位运算、不做字符串比较。
  const found = [];
  days.forEach(function (d) {
    const raw = grid[objId + '|' + d];
    if (typeof raw === 'string' && raw !== '') found.push([d, raw]);
  });
  if (!found.length) {
    wrap.appendChild(el('span', { cls: 'chip', text: '该对象本周无占用记录' }));
    return wrap;
  }
  found.forEach(function (pair) {
    const isHit = state.hitDates.indexOf(pair[0]) >= 0;
    const chip = el('span', { cls: isHit ? 'chip hit' : 'chip' });
    chip.appendChild(el('code', { text: pair[1] }));   // 十六进制串原样透传
    chip.appendChild(el('span', { text: ' ' + dowLabel(dayIndexOf(pair[0], state.days) + 1) }));
    wrap.appendChild(chip);
  });
  return wrap;
}

/**
 * 从冲突条目算出它落在第几天、哪几节。
 * 🔴 只读后端给的 readable，不解掩码。readable 缺失或越界 → periods 为空，
 *    该条冲突仍会显示在列表里，只是不落进周视图的格子（降级而非报错）。
 * @param nPeriods 当前渲染的节次数（可能来自 periods.length，不写死 10）
 */
function conflictPlacement(item, days, nPeriods) {
  if (!item || typeof item !== 'object') return null;
  const date = str(item, 'date', '');
  const dayIndex = days.indexOf(date);
  if (dayIndex < 0) return null;            // 不在本周 → 不落格
  const limit = nPeriods || DEMO_PERIODS;
  const periods = [];
  arr(item, 'readable').forEach(function (r) {
    const p = readablePeriod(r);
    if (p !== null && p >= 1 && p <= limit) periods.push(p);
  });
  return { dayIndex: dayIndex, date: date, periods: periods };
}

/**
 * a/b 一侧的显示名。
 * 契约 §5 的 title 可能是「数学」也可能是「高一(1)班 物理」（后端怎么存就怎么显示，
 * 前端不猜）。若 title 里已经含了对象名，就只显示 title，避免出现
 * 「高一(1)班 高一(1)班 物理」这种重复。
 */
function sideTitle(side) {
  const who = str(side, 'object_name', '') || str(side, 'object_id', '') || '（对象未知）';
  const title = str(side, 'title', '');
  if (!title) return who;
  if (who && title.indexOf(who) >= 0) return title;   // title 已含who，去重
  return who + ' ' + title;
}

function dayIndexOf(date, days) {
  if (!Array.isArray(days)) return -1;
  return days.indexOf(date);
}

function objectName(id) {
  const o = state.objects.find(function (x) { return x.id === id; });
  return o ? str(o, 'name', '') : '';
}

/* =============================================================================
 * 6. 渲染：冲突列表
 * ========================================================================== */
function renderConflicts(counts) {
  const box = $('confBox');
  const errBox = $('confErr');
  clear(box);

  // 🔴 记录冲突涉及的日期，用于把周视图里对应的占用格也标红（同框显示）
  state.hitDates = [];
  arr(counts, 'items').forEach(function (it) {
    const d = str(it, 'date', '');
    if (d && state.hitDates.indexOf(d) < 0) state.hitDates.push(d);
  });

  // ---- 统计条：count（组数）与 raw_count（原始处数）分开显示，不混用 ----
  const stats = el('div', { cls: 'stats' });
  const gStat = el('div', { cls: counts.groups > 0 ? 'stat bad' : 'stat good' });
  gStat.appendChild(el('b', { text: String(counts.groups) }));
  gStat.appendChild(el('span', { text: ' 组冲突待处理' }));
  stats.appendChild(gStat);

  const rStat = el('div', { cls: 'stat' });
  rStat.appendChild(el('b', { text: counts.raw === null ? '—' : String(counts.raw) }));
  rStat.appendChild(el('span', {
    text: counts.raw === null ? ' 处原始冲突（接口未提供 raw_count）' : ' 处原始冲突',
  }));
  stats.appendChild(rStat);

  box.appendChild(stats);

  // ---- 口径说明：把count / raw_count 的区别直接写在页面上 ----
  //     答辩时评委一定会问「为什么上面写3、下面写 23」。答案就地写在页面上，
  //     不用口头解释，也避免答辩现场解释不清。
  //     🔴 这两个数字都来自响应，本页不产生、不推断任何数字。
  showBanner($('resolveHint'),
    counts.raw === null
      ? '说明：接口未提供 raw_count，无法显示原始冲突处数。'
      : '口径说明：「' + counts.groups + ' 组」是服务端聚合后的冲突组数（一条冲突涉及多个学生时'
        + '只报1 组，否则同班 40 名学生会把一处冲突刷成 40 条）；「' + counts.raw
        + ' 处」是聚合前的原始冲突条数。消解按钮调POST /api/resolve，'
        + '由后端统一求解（契约 §5 要求服务端聚合、§6 要求前端不做位运算）。'
        + ' 消解后结果条上可「撤销本次消解」——消解是原地改写课表，撤销按应用前的'
        + ' 全量快照逐字段回写；管理员还可「固化此版本」，固化后不可撤销。');

  // ---- 列表 ----
  const items = arr(counts, 'items');
  if (!items.length) {
    box.appendChild(el('div', {
      cls: 'empty',
      text: '接口返回 0 组冲突。注意：这是接口的真实结果，不是加载失败。',
    }));
    return;
  }

  const ul = el('ul', { cls: 'items' });
  items.forEach(function (it, idx) {
    ul.appendChild(renderConflictItem(it, idx));
  });
  box.appendChild(ul);
}

function renderConflictItem(item, idx) {
  const li = el('li');

  // ---- 顶部：第几组 + 时间 ----
  const top = el('div', { cls: 'it-top' });
  const left = el('div');
  left.appendChild(el('span', { cls: 'it-when', text: '冲突组 ' + (idx + 1) }));
  const when = conflictWhenText(item);
  if (when) left.appendChild(el('span', { text: '· ' + when }));
  top.appendChild(left);

  // F7（B8）：冲突旁「提议改期」入口 → 弹层选日程 + 填新起始日 + 理由 → POST /api/approvals。
  // 🔴 入口不做权限预判（后端鉴权 + 自己批自己 403），前端只负责把请求如实交上去、把错误如实显示。
  const proposeBtn = el('button', { text: '提议改期', attrs: { type: 'button' } });
  proposeBtn.addEventListener('click', function () { openProposeModal(conflictTargets(item)); });
  top.appendChild(proposeBtn);

  const btn = el('button', {
    cls: 'danger',
    text: '一键消解',
    attrs: { type: 'button', 'data-idx': String(idx) },
  });
  btn.addEventListener('click', function () { doResolve(btn); });
  top.appendChild(btn);
  li.appendChild(top);

  // ---- 中部：a × b ----
  const vs = el('div', { cls: 'it-vs' });
  vs.appendChild(renderSide(item.a));
  vs.appendChild(el('span', { cls: 'vs-mark', text: '撞' }));
  vs.appendChild(renderSide(item.b));
  li.appendChild(vs);

  // ---- 掩码透传：a/b 两侧已各自显示过slots，这里只补冲突条目自身的 ----
  const slot = str(item, 'slots', '');
  if (slot) {
    const meta = el('div', { cls: 'mt', text: '本组冲突占用槽位（后端原样下发，不解码）：' });
    meta.appendChild(el('code', { text: slot }));
    li.appendChild(meta);
  }

  return li;
}

function renderSide(side) {
  const box = el('div', { cls: 'side' });
  const nm = el('div', { cls: 'nm', text: sideTitle(side) });
  box.appendChild(nm);

  const kind = str(side, 'object_kind', '');
  if (kind) {
    // 用带前导空格的 span 排版，不用 createTextNode —— 所有文本节点一律经el() 走 textContent
    nm.appendChild(el('span', { cls: 'kind ' + kindClass(kind), text: ' ' + kindLabel(kind) }));
  }

  const title = str(side, 'title', '');
  if (title && title !== sideTitle(side)) box.appendChild(el('div', { cls: 'ti', text: title }));

  // 🔴 slots 一律原样显示，绝不做字符串比较（AGENTS §9b：长度不定，比字符串会静默漏匹配）
  const s = str(side, 'slots', '');
  if (s) {
    const m = el('div', { cls: 'mt' });
    m.appendChild(el('span', { text: '槽位 ' }));
    m.appendChild(el('code', { text: s }));
    box.appendChild(m);
  }
  const d = str(side, 'date', '');
  if (d) box.appendChild(el('div', { cls: 'mt', text: '日期 ' + d }));
  return box;
}

function conflictWhenText(item) {
  const bits = [];
  const d = str(item, 'date', '');
  let dowText = '';
  if (d) {
    const di = dayIndexOf(d, state.days);
    if (di >= 0) dowText = dowLabel(di + 1);
    bits.push(d + (dowText ? '（' + dowText + '）' : ''));
  }
  // readable 的 dow 已在上面括号里写过了，此处只补「第N 节」，避免出现「周三 周三 第1节」
  const ps = [];
  arr(item, 'readable').forEach(function (r) {
    const p = readablePeriod(r);
    if (p !== null) ps.push('第' + p + '节');
  });
  if (ps.length) bits.push(ps.join('、'));
  return bits.join(' ');
}

/* =============================================================================
 * 7. 渲染：消解前后对比
 * -----------------------------------------------------------------------------
 * 🔴 所有数字都从 POST /api/resolve 的响应里读，一处都不写死。
 * ========================================================================== */
function renderResolveResult(body) {
  const box = document.getElementById('resBox');
  clear(box);

  if (!body || typeof body !== 'object') {
    box.appendChild(el('div', { cls: 'empty', text: '消解接口没有返回可显示的结果（响应为空或结构不符）。' }));
    return;
  }

  // remaining_before / remaining_after 是「处数」，与列表用的 raw_count 同口径
  const before = intOrNull(body, 'remaining_before');
  const after = intOrNull(body, 'remaining_after');
  const applied = intOrNull(body, 'applied');
  const rounds = intOrNull(body, 'rounds');
  const filtered = intOrNull(body, 'filtered_out_count');
  const term = body.termination;

  const banner = el('div', { cls: after === 0 ? 'banner ok' : 'banner warn' });
  const cmp = el('div', { cls: 'cmp' });
  cmp.appendChild(el('span', { text: '冲突' }));
  cmp.appendChild(el('span', { cls: 'from', text: before === null ? '?' : String(before) + ' 处' }));
  cmp.appendChild(el('span', { text: '→' }));
  cmp.appendChild(el('span', { cls: 'to', text: after === null ? '?' : String(after) + ' 处' }));
  cmp.appendChild(el('span', { text: '（' + terminationLabel(term) + '）' }));
  banner.appendChild(cmp);
  box.appendChild(banner);

  const kv = el('div', { cls: 'kv' });
  [
    ['已应用方案数 applied', applied],
    ['迭代轮数 rounds', rounds],
    ['其中因不在可改范围、未尝试的filtered_out_count', filtered],
  ].forEach(function (pair) {
    kv.appendChild(el('div', {
      text: pair[0] + '：' + (pair[1] === null ? '接口未返回' : String(pair[1])),
    }));
  });
  box.appendChild(kv);

  if (filtered !== null && filtered > 0) {
    box.appendChild(el('div', {
      cls: 'banner warn',
      text: '其中 ' + filtered + ' 处因不在你的可改范围，系统未尝试求解——不是消解失败，是权限边界。',
    }));
  }
  if (after !== 0) {
    box.appendChild(el('div', {
      cls: 'banner warn',
      text: '仍有 ' + after + ' 处未消解。请看上方终止态说明，或联系有改权限的人处理；系统不会假装已经解决。',
    }));
  }

  /* ---- 闭环（卡 rvdL9a）：撤销本次消解 / 固化此版本 ----
   * 为什么必须有：apply_plan 是原地 UPDATE，点一次就回不去了。
   * 现场敢点「一键消解」的前提是「能撤回」，所以按钮紧贴结果条，
   * 文案带上真实数字（N 处 / 23 → 0），不写死。
   * 🔴 只有 applied > 0 才显示撤销（没改动就没有可撤销的东西）。
   * 🔴 固化只对 admin 显示前端按钮；真正的权限判定在后端（teacher → 403）。
   */
  const sid = body.snapshot_id ? String(body.snapshot_id) : '';
  if (sid) {
    const strip = el('div', { cls: 'banner ok' });
    const canUndo = applied !== null && applied > 0;
    strip.appendChild(el('span', {
      text: '已消解 ' + (applied === null ? '?' : String(applied)) + ' 处（'
        + (before === null ? '?' : String(before)) + ' → '
        + (after === null ? '?' : String(after)) + '）' + (canUndo ? '｜' : ''),
    }));
    if (canUndo) {
      const undo = el('button', {
        cls: 'primary', text: '撤销本次消解', attrs: { type: 'button' },
      });
      undo.addEventListener('click', function () { doRevert(sid, undo); });
      strip.appendChild(undo);
    }
    if (isAdmin()) {
      const fix = el('button', {
        cls: 'danger', text: '固化此版本', attrs: { type: 'button' },
      });
      fix.addEventListener('click', function () { doCommit(sid, fix); });
      strip.appendChild(fix);
    }
    box.appendChild(strip);
  }
}

/** 当前登录身份是否管理员（仅用于决定「固化此版本」按钮是否显示）。
 *  真正的权限判定在后端（commit 要求该组 own 档，teacher 一律 403）。 */
function isAdmin() {
  return !!(state.user && state.user.global_role === 'admin');
}

/** 撤销本次消解：按应用前快照逐字段回写 → 刷新周视图与冲突列表。 */
async function doRevert(sid, btn) {
  if (btn) btn.disabled = true;
  try {
    const body = await apiPost('/api/resolve/revert', withGroup({ id: sid }), {});
    await loadAll();   // 周视图 + 冲突列表重新拉，反映还原后的真实状态
    const after = intOrNull(body, 'conflicts_after');
    const restored = intOrNull(body, 'restored');
    clear($('resBox'));
    $('resBox').appendChild(el('div', {
      cls: 'banner ok',
      text: '已还原：冲突回到 ' + (after === null ? '?' : String(after)) + ' 处'
        + '（回写 ' + (restored === null ? '?' : String(restored)) + ' 条日程，逐字段等于消解前）'
        + ((body && body.already_reverted) ? '。这条快照先前已撤销，本次未改动数据。' : ''),
    }));
  } catch (e) {
    if (btn) btn.disabled = false;
    clear($('resBox'));
    $('resBox').appendChild(el('div', {
      cls: 'banner error',
      text: '撤销失败：' + httpErrText(e) + '（409 = 该版本已固化，需管理员重新消解）',
    }));
  }
}

/** 固化此版本：此后 revert 一律 409（不许静默忽略）。 */
async function doCommit(sid, btn) {
  if (btn) btn.disabled = true;
  try {
    await apiPost('/api/resolve/commit', withGroup({ id: sid }), {});
    clear($('resBox'));
    $('resBox').appendChild(el('div', {
      cls: 'banner ok',
      text: '已固化此版本（快照 ' + sid + '）。此后再撤销会被拒绝；如需调整请重新执行消解。',
    }));
  } catch (e) {
    if (btn) btn.disabled = false;
    clear($('resBox'));
    $('resBox').appendChild(el('div', {
      cls: 'banner error',
      text: '固化失败：' + httpErrText(e) + '（403 = 需要管理员或 own 档权限）',
    }));
  }
}

/* =============================================================================
 * 8. 交互与数据加载
 * ========================================================================== */
const state = {
  groupId: '',
  objectId: '',
  objects: [],
  days: [],
  hitDates: [],
  schedules: null,   // 最近一次 /api/schedules 原始响应（切对象时重画用，不重复打接口）
  conflicts: null,   // 最近一次 /api/conflicts 原始响应
  lastResult: null,
  token: '',         // F3b：登录后的 Bearer token（仅内存，不落盘，刷新需重登）
  user: null,        // /api/login 返回的 user 对象（仅用于显示登录身份）
  sessionId: '',     // F5：会话级随机串（埋点平均时长用，boot() 时生成一次）
  taskActive: false, // F5：task_start 已发、尚未 success/abandon
  taskStartedAt: 0,  // F5：task_start 时刻（ms）
  taskTimer: 0,      // F5：90 秒无操作超时定时器 id
};

const $ = function (id) { return document.getElementById(id); };

/* ── F3b 登录闭环：POST /api/login → 存 token → 首次 loadAll ──────────────
 * 为什么必须有这一步：B3/B4 接入鉴权后未登录一律 401（docs/07 通用约定），
 * mock 时代不需要；登录本身不需要 token（docs/07 §1）。
 * ---------------------------------------------------------------------- */
function showLogin(msg) {
  const sec = $('loginSec');
  if (sec) sec.hidden = false;
  const form = $('loginForm');
  if (form) form.hidden = false;
  if (msg) showBanner($('loginErr'), msg);
}

/** 登录成功：只收起表单行，loginSec 与身份提示保留（现场要能看出登录身份）。 */
function hideLogin() {
  const form = $('loginForm');
  if (form) form.hidden = true;
  showBanner($('loginErr'), null);
}

async function doLogin() {
  const name = $('loginName').value.trim();
  const pwd = $('loginPwd').value;
  if (!name || !pwd) {
    showBanner($('loginErr'), '请输入登录名与密码。');
    return;
  }
  const btn = $('loginBtn');
  btn.disabled = true;
  try {
    const body = await apiPost('/api/login', {}, { login: name, password: pwd });
    state.token = str(body, 'token', '');
    state.user = (body && typeof body.user === 'object' && body.user) ? body.user : null;
    if (!state.token) {
      showBanner($('loginErr'), '登录接口未返回 token（HTTP 200 但结构不符），请检查后端版本与契约 §1。');
      return;
    }
    const gid = str(body, 'default_group_id', '');
    if (gid) state.groupId = gid;   // 默认组＝该用户所在的最高组（契约 §1，队长 10-01 定）
    $('loginPwd').value = '';
    $('loginWho').textContent = '已登录：' + (state.user ? (str(state.user, 'login', '') || name) : name);
    hideLogin();
    delete $('groupSel').dataset.loaded;   // 换账号后授权组可能不同，强制重拉组列表
    await loadAll();
  } catch (e) {
    // 🔴 2026-11-10 文案纠正：原文写「连续失败 5 次将锁定 15 分钟」——
    //    实际实现是**组合锁 60 秒**（api_login._LOCK_SECONDS），不是 15 分钟。
    //    按错文案，老师会白等 15 分钟；且不该把限流阈值透露给使用者。
    //    统一口径：只提示「会短暂锁定 / 等 1 分钟」，与后端 message 一致。
    showBanner($('loginErr'), '登录失败：' + e.message +
      '（请检查大小写与首尾空格；多次失败会短暂锁定，等 1 分钟再试）');
  } finally {
    btn.disabled = false;
  }
}

/** 组切换器。只列有授权的组——后端不返回未授权的组，前端不做灰显（契约 §2）。 */
function renderGroupOptions(groups) {
  const sel = $('groupSel');
  clear(sel);
  const list = Array.isArray(groups) ? groups : [];
  if (!list.length) {
    sel.appendChild(el('option', { text: '（没有你有授权的组）', attrs: { value: '' } }));
    return;
  }
  list.forEach(function (g) {
    const id = str(g, 'group_id', '');
    const bits = [str(g, 'name', '(未命名组)')];
    const parent = str(g, 'parent_name', '');
    if (parent) bits.push('隶属 ' + parent);
    const level = str(g, 'level', '');
    if (level) bits.push('权限 ' + level);
    const mc = intOrNull(g, 'member_count');
    if (mc !== null) bits.push(mc + ' 人');
    const o = el('option', { text: bits.join(' · '), attrs: { value: id } });
    sel.appendChild(o);
  });
  if (state.groupId) sel.value = state.groupId;
}

/** 对象下拉：周视图的「查看对象」。按 .length 渲染，不写死条数。 */
function renderObjectOptions(objects) {
  const sel = $('objSel');
  clear(sel);
  state.objects = Array.isArray(objects) ? objects : [];
  if (!state.objects.length) {
    sel.appendChild(el('option', { text: '（无对象）', attrs: { value: '' } }));
    state.objectId = '';
    return;
  }
  state.objects.forEach(function (o) {
    const id = str(o, 'id', '');
    const bits = [str(o, 'name', '(未命名)')];
    const k = str(o, 'kind', '');
    if (k) bits.push(kindLabel(k));
    sel.appendChild(el('option', { text: bits.join(' · '), attrs: { value: id } }));
  });
  // 默认落在当前组本身（组的周视图最有意义），组不在列表里就取第一个
  const hasGroup = state.objects.some(function (o) { return o.id === state.groupId; });
  state.objectId = hasGroup ? state.groupId : str(state.objects[0], 'id', '');
  sel.value = state.objectId;
}

/** 一次拉齐三接口并渲染。任一失败 → 该区块显式报错，不白屏、不伪装成「无数据」。 */
async function loadAll() {
  const fatal = $('fatal');
  showBanner(fatal, null);

  // ---- 1. 组列表（仅首次；组切换时不重拉，避免无谓闪烁）----
  if (!$('groupSel').dataset.loaded) {
    try {
      const groups = await apiGet('/api/groups', {});
      renderGroupOptions(groups);
      $('groupSel').dataset.loaded = '1';
      const list = Array.isArray(groups) ? groups : [];
      // F3b：登录返回的 default_group_id 已预置进 state.groupId；
      // 但若它不在授权列表里（或为空），回退到第一个授权组，绝不保留无效值
      // ——select.value 设成不存在的 option 会导致后续切组静默失效。
      const ids = list.map(function (g) { return str(g, 'group_id', ''); });
      if (ids.indexOf(state.groupId) < 0) state.groupId = ids.length ? ids[0] : '';
      $('groupSel').value = state.groupId;
    } catch (e) {
      showBanner(fatal, '组列表加载失败：' + e.message + '（无法切换组，请检查后端服务是否在运行）');
      renderGroupOptions([]);
      return;
    }
  }
  state.groupId = $('groupSel').value || state.groupId;

  // ---- 2. 周视图数据 ----
  let schedules = null;
  let conflicts = null;
  const params = withGroup({});
  const weekVal = $('weekSel').value.trim();
  if (weekVal) params.week = weekVal;

  try {
    schedules = await apiGet('/api/schedules', params);
  } catch (e) {
    showBanner($('weekErr'), '周视图加载失败：' + e.message);
    clear($('weekBox'));
    $('weekBox').appendChild(el('div', { cls: 'empty', text: '周视图数据未取到（不是「本周没课」）。' }));
  }
  if (schedules) {
    state.days = arr(schedules, 'days');
    renderObjectOptions(arr(schedules, 'objects'));
  }

  try {
    conflicts = await apiGet('/api/conflicts', withGroup({}));
  } catch (e) {
    showBanner($('confErr'), '冲突列表加载失败：' + e.message);
    clear($('confBox'));
    $('confBox').appendChild(el('div', {
      cls: 'empty',
      text: '🔴 冲突数据未取到。这不等于「没有冲突」——请先修好接口再看结论。',
    }));
  }

  // ---- 3. 渲染（有数据才渲染，缺数据保留上面的错误提示）----
  // 顺序有讲究：renderConflicts 会填 state.hitDates（冲突涉及的日期），
  // 周视图的「每日占用」芯片要用它把撞车那天的掩码标红，所以先渲染冲突列表。
  state.schedules = schedules;
  state.conflicts = conflicts;

  if (conflicts) {
    renderConflicts(readCounts(conflicts));
  } else {
    // 冲突接口挂了：hitDates 清空，但绝不能显示成「0 组冲突」
    state.hitDates = [];
  }
  if (schedules) {
    renderWeek(schedules, conflicts || { items: [] });
  }

  $('resolveBtn').disabled = !(conflicts && readCounts(conflicts).groups > 0);

  // ---- 4. 变更请求（F7/B8）：org 级列表，登录/切组/重载后都刷新 ----
  await loadRequests();
}

async function doResolve(btn) {
  const btns = document.querySelectorAll('#confBox button.danger');
  btns.forEach(function (b) { b.disabled = true; });
  const main = $('resolveBtn');
  main.disabled = true;
  main.textContent = '消解中…';

  // F5 埋点：点下「一键消解」的瞬间记 task_start（完成率分母，docs/08 §3.2）
  ping('task_start', { mode: 'minimal' });
  markTaskActive();

  try {
    const body = await apiPost('/api/resolve', withGroup({ mode: 'minimal' }), { plan_index: 0 });
    state.lastResult = body;
    // F5 埋点：方案成功写回记 task_success（完成率分子）。payload 只带数值，无名字。
    ping('task_success', {
      applied: intOrNull(body, 'applied'),
      remaining_after: intOrNull(body, 'remaining_after'),
    });
    markTaskDone();
    renderResolveResult(body);
    await loadAll();   // 重新拉数据，让周视图与冲突列表反映消解后的真实状态
  } catch (e) {
    // F5 埋点：失败/取消记 task_abandon（单独说明，不是失败，docs/08 §3.2）
    ping('task_abandon', { reason: 'error' });
    markTaskDone();
    clear($('resBox'));
    $('resBox').appendChild(el('div', {
      cls: 'banner error',
      text: '一键消解失败：' + e.message + '（数据未改变，可重试；若持续 429 是限频，等 60 秒再试）',
    }));
  } finally {
    main.textContent = '一键消解（当前组）';
    main.disabled = false;
    btns.forEach(function (b) { b.disabled = false; });
  }
}

/* =============================================================================
 * 9. 启动
 * ========================================================================== */
function bindEvents() {
  $('groupSel').addEventListener('change', function () {
    state.groupId = $('groupSel').value;
    state.objectId = '';       //换组 → 对象重选，避免看到上一个组的周视图
    clear($('resBox'));
    $('resBox').appendChild(el('div', { cls: 'empty', text: '尚未执行消解。' }));
    loadAll();
  });
  $('reloadBtn').addEventListener('click', function () { loadAll(); });
  // F7（B8）：变更请求列表的刷新按钮（org 级，不触发整页重载）
  $('reqReloadBtn').addEventListener('click', function () { loadRequests(); });
  $('weekSel').addEventListener('change', function () { loadAll(); });
  $('objSel').addEventListener('change', function () {
    state.objectId = $('objSel').value;
    // 只重画周视图，不必重拉接口
    if (state.schedules) renderWeek(state.schedules, state.conflicts || { items: [] });
  });
  $('resolveBtn').addEventListener('click', function () { doResolve(null); });
  // F3b：登录栏（mock 模式下 loginSec 整体隐藏，绑定无害）
  $('loginBtn').addEventListener('click', function () { doLogin(); });
  [$('loginName'), $('loginPwd')].forEach(function (inp) {
    inp.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') doLogin(); });
  });
  // F5 埋点：关页时若任务仍在进行，补 task_abandon（docs/08 §4.3 beforeunload 兜底）
  window.addEventListener('pagehide', function () {
    if (state.taskActive) {
      state.taskActive = false;
      ping('task_abandon', { reason: 'page_hide' });
    }
  });
}

function boot() {
  state.sessionId = newSessionId();   // F5：每次打开页面生成一个会话串（埋点时长统计用）
  const tag = $('srcTag');
  if (USE_MOCK) {
    tag.hidden = false;
    tag.textContent = '演示数据（mock）';
  } else {
    // F3b：真实接口须先登录（未登录一律 401）；登录成功后由 doLogin() 触发首次加载
    showLogin(null);
  }
  bindEvents();
  if (USE_MOCK) loadAll();
}

/* =============================================================================
 * 10. 审批流（F7 / B8）：提议改期 + 我的请求
 * -----------------------------------------------------------------------------
 * 端点形态（B8 已实测，路由精确匹配、无 path 参数）：
 *   POST /api/approvals              提交提议
 *   GET  /api/approvals              列出本组织请求
 *   POST /api/approvals/approve?id=  批准
 *   POST /api/approvals/reject?id=   拒绝（body 必须带 reason）
 *   POST /api/approvals/withdraw?id= 撤回（仅提出人）
 *   GET  /api/approvals/events?id=   审计轨迹（本卡不铺 UI，仅列契约）
 *
 * 🔴 铁律（与后端一致）：
 *   - 禁用 innerHTML，一律 textContent；
 *   - 不做位运算、不解掩码、不内置枚举映射表（状态文案有默认表，未知值原样显示）；
 *   - 宽容解析：未知字段忽略、未知枚举降级、数组长度读 .length 不写死；
 *   - 不本地硬编码 group/user：身份 = state.user.id（登录返回），组 = state.groupId；
 *   - 404/403/409 一律如实提示（apiGet/apiPost 已把 res.status 挂到 Error.status）。
 * ========================================================================== */

/** 错误文案：能拿到 HTTP 状态码就前缀标注（如「HTTP 403：不能批准自己提出的请求」）。 */
function httpErrText(e) {
  const code = (e && e.status) ? ('HTTP ' + e.status + '：') : '';
  const msg = (e && e.message) ? e.message : String(e);
  return code + msg;
}

/* ---- 弹层（提议改期 / 拒绝共用容器） ---- */

function modalOverlay() {
  const ov = el('div', { cls: 'modal-overlay' });
  const box = el('div', { cls: 'modal', attrs: { role: 'dialog', 'aria-modal': 'true' } });
  ov.appendChild(box);
  document.body.appendChild(ov);
  return { ov: ov, box: box };
}

function closeModal(ov) {
  if (ov && ov.parentNode) ov.parentNode.removeChild(ov);
}

/* ---- 提议改期 ---- */

/** 从冲突条目取可选目标日程（a/b 两侧，按 schedule_id 去重）。返回 [{schedule_id, title}]。 */
function conflictTargets(item) {
  const out = [];
  const seen = {};
  [item.a, item.b].forEach(function (side) {
    if (!side || typeof side !== 'object') return;
    const sid = str(side, 'schedule_id', '');
    if (!sid || seen[sid]) return;
    seen[sid] = true;
    out.push({ schedule_id: sid, title: sideTitle(side) });
  });
  return out;
}

/** 弹层填「日程 + 新起始日 + 理由」，提交 POST /api/approvals。 */
function openProposeModal(targets) {
  const md = modalOverlay();
  md.box.appendChild(el('h3', { text: '提议改期（走审批，批准后才落库）' }));

  const fTarget = el('div', { cls: 'field' });
  fTarget.appendChild(el('label', { text: '要改期的日程（周期日程起始日）' }));
  const selTarget = el('select');
  (targets || []).forEach(function (t) {
    selTarget.appendChild(el('option', {
      text: t.title + '（' + t.schedule_id + '）',
      attrs: { value: t.schedule_id },
    }));
  });
  if (!(targets || []).length) {
    selTarget.appendChild(el('option', { text: '（该冲突未提供可改期的日程 id）', attrs: { value: '' } }));
  }
  fTarget.appendChild(selTarget);
  md.box.appendChild(fTarget);

  const fDate = el('div', { cls: 'field' });
  fDate.appendChild(el('label', { text: '新的起始日 valid_from（YYYY-MM-DD）' }));
  const inpDate = el('input', { attrs: { type: 'date' } });
  fDate.appendChild(inpDate);
  md.box.appendChild(fDate);

  const fReason = el('div', { cls: 'field' });
  fReason.appendChild(el('label', { text: '理由 reason（必填）' }));
  const taReason = el('textarea', { attrs: { rows: '3' } });
  fReason.appendChild(taReason);
  md.box.appendChild(fReason);

  const errBox = el('div', { cls: 'banner error', attrs: { role: 'alert' } });
  errBox.hidden = true;
  md.box.appendChild(errBox);

  const actions = el('div', { cls: 'actions' });
  const btnOk = el('button', { cls: 'primary', text: '提交提议', attrs: { type: 'button' } });
  const btnCancel = el('button', { text: '取消', attrs: { type: 'button' } });
  btnCancel.addEventListener('click', function () { closeModal(md.ov); });
  actions.appendChild(btnOk);
  actions.appendChild(btnCancel);
  md.box.appendChild(actions);

  btnOk.addEventListener('click', async function () {
    const targetId = selTarget.value.trim();
    const validFrom = inpDate.value.trim();
    const reason = taReason.value.trim();
    if (!targetId) { showBanner(errBox, '请选择要改期的日程。'); return; }
    if (!validFrom) { showBanner(errBox, '请填写新的起始日。'); return; }
    if (!reason) { showBanner(errBox, '请填写改期理由。'); return; }
    btnOk.disabled = true;
    try {
      const body = await apiPost('/api/approvals', withGroup({}), {
        scope: 'permanent',
        op: 'update',
        target_type: 'schedule',
        target_id: targetId,
        payload: { after: { valid_from: validFrom } },
        reason: reason,
      });
      closeModal(md.ov);
      showBanner($('reqMsg'),
        '提议已提交：请求 ' + str(body, 'request_id', '') + '，状态 '
        + str(body, 'status', 'pending') + '。课表数据【零变化】——要等批准后才落库。');
      await loadRequests();
    } catch (e) {
      btnOk.disabled = false;
      showBanner(errBox, '提交失败：' + httpErrText(e));
    }
  });

  md.ov.addEventListener('click', function (ev) { if (ev.target === md.ov) closeModal(md.ov); });
}

/* ---- 我的请求列表 ---- */

/** 请求状态 → 中文。未知值原样显示（契约 §12.2 第 2 条：不内置白名单）。 */
const REQ_STATUS_CN = {
  draft: '草稿', pending: '待审批', approved: '已批准', applied: '已生效',
  rejected: '已拒绝', withdrawn: '已撤回',
};
function reqStatusLabel(s) {
  return (typeof s === 'string' && REQ_STATUS_CN[s]) ? REQ_STATUS_CN[s] : (s || '未知状态');
}

function isSelfRequest(req) {
  return !!(state.user && state.user.id && req.requester_id === state.user.id);
}

/** 按「后端状态 × 身份」决定动作按钮（返回动作名数组，终态只读）。 */
function reqActions(req) {
  if (req.status !== 'pending') return [];
  if (isSelfRequest(req)) return ['withdraw'];   // 自己提的 → 只撤回
  return ['approve', 'reject'];                   // 别人提的 → 同意/拒绝
}

function renderRequestItem(req) {
  const li = el('li', { cls: 'req-item' });

  // 顶部：状态 + 目标 + 是否我提的
  const top = el('div', { cls: 'it-top' });
  const left = el('div');
  left.appendChild(el('span', { cls: 'req-status st-' + str(req, 'status', ''), text: reqStatusLabel(str(req, 'status', '')) }));
  const target = str(req, 'target_id', '');
  if (target) left.appendChild(el('span', { text: ' · 目标日程 ' + target }));
  top.appendChild(left);
  if (isSelfRequest(req)) top.appendChild(el('span', { cls: 'req-self', text: '（我提出的）' }));
  li.appendChild(top);

  // before → after（payload 宽容解析，缺字段显示 —）
  const payload = (req && typeof req.payload === 'object' && req.payload) ? req.payload : {};
  const before = (payload && typeof payload.before === 'object' && payload.before) ? payload.before : null;
  const after = (payload && typeof payload.after === 'object' && payload.after) ? payload.after : null;
  const diff = el('div', { cls: 'req-diff' });
  diff.appendChild(el('span', {
    text: '起始日 ' + (before ? str(before, 'valid_from', '') || '—' : '—')
      + ' → ' + (after ? str(after, 'valid_from', '') || '—' : '—'),
  }));
  li.appendChild(diff);

  const reason = str(req, 'reason', '');
  if (reason) li.appendChild(el('div', { cls: 'req-reason', text: '理由：' + reason }));

  const meta = [];
  const created = str(req, 'created_at', '');
  if (created) meta.push('提出于 ' + created);
  const approver = str(req, 'approver_id', '');
  if (approver) meta.push('处理人 ' + approver);
  if (meta.length) li.appendChild(el('div', { cls: 'mt', text: meta.join(' · ') }));

  // 动作按钮（按 reqActions 决定；终态不出按钮）
  const acts = reqActions(req);
  if (acts.length) {
    const bar = el('div', { cls: 'req-actions' });
    acts.forEach(function (a) {
      let btn = null;
      if (a === 'approve') {
        btn = el('button', { cls: 'primary', text: '同意', attrs: { type: 'button' } });
        btn.addEventListener('click', function () { doApprove(req, btn); });
      } else if (a === 'reject') {
        btn = el('button', { cls: 'danger', text: '拒绝', attrs: { type: 'button' } });
        btn.addEventListener('click', function () { openRejectModal(req); });
      } else if (a === 'withdraw') {
        btn = el('button', { text: '撤回', attrs: { type: 'button' } });
        btn.addEventListener('click', function () { doWithdraw(req, btn); });
      }
      if (btn) bar.appendChild(btn);
    });
    li.appendChild(bar);
  }
  return li;
}

function renderRequests(body) {
  const box = $('reqBox');
  const errBox = $('reqErr');
  clear(box);
  showBanner(errBox, null);

  const items = arr(body, 'requests');
  if (!items.length) {
    box.appendChild(el('div', { cls: 'empty', text: '当前组织暂无变更请求。' }));
    return;
  }
  const ul = el('ul', { cls: 'req-list' });
  items.forEach(function (req) { ul.appendChild(renderRequestItem(req)); });
  box.appendChild(ul);
}

async function loadRequests() {
  const errBox = $('reqErr');
  try {
    const body = await apiGet('/api/approvals', withGroup({}));
    renderRequests(body);
  } catch (e) {
    showBanner(errBox, '变更请求加载失败：' + httpErrText(e));
    clear($('reqBox'));
    $('reqBox').appendChild(el('div', {
      cls: 'empty',
      text: '🔴 变更请求未取到（这不等于「没有请求」），请检查登录状态与后端服务。',
    }));
  }
}

async function doApprove(req, btn) {
  btn.disabled = true;
  try {
    const body = await apiPost('/api/approvals/approve', { id: req.id }, {});
    showBanner($('reqMsg'),
      '已批准请求 ' + req.id + '，状态 ' + str(body, 'status', 'applied')
      + '（valid_from 已落库生效）。');
    await loadRequests();
    await loadAll();   // 落库后重拉周视图/冲突列表，反映真实状态
  } catch (e) {
    showBanner($('reqMsg'), '批准失败：' + httpErrText(e));
    btn.disabled = false;
  }
}

async function doWithdraw(req, btn) {
  btn.disabled = true;
  try {
    const body = await apiPost('/api/approvals/withdraw', { id: req.id }, {});
    showBanner($('reqMsg'),
      '已撤回请求 ' + req.id + '，状态 ' + str(body, 'status', 'withdrawn') + '。');
    await loadRequests();
  } catch (e) {
    showBanner($('reqMsg'), '撤回失败：' + httpErrText(e));
    btn.disabled = false;
  }
}

/** 拒绝弹层：body 必须带 reason，否则后端 400。 */
function openRejectModal(req) {
  const md = modalOverlay();
  md.box.appendChild(el('h3', { text: '拒绝请求（必须填写理由）' }));

  const fReason = el('div', { cls: 'field' });
  fReason.appendChild(el('label', { text: '拒绝理由 reason（必填，会被写入审计日志）' }));
  const taReason = el('textarea', { attrs: { rows: '3' } });
  fReason.appendChild(taReason);
  md.box.appendChild(fReason);

  const errBox = el('div', { cls: 'banner error', attrs: { role: 'alert' } });
  errBox.hidden = true;
  md.box.appendChild(errBox);

  const actions = el('div', { cls: 'actions' });
  const btnOk = el('button', { cls: 'danger', text: '确认拒绝', attrs: { type: 'button' } });
  const btnCancel = el('button', { text: '取消', attrs: { type: 'button' } });
  btnCancel.addEventListener('click', function () { closeModal(md.ov); });
  actions.appendChild(btnOk);
  actions.appendChild(btnCancel);
  md.box.appendChild(actions);

  btnOk.addEventListener('click', async function () {
    const reason = taReason.value.trim();
    if (!reason) { showBanner(errBox, '拒绝必须填写理由（后端会 400 拦截空理由）。'); return; }
    btnOk.disabled = true;
    try {
      const body = await apiPost('/api/approvals/reject', { id: req.id }, { reason: reason });
      closeModal(md.ov);
      showBanner($('reqMsg'), '已拒绝请求 ' + req.id + '，状态 ' + str(body, 'status', 'rejected') + '。');
      await loadRequests();
    } catch (e) {
      btnOk.disabled = false;
      showBanner(errBox, '拒绝失败：' + httpErrText(e));
    }
  });

  md.ov.addEventListener('click', function (ev) { if (ev.target === md.ov) closeModal(md.ov); });
}

document.addEventListener('DOMContentLoaded', boot);
