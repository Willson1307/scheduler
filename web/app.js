/* =============================================================================
 * 时之交响 TimeSymphony · 前端（F1 骨架）
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
 * 登录栏只在真实接口模式显示。登录态存 localStorage（卡 r4kJEe：
 * 刷新 / 新标签 / 关浏览器重开都不用重登），12h 滑动过期 + 24h 绝对上限，
 * 判据与取舍见下方「0b. 登录态持久化」。
 * ========================================================================== */
'use strict';

/* =============================================================================
 * 0. 数据来源开关
 * ========================================================================== */
const USE_MOCK = false;   // F3b：真实接口（mock 分支保留，供离线兜底演示随时切回）

/* =============================================================================
 * 0b. 登录态持久化（卡 r4kJEe）
 * -----------------------------------------------------------------------------
 * 为什么是 localStorage（不是 cookie，也不是 sessionStorage）：
 *   1. auth.py 的 verify_token 只验签名，**不看 iat、没有 exp**，密钥落盘
 *      → 放进 HttpOnly cookie 等于「永久自动登录」，共用演示机更危险；
 *      而且改 cookie 要动鉴权层（Set-Cookie / 读 Cookie / CSRF / 退出接口）。
 *   2. 老师从入口页点链接往往新开标签，sessionStorage 不跨标签 → 每次都要重登，
 *      手感不符（2026-11-13 队长拍板改 localStorage）。
 *   3. XSS 面已压得很小（禁 innerHTML / 零外链 / 零三方脚本）。
 *
 * 🔴 代价（答辩必须如实说）：
 *   · localStorage **明文落盘**（含 login 与 global_role），共用演示机演示后要手动退出；
 *   · 12h / 24h **只是前端自我约束**——后端令牌无 TTL、无吊销接口，
 *     「退出登录」只是本机丢弃，不会让服务端令牌失效。
 *
 * 记录结构（v1，写死）：
 *   {v:1, token, user, groupId, loginAt, exp}
 *   exp = min(now + 12h, loginAt + 24h)   → 活跃即滑动，但登录满 24h 必须重登
 * ========================================================================== */
const AUTH_KEY = 'sk_auth_v1';
const AUTH_SLIDE_MS = 12 * 60 * 60 * 1000;   // 单次有效期（每次成功取数后续期）
const AUTH_ABS_MS = 24 * 60 * 60 * 1000;     // 登录后的绝对上限

/** localStorage 句柄。🔴 隐私模式 / 沙箱 iframe 下**取属性本身**就可能抛 → 一律 try。 */
function _ls() {
  try { return window.localStorage || null; } catch (e) { return null; }
}

/**
 * 存储不可用时的纯内存退化（隐私模式 / 存储被禁）。
 * 🔴 语义：本页仍可正常登录使用，只是**刷新后必然丢**——这比崩掉或谎称已保存好。
 */
let authMem = null;

/** 记录合法性（时间判据全在这里）。⛔ 手改 exp 不能绕过 24h 上限。 */
function authValid(r, now) {
  if (!r || typeof r !== 'object') return false;
  if (r.v !== 1) return false;
  if (typeof r.token !== 'string' || !r.token) return false;
  if (r.user != null && typeof r.user !== 'object') return false;
  if (typeof r.groupId !== 'string') return false;
  if (typeof r.loginAt !== 'number' || !isFinite(r.loginAt)) return false;
  if (typeof r.exp !== 'number' || !isFinite(r.exp)) return false;
  if (r.loginAt <= 0 || r.loginAt > now + 60000) return false;   // 时钟回拨 / 伪造未来时间
  if (r.exp <= r.loginAt) return false;                          // exp 必须晚于 loginAt
  if (r.exp > r.loginAt + AUTH_ABS_MS) return false;             // 伪造远期 exp 也要被 24h 挡住
  return r.exp > now;
}

/**
 * 读登录态。🔴 纯读，**不顺手清**——某标签清除会让其它标签收到 storage 事件、
 * 连锁把所有人踢下线。坏记录留在那里无害，下次登录自然覆盖。
 * 读序：storage 优先（别的标签写的新记录不能被本标签旧内存遮住）；
 *      storage 明确「没有」→ 就是没有（⛔ 不回退内存，否则别的标签退出会被本页复活）。
 */
function readAuth() {
  const now = Date.now();
  const ls = _ls();
  if (ls) {
    let raw;
    try { raw = ls.getItem(AUTH_KEY); } catch (e) { raw = undefined; }   // undefined = 读失败
    if (raw !== undefined) {
      if (raw === null) return null;
      let rec = null;
      try { rec = JSON.parse(raw); } catch (e) { rec = null; }
      return authValid(rec, now) ? rec : null;
    }
  }
  return authValid(authMem, now) ? authMem : null;
}

/** 落盘（失败静默，调用方不因存储问题中断登录）。 */
function _writeAuth(rec) {
  authMem = rec;
  const ls = _ls();
  if (!ls) return false;   // 退化为纯内存
  try { ls.setItem(AUTH_KEY, JSON.stringify(rec)); return true; } catch (e) { return false; }
}

/**
 * 写记录。
 * @param {{keepExpiry?:boolean}} [opts] keepExpiry=true 只更新 groupId，沿用 loginAt/exp
 *        （切组不是活跃信号，不续期）。记录必须存在且是本标签的 token，
 *        ⛔ 否则会把「别的标签刚退出」的记录复活。
 */
function saveAuth(opts) {
  const o = opts || {};
  if (!state.token) return false;
  if (o.keepExpiry) {
    const prev = readAuth();
    if (!prev || prev.token !== state.token) return false;
    return _writeAuth({
      v: 1, token: prev.token, user: prev.user,
      groupId: state.groupId || '', loginAt: prev.loginAt, exp: prev.exp,
    });
  }
  const now = Date.now();
  state.authDeadline = now + AUTH_ABS_MS;
  state.authTouchedAt = now;
  scheduleAuthDeadline();
  return _writeAuth({
    v: 1, token: state.token, user: state.user,
    groupId: state.groupId || '', loginAt: now, exp: now + AUTH_SLIDE_MS,
  });
}

/** 清本机记录（⛔ 服务端令牌不会因此失效——无吊销接口，文案不得写成「安全退出」）。 */
function clearAuth() {
  authMem = null;
  const ls = _ls();
  if (!ls) return;
  try { ls.removeItem(AUTH_KEY); } catch (e) { /* 存储被禁：本页无记录可清 */ }
}

/**
 * 滑动续期：exp = min(now + 12h, loginAt + 24h)。
 * 🔴 三个「不许写」：没有 token / 记录不存在（别的标签已退出）/ 记录 token ≠ 本标签 token。
 * 60 秒节流：12h 窗口秒级精度没意义，顺带少一次磁盘写与跨标签 storage 事件。
 */
function touchAuth() {
  if (!state.token) return false;
  const now = Date.now();
  if (state.authTouchedAt && now - state.authTouchedAt < 60000) return false;
  const prev = readAuth();
  if (!prev || prev.token !== state.token) return false;
  const exp = Math.min(now + AUTH_SLIDE_MS, prev.loginAt + AUTH_ABS_MS);
  if (exp <= now) {
    endSession({ clearStorage: true, msg: '登录已超过 24 小时，请重新登录。' });
    return false;
  }
  const ok = _writeAuth({
    v: 1, token: prev.token, user: prev.user,
    groupId: state.groupId || prev.groupId || '', loginAt: prev.loginAt, exp: exp,
  });
  if (ok) state.authTouchedAt = now;
  return ok;
}

/**
 * 24h 绝对上限的定时器。到点即结束会话（⛔ 不许「一直活跃就永不掉线」）。
 * ⚠️ 后台标签的 setTimeout 会被浏览器节流到分钟级 → 请求层还有一道 authExpiredByDeadline() 兜底。
 */
function scheduleAuthDeadline() {
  clearTimeout(state.authTimer);
  state.authTimer = 0;
  if (!state.token || !state.authDeadline) return;
  const left = state.authDeadline - Date.now();
  if (left <= 0) return;   // 交给下一次请求或 readAuth 判死，避免在这里递归结束会话
  state.authTimer = setTimeout(function () {
    endSession({ clearStorage: true, msg: '登录已超过 24 小时，请重新登录。' });
  }, Math.min(left, 2147483647));
}

/** 请求前兜底检查 24h 上限（覆盖被节流的后台标签）。返回 true 表示会话已结束。 */
function authExpiredByDeadline() {
  if (!state.token || !state.authDeadline) return false;
  if (Date.now() < state.authDeadline) return false;
  endSession({ clearStorage: true, msg: '登录已超过 24 小时，请重新登录。' });
  return true;
}

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
 * 清空「必须登录才能看」的已渲染内容。
 *
 * 🔴 2026-11-13 补（外部复审发现）：401 原先只清 token 与状态条，**不清 DOM** ——
 *    账号被停用/会话失效后，屏幕上仍留着上一个身份的课表、23 处冲突与变更请求。
 *    状态条写着「未登录」、内容区却列着具体课表，这是最坏的一种错数据：
 *    老师会以为「还能看」，评委看到的是已经失效的数据。
 * 原则：**取不到就清空**，绝不留「看起来还能用」的旧数据。
 */
function clearPrivateView() {
  // rTVvb8：公告是「上一个身份」的内容，401 / 退出后必须一起清掉。
  // 与本函数开头 500-504 行记录的教训同类：屏幕上留着旧身份的公告，
  // 是比「空屏」更坏的错数据（老师会以为还能看）。
  state.announcement = null;
  state.annSeq += 1;        // 作废在途的 GET /api/announcements
  state.groupLevels = {};   // 旧身份的组级档位不能留给下一个身份
  closeAnnouncementEditor();
  const annBarNode = $('annBar');
  if (annBarNode) {
    annBarNode.hidden = true;
    annBarNode.classList.remove('is-error', 'is-empty');
  }
  if ($('annTitle')) $('annTitle').textContent = '';
  if ($('annMeta')) $('annMeta').textContent = '';
  if ($('annBody')) clear($('annBody'));

  state.objects = [];
  state.days = [];
  state.hitDates = [];
  state.schedules = null;
  state.conflicts = null;
  state.lastResult = null;
  state.slotTarget = '';
  const placeholders = [
    ['weekBox', '登录后查看课表。'],
    ['confBox', '登录后查看冲突。'],
    ['reqBox', '登录后查看变更请求。'],
    ['resBox', '尚未执行消解。'],
    ['chatBox', '尚未提问。'],
    ['slotBox', '登录后可查询空位。'],
  ];
  placeholders.forEach(function (pair) {
    const node = $(pair[0]);
    if (!node) return;
    clear(node);
    node.appendChild(el('div', { cls: 'empty', text: pair[1] }));
  });
  if ($('slotSum')) setFoldState('slotSum', '尚未查询', '');
  // rrr4vM：临时微调面板持有的是「上一个身份」的日程，401 后必须一起拆掉。
  // 它挂在 body 上，不在 placeholders 里，不显式关闭会留在屏幕上。
  closeExceptionPanel();
  showBanner($('excMsg'), null);
  showBanner($('excErr'), null);
}

/**
 * 身份卡三态（卡 r4kJEe）。🔴 恢复期必须是**中性态**：
 *   记住的 token 已失效时若先亮绿「已登录」再被打回未登录，比不恢复还难看。
 *   只有首个请求批次确认「本会话仍是当前会话」（authOk）后才转绿。
 */
function paintLoggedIn(fallbackName) {
  const sec = $('loginSec');
  if (sec) sec.hidden = false;
  const badge = $('loginBadge');
  if (badge) { badge.className = 'status-badge is-ok'; badge.textContent = '已登录'; }
  const who = $('loginWho');
  if (who) {
    const name = state.user ? str(state.user, 'login', '') : '';
    who.textContent = name || fallbackName || '已登录';
  }
  const out = $('logoutBtn');
  if (out) out.hidden = false;
}

/** 恢复中：中性蓝徽章「正在恢复…」，⛔ 不许 is-ok（验收第 6 条盯的就是这一帧）。 */
function paintRestoring() {
  const sec = $('loginSec');
  if (sec) sec.hidden = false;
  const badge = $('loginBadge');
  if (badge) { badge.className = 'status-badge is-load'; badge.textContent = '正在恢复…'; }
  const who = $('loginWho');
  if (who) {
    const name = state.user ? str(state.user, 'login', '') : '';
    who.textContent = name ? (name + '（正在恢复登录态）') : '正在恢复登录态';
  }
  const out = $('logoutBtn');
  if (out) out.hidden = true;   // 登录未被确认前不给「退出登录」，避免误点
}

/**
 * 结束会话的唯一出口：401 / 退出按钮 / 跨标签退出 / 24h 到点 四路共用。
 * 🔴 为什么必须收口：四套各写一遍，必然漏掉 loginWho、groupSel.dataset.loaded、
 *    旧红条、在途响应渲染中的至少一项（那会留下「已退出但课表还在屏上」）。
 * @param {{clearStorage?:boolean, msg?:string|null}} [opts]
 */
function endSession(opts) {
  const o = opts || {};
  // 先清内容再改状态：避免任何一帧出现「未登录 + 旧课表」
  clearPrivateView();
  if (o.clearStorage) clearAuth();
  state.authEpoch += 1;          // 在途请求的渲染 / 续期全部作废
  state.token = '';
  state.user = null;
  state.conflicts = null;
  state.groupId = '';
  state.objectId = '';
  state.authDeadline = 0;
  state.authTouchedAt = 0;
  clearTimeout(state.authTimer);
  state.authTimer = 0;
  markTaskDone();
  state.statusSeq += 1;
  setStatus('unauth', '登录后显示本周冲突数');
  showLogin(o.msg || null);      // 身份卡 / 退出按钮 / 组下拉的复位都在 showLogin 里
  ['weekErr', 'confErr', 'reqErr', 'fatal'].forEach(function (id) {
    showBanner($(id), null);
  });
  const pwd = $('loginPwd');
  if (pwd) pwd.value = '';
}

/**
 * 401 统一处理：先断开持久化，再清 token / 已渲染的私有数据，弹出登录栏。
 * 🔴 绝不静默当成「空数据」——那会把「没登录」伪装成「没有冲突」。
 * 🔴 403 不走这里：403 是「这个组没权限」，回退组即可（见 loadAll 的组失效回退）。
 */
function handle401(body) {
  // 首行断开持久化：否则刷新后又拿同一个废 token「恢复」→ 弹登录 ⇄ 又登录的死循环
  if (state.token) clearAuth();
  endSession({ msg: '登录已过期或未登录（HTTP 401），请重新登录。' });
  throw new Error(extractMessage(body) || 'HTTP 401 未登录');
}


/** 取查询串里的 group（可空＝隐式模式，走服务端默认）。 */
function withGroup(params) {
  const p = params || {};
  if (state.groupId) p.group = state.groupId;
  return p;
}

async function apiGet(pathname, params) {
  // 24h 硬上限的请求层兜底（后台标签的 setTimeout 会被节流，不能只靠 timer）
  if (authExpiredByDeadline()) throw new Error('登录已超过 24 小时，请重新登录。');
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
    // rrr4vM：把原始响应体挂上——409 的结构化 exception_id 在里面，
    // 前端不必再从 message 字符串里正则抠（抠法留作兜底）。
    err.body = body;
    throw err;
  }
  return body;
}

async function apiPost(pathname, params, payload) {
  // 24h 硬上限的请求层兜底（同 apiGet）
  if (authExpiredByDeadline()) throw new Error('登录已超过 24 小时，请重新登录。');
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
    // rrr4vM：把原始响应体挂上——409 的结构化 exception_id 在里面，
    // 前端不必再从 message 字符串里正则抠（抠法留作兜底）。
    err.body = body;
    throw err;
  }
  return body;
}

/**
 * DELETE 请求（rrr4vM：撤销临时例外用）。
 *
 * 🔴 与 apiGet/apiPost 完全同构，只有两点不同：无 body、方法为 DELETE。
 *    错误处理必须一致（尤其 401 要走进 handle401），否则「登录过期」会变成
 *    一条没人处理的异常，页面上既不清数据也不提示。
 *
 * ⛔ mock 分支不支持 DELETE（mockRoute 只认 GET/POST）：调用方已按 USE_MOCK
 *    不渲染入口，这里再兜一层，避免静默发到一个不存在的 mock 路由。
 */
async function apiDelete(pathname, params) {
  // 24h 硬上限的请求层兜底（同 apiGet）
  if (authExpiredByDeadline()) throw new Error('登录已超过 24 小时，请重新登录。');
  if (USE_MOCK) {
    throw new Error('离线演示（USE_MOCK）不支持 DELETE：本功能需连真实服务');
  }
  const url = new URL(pathname, window.location.origin);
  Object.keys(params || {}).forEach(function (k) {
    if (params[k] !== '' && params[k] != null) url.searchParams.set(k, params[k]);
  });
  const res = await fetch(url.toString(), {
    method: 'DELETE',
    headers: authHeaders({ Accept: 'application/json' }),
  });
  const body = await readBody(res);
  if (!res.ok) {
    if (res.status === 401) handle401(body);   // handle401 内部必 throw
    const err = new Error(extractMessage(body) || ('HTTP ' + res.status + ' ' + res.statusText));
    err.status = res.status;
    err.body = body;           // 同上：DELETE 的错误也可能是结构化的
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
  if (typeof t !== 'string' || t === '') return '求解未结束';
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

/**
 * 🔴 掩码一律由服务端给（AGENTS §3.4「前端不做位运算」）。
 *    - 周视图 grid 的掩码 → dayMaskPeriods() 只做**反解**（读位，不造值）；
 *    - 「改到第 N 节」的目标掩码 → 直接取 detail.periods[].new_mask（服务端算好的
 *      单日掩码，与 cells[].mask 同格式）。
 *    2026-11-13 前这里有个 apiMaskForPeriod(idx) 用 Math.pow 自己造掩码，
 *    在 periods 补了 new_mask 之后已删除 —— 前端不再有任何「造掩码」的代码。
 */

/**
 * 格子里的课程名：去掉「高一(1)班 」这类对象名前缀。
 * 周视图整列都是同一个对象，标题里再带一遍班名纯属冗余，还会把课程名挤没。
 */
function shortTitle(title, objName) {
  const t = String(title || '');
  if (objName && t.indexOf(objName + ' ') === 0) {
    const rest = t.slice(objName.length + 1).trim();
    return rest || t;
  }
  return t;
}

function renderWeek(schedules, conflicts) {
  const box = $('weekBox');
  const errBox = $('weekErr');
  clear(box);

  const days = arr(schedules, 'days');
  if (!days.length) {
    showBanner(errBox, '本周日期没加载出来，请点「重新加载」。');
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
  // 🔴 2026-11-13：每格的**课程名**。<对象>|<日期> → ["语文","数学"]（后端纯追加字段）。
  //    没有它，格子只能写对象名（整列都一样），老师会以为课表没加载。
  const titles = (schedules && typeof schedules.titles === 'object' && schedules.titles) || {};
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
      cell.texts.push(sideShort(it.a) + ' × ' + sideShort(it.b));
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
      // 🔴 2026-11-13 两条修正（实测截图发现）：
      //   1) 普通格：原来固定写 `objName`，而周视图是「单个对象的课表」，整列对象名
      //      都一样 → 每格只显示「高一(1)班」四个字，看不到上的是什么课。改显示课程名。
      //   2) 冲突格：**不再写课程名**。冲突对的文字里已经含日程名，再叠一层课程名
      //      会让同一门课在格子里出现两三次，把行高撑到下一行去（截图实测）。
      const cellTitles = hit ? [] : (titles[objId + '|' + days[di] + '|' + p] || []);
      if (cellTitles.length) {
        // 一格最多列 2 门：再多的挤不下，用「另 N 门」显式指路（不静默丢）
        cellTitles.slice(0, 2).forEach(function (t) {
          td.appendChild(el('span', { cls: 'who', text: shortTitle(t, objName) }));
        });
        if (cellTitles.length > 2) {
          td.appendChild(el('span', { cls: 'who', text: '另 ' + (cellTitles.length - 2) + ' 门' }));
        }
      } else if (!hit) {
        // 空白兜底：没有课程名又没有冲突定位（旧后端 / 定不到位）时才写对象名。
        // 冲突格不写 —— 它的信息由下面的 vs 承担，再写对象名就是第三种重复。
        td.appendChild(el('span', { cls: 'who', text: objName || '（占用）' }));
      }
      if (hit) {
        // 同一格可能撞多条，去重后逐条列出（.length 驱动，不写死条数）
        const seen = [];
        hit.texts.forEach(function (t) { if (seen.indexOf(t) < 0) seen.push(t); });
        // 窄格里最多列 2 条：其余用一句「另 N 处」显式指路，⛔ 不静默丢掉
        seen.slice(0, 2).forEach(function (t) { td.appendChild(el('span', { cls: 'vs', text: t })); });
        if (seen.length > 2) {
          td.appendChild(el('span', {
            cls: 'vs',
            text: '另 ' + (seen.length - 2) + ' 处（见下方冲突列表）',
          }));
        }
      }
      // ---- rrr4vM：临时微调入口 ----
      // 🔴 两个入口都要：常显的「⋯」按钮 + 双击快捷方式。
      //    只做双击＝功能不可发现（老师不知道能双击），卡面明令两个缺一不可。
      // ⛔ USE_MOCK 下不挂：mockRoute 不支持 /api/schedules/detail 与 DELETE，
      //    点出去必然 404，等于给离线演示埋一个假按钮。
      const cellDate = days[di];
      if (!USE_MOCK && objId && typeof cellDate === 'string' && cellDate) {
        const more = el('button', {
          cls: 'cellmore',
          text: '⋯',
          attrs: {
            type: 'button',
            title: '临时微调：取消 / 改到别的节次',
            'aria-label': cellDate + ' 第' + p + '节 临时微调',
          },
        });
        more.addEventListener('click', function (ev) {
          ev.stopPropagation();
          openExceptionPanel({ objectId: objId, date: cellDate, idx: p });
        });
        td.appendChild(more);
        // 双击整格 = 同一个面板（快捷方式）。stopPropagation 挡不住 dblclick，
        // 所以这里要显式排除按钮本身，否则双击「⋯」会开两次面板。
        td.addEventListener('dblclick', function (ev) {
          if (ev.target && ev.target.closest && ev.target.closest('.cellmore')) return;
          openExceptionPanel({ objectId: objId, date: cellDate, idx: p });
        });
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

/**
 * 周视图格子里的紧凑标签：只取对象名。
 * 🔴 为什么不复用 sideTitle()：三栏改版后中栏只有 ~700px，一条冲突用完整标题
 *    （「学生1-09（冲突演示）高一(1)班 物理 × 学生1-09 全年级大会」）会把一个格子
 *    撑成 6 行，整张表被顶长一倍。完整标题在右侧「冲突列表」里一字不少，
 *    这里只负责「这一格谁和谁撞了」。
 */
function sideShort(side) {
  // 优先用 title（「物理」「全年级大会」这种"事项"），并去掉开头的括注前缀
  // （如「（冲突演示）」——那是标签不是内容）。取不到再退回对象名。
  const t = str(side, 'title', '').replace(/^[（(][^）)]*[）)]\s*/, '');
  if (t) return t;
  return str(side, 'object_name', '') || '（对象未知）';
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

  // 原始冲突处数：取不到就不显示这一格（不在界面上暴露「字段缺失」这种开发期信息）
  if (counts.raw !== null) {
    const rStat = el('div', { cls: 'stat' });
    rStat.appendChild(el('b', { text: String(counts.raw) }));
    rStat.appendChild(el('span', { text: ' 处原始冲突' }));
    stats.appendChild(rStat);
  }

  box.appendChild(stats);

  // ---- 一句话说清两个数字的关系 ----
  //     答辩时评委会问「为什么上面写 3、下面写 23」：实时给一句对照即可，
  //     完整的聚合口径与接口来源收在页面底部「技术说明」折叠区（展开才是原话）。
  //     🔴 这两个数字都来自响应，本页不产生、不推断任何数字。
  showBanner($('resolveHint'),
    counts.raw === null
      ? null
      : '「' + counts.groups + ' 组」＝合并后；「' + counts.raw + ' 处」＝合并前。');

  // ---- 列表 ----
  const items = arr(counts, 'items');
  if (!items.length) {
    box.appendChild(el('div', {
      cls: 'empty',
      text: '本周没有冲突。',
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
  // 两个动作收进一组、统一右对齐：原来「提议改期 | 一键消解」和左侧文字三分天下，
  // 中间那个按钮看着像悬空。
  const acts = el('div', { cls: 'it-acts' });
  const proposeBtn = el('button', { text: '提议改期', attrs: { type: 'button' } });
  proposeBtn.addEventListener('click', function () { openProposeModal(conflictTargets(item)); });
  acts.appendChild(proposeBtn);

  // rrr4vM：第二个入口——冲突条目也能直接唤起「临时微调」面板。
  // 为什么必须要有它：周视图格子的入口只能覆盖「当前查看对象」的占用格，
  // 而冲突列表覆盖全部冲突点；且 renderWeek 有早退路径（本周无占用）时一个格子都没有。
  if (!USE_MOCK) {
    const excBtn = el('button', { text: '临时微调', attrs: { type: 'button' } });
    excBtn.addEventListener('click', function () {
      const anchor = conflictAnchor(item);
      if (!anchor) {
        // 不猜、不静默：定位不到就明说，绝不用一个假 id 去发请求
        showBanner($('confErr'), '这条冲突没有可定位的对象/日期，无法打开临时微调。');
        return;
      }
      openExceptionPanel(anchor);
    });
    acts.appendChild(excBtn);
  }

  // 🔴 2026-11-12：删掉条目内的「一键消解」。
  //    它和标题栏那个按钮发的是**同一条** POST /api/resolve（data-idx 从未被读取，
  //    doResolve(btn) 也不看 btn）—— 纯重复入口。三条冲突 = 三个红按钮，
  //    老师第一眼就在"点哪一个"上犹豫。按条处理请用「提议改期」。
  top.appendChild(acts);
  li.appendChild(top);

  // ---- 中部：a × b ----
  const vs = el('div', { cls: 'it-vs' });
  vs.appendChild(renderSide(item.a));
  vs.appendChild(el('span', { cls: 'vs-mark', text: '撞' }));
  vs.appendChild(renderSide(item.b));
  li.appendChild(vs);

  // 2026-11-12：条目级的「节次编码」行已删 —— a / b 两侧各自带「节次 0xN · 日期」，
  // 这一行永远与两侧重复，三条冲突白占 3 行高度。要查总掩码看两侧即可。

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
  // 2026-11-12：原来比的是 title !== sideTitle()，而 sideTitle 在多数数据里就是
  // 「对象名 + 空格 + title」→ 永远不等 → 标题被打印两遍。改成「nm 行里没含 title 才补」。
  if (title && sideTitle(side).indexOf(title) < 0) {
    box.appendChild(el('div', { cls: 'ti', text: title }));
  }

  // 🔴 slots 一律原样显示，绝不做字符串比较（AGENTS §9b：长度不定，比字符串会静默漏匹配）
  // 2026-11-12：节次编码与日期并成一行 —— 整行宽版里一条冲突原来要 4 行副信息，
  // 并成 2 行后三条冲突合计省出 ~80px，第一屏能多露出一条。
  const s = str(side, 'slots', '');
  const d = str(side, 'date', '');
  if (s || d) {
    const m = el('div', { cls: 'mt' });
    if (s) {
      m.appendChild(el('span', { text: '节次 ' }));
      m.appendChild(el('code', { text: s }));
    }
    if (d) m.appendChild(el('span', { text: (s ? ' · ' : '') + d }));
    box.appendChild(m);
  }
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
    box.appendChild(el('div', { cls: 'empty', text: '没拿到结果，请重试。' }));
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

  // 明细行：某个数没给出来就不显示那一行（不把「字段缺失」这种开发期信息写上屏幕）
  const kv = el('div', { cls: 'kv' });
  [
    ['已应用方案数', applied],
    ['迭代轮数', rounds],
    ['因不在你的可改范围而未尝试', filtered],
  ].forEach(function (pair) {
    if (pair[1] === null) return;
    kv.appendChild(el('div', { text: pair[0] + '：' + String(pair[1]) }));
  });
  if (kv.firstChild) box.appendChild(kv);

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
    // 🔴 2026-11-10：`missing` 必须参与文案。
    //    原文案无条件写「逐字段等于消解前」—— 但后端在「消解后该日程被别人删掉了」
    //    时会返回 missing>0，此时并未完全还原。照旧文案就是**让界面撒谎**，
    //    正是本项目最痛的静默失效形态（只是这次落在提示文字上）。
    const missing = intOrNull(body, 'missing');
    const missN = (missing === null ? 0 : missing);
    const exact = (missN === 0);
    clear($('resBox'));
    $('resBox').appendChild(el('div', {
      cls: exact ? 'banner ok' : 'banner warn',
      text: '已还原：冲突回到 ' + (after === null ? '?' : String(after)) + ' 处'
        + '（回写 ' + (restored === null ? '?' : String(restored)) + ' 条日程'
        + (exact ? '，逐字段等于消解前）'
                 : '；另有 ' + String(missN) + ' 条日程已不存在，未能还原）')
        + ((body && body.already_reverted) ? '。这次撤销先前已经做过，本次没有改动数据。' : ''),
    }));
    openFold('resDetails');
    setFoldState('resSum', '已撤销，冲突回到 ' + (after === null ? '?' : String(after)) + ' 处', 'ok');
  } catch (e) {
    if (btn) btn.disabled = false;
    clear($('resBox'));
    $('resBox').appendChild(el('div', {
      cls: 'banner error',
      text: '撤销失败：' + httpErrText(e) + '（409 = 该版本已固化，需管理员重新消解）',
    }));
    openFold('resDetails');
    setFoldState('resSum', '撤销失败，见详情', 'error');
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
      text: '已固化，之后不能再撤销。',
    }));
    openFold('resDetails');
    setFoldState('resSum', '已固化此版本', 'ok');
  } catch (e) {
    if (btn) btn.disabled = false;
    clear($('resBox'));
    $('resBox').appendChild(el('div', {
      cls: 'banner error',
      text: '固化失败：' + httpErrText(e) + '（403 = 需要管理员或 own 档权限）',
    }));
    openFold('resDetails');
    setFoldState('resSum', '固化失败，见详情', 'error');
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
  token: '',         // F3b：登录后的 Bearer token（内存副本；落盘见 AUTH_KEY，12h/24h）
  user: null,        // /api/login 返回的 user 对象（仅用于显示登录身份）
  authDeadline: 0,   // r4kJEe：loginAt + 24h（ms）。请求层与定时器共用，到点必须重登
  authTouchedAt: 0,  // r4kJEe：上次续期时刻（60s 节流，避免每次取数都写盘 + 跨标签事件）
  authTimer: 0,      // r4kJEe：24h 上限的定时器 id
  authEpoch: 0,      // r4kJEe：会话代次。退出/401/超时 +1，用来作废在途请求的渲染
  loggingIn: false,  // r4kJEe：登录请求互斥（回车与按钮会并发触发 doLogin）
  groupFallbackPending: false,  // r4kJEe：403 组失效回退后待提示一次
  sessionId: '',     // F5：会话级随机串（埋点平均时长用，boot() 时生成一次）
  slotTarget: '',    // F7 secSlots：最近一次「查空位」用的对象 id（按此提议时对应）
  taskActive: false, // F5：task_start 已发、尚未 success/abandon
  taskStartedAt: 0,  // F5：task_start 时刻（ms）
  taskTimer: 0,      // F5：90 秒无操作超时定时器 id
  status: 'unauth',  // runQn7：首屏状态条四态 unauth / loading / ok / error
  statusSeq: 0,      // runQn7：状态写入序号（旧请求后返回时不覆盖新状态）
  resolving: false,  // runQn7：消解进行中（两个「一键消解」入口共用一把锁）
  // rrr4vM：临时微调面板句柄（null = 未打开）。挂 body，不随周视图重画销毁，
  // 所以关闭必须显式：切组/切周/重载(loadAll) / 401(clearPrivateView) / 用户点关闭。
  excPanel: null,
  excBusy: false,    // 面板内的动作锁：连点两次会得到 409，也浪费一次写入
  // rTVvb8：组织公告
  announcement: null,   // 最近一次 GET /api/announcements 的 announcement；null = 无公告
  annSeq: 0,            // 请求代次：切组 / 退出后，旧响应不能覆盖新状态
  groupLevels: {},      // group_id -> /api/groups 的 level（有效档位，含继承与天花板）
  annEditor: null,      // 公告编辑弹层句柄（null = 未打开）
  annSaving: false,     // 保存 / 停用互斥，防连点
  annPreviewTimer: 0,   // 实时预览防抖定时器
};

const $ = function (id) { return document.getElementById(id); };

/* =============================================================================
 * 8b. 首屏状态条 + 折叠区状态字（卡 runQn7）
 * -----------------------------------------------------------------------------
 * 🔴 数字只有一个来源：GET /api/conflicts 的响应。前端一处都不写死。
 * 🔴 接口失败 → error 态（「冲突没加载出来，请点重新加载」），⛔ 绝不显示 0：
 *    0 会被老师读成「本周没问题」，而实际情况是「没取到」—— 这正是本项目
 *    反复踩的那类静默失效（把失败伪装成正常结论）。
 * 🔴 raw_count / count 都缺 → 也算 error，⛔ 不猜、不用 items.length 合成 0
 *    （readCounts() 会那样兜底，所以这里**不复用**它，直接读原始字段）。
 * ========================================================================== */

const STATUS_BADGE = {
  unauth:  { cls: 'is-warn', text: '未登录' },
  loading: { cls: 'is-load', text: '加载中' },
  ok:      { cls: 'is-ok',   text: '已更新' },
  error:   { cls: 'is-err',  text: '取数失败' },
};

/**
 * 写状态条。数字单独放大（<b>）便于 1 米外读到，其余文字走 textContent。
 * @param kind 'unauth' | 'loading' | 'ok' | 'error'
 * @param lead 数字前的文字（如「本周 」）
 * @param num  数字（null / undefined 表示没有数字）
 * @param tail 数字后的文字（如「 处冲突」）
 */
function setStatus(kind, lead, num, tail) {
  const key = STATUS_BADGE[kind] ? kind : 'error';
  state.status = key;
  const b = STATUS_BADGE[key];
  const badge = $('statusBadge');
  if (badge) {
    badge.className = 'status-badge ' + b.cls;
    badge.textContent = b.text;
  }
  const t = $('statusText');
  if (t) {
    clear(t);
    if (lead) t.appendChild(el('span', { text: lead }));
    if (num !== undefined && num !== null) t.appendChild(el('b', { text: String(num) }));
    if (tail) t.appendChild(el('span', { text: tail }));
  }
  syncResolveDisabled();
}

/**
 * 从 /api/conflicts 响应里取「处 / 组」计数。
 * raw_count 优先（「23 处」是聚合前条数，与页面其它口径一致）；缺失才用 count。
 * 两者都缺 → null（调用方按 error 处理，⛔ 不当 0）。
 */
function conflictCounts(body) {
  const raw = intOrNull(body, 'raw_count');
  if (raw !== null) return { n: raw, unit: ' 处冲突' };
  const groups = intOrNull(body, 'count');
  if (groups !== null) return { n: groups, unit: ' 组冲突' };
  return null;
}

/** 两个「一键消解」入口（首屏状态条 / 冲突列表）共用同一套禁用规则。 */
function syncResolveDisabled() {
  const cnt = (state.status === 'ok') ? conflictCounts(state.conflicts) : null;
  const off = state.resolving || cnt === null || !(cnt.n > 0);
  ['resolveBtn', 'resolveBtnTop'].forEach(function (id) {
    const b = $(id);
    if (b) b.disabled = off;
  });
}

/** 折叠区标题行的常显状态字（收起时也要能看见「有没有结果 / 有没有失败」）。 */
function setFoldState(id, text, kind) {
  const n = $(id);
  if (!n) return;
  n.className = 'fold-state' + (kind ? ' is-' + kind : '');
  n.textContent = text || '';
}

/** 打开折叠区（写结果 / 写错误之后必须调用，否则「做完了却看不见」）。 */
function openFold(id) {
  const d = $(id);
  if (d) d.open = true;
}

/* ── F3b 登录闭环：POST /api/login → 存 token → 首次 loadAll ──────────────
 * 为什么必须有这一步：B3/B4 接入鉴权后未登录一律 401（docs/07 通用约定），
 * mock 时代不需要；登录本身不需要 token（docs/07 §1）。
 * ---------------------------------------------------------------------- */
function showLogin(msg) {
  const sec = $('loginSec');
  if (sec) sec.hidden = false;
  const form = $('loginForm');
  if (form) form.hidden = false;
  // runQn7：未登录从「灰色小字」改成黄色 Badge（投影下才读得出来）
  const badge = $('loginBadge');
  if (badge) { badge.className = 'status-badge is-warn'; badge.textContent = '未登录'; }
  // r4kJEe：身份卡上的「上一个身份」必须一起清掉 —— 否则未登录却写着 admin
  const who = $('loginWho');
  if (who) who.textContent = '登录后可查看课表';
  const out = $('logoutBtn');
  if (out) out.hidden = true;      // 未登录时的「退出登录」是纯误导
  const sel = $('groupSel');
  if (sel) {                       // 换身份后授权组可能不同，强制重拉，且不留旧选中值
    delete sel.dataset.loaded;
    sel.value = '';
  }
  // 无消息时也要清：否则上一轮的 401 红字会留在「未登录」旁边（静默过期尤其明显）
  showBanner($('loginErr'), msg || null);
}

/** 登录成功：只收起表单行，loginSec 与身份提示保留（现场要能看出登录身份）。 */
function hideLogin() {
  const form = $('loginForm');
  if (form) form.hidden = true;
  showBanner($('loginErr'), null);
}

async function doLogin() {
  // r4kJEe：回车与按钮会并发触发（keydown 不看 disabled）→ 两个请求两个 token，
  // 最后落盘的记录可能「token 来自 A、loginAt 来自 B」，必须互斥。
  if (state.loggingIn) return;
  const name = $('loginName').value.trim();
  const pwd = $('loginPwd').value;
  if (!name || !pwd) {
    showBanner($('loginErr'), '请输入登录名与密码。');
    return;
  }
  const btn = $('loginBtn');
  state.loggingIn = true;
  btn.disabled = true;
  const epoch = state.authEpoch;   // 登录在途时用户点了退出 / 会话超时 → 晚到响应必须丢弃
  let ok = false;
  try {
    const body = await apiPost('/api/login', {}, { login: name, password: pwd });
    if (epoch !== state.authEpoch) return;
    state.token = str(body, 'token', '');
    state.user = (body && typeof body.user === 'object' && body.user) ? body.user : null;
    if (!state.token) {
      showBanner($('loginErr'), '登录失败：没拿到登录凭证，请重试。');
      return;
    }
    const gid = str(body, 'default_group_id', '');
    if (gid) state.groupId = gid;   // 默认组＝该用户所在的最高组（契约 §1，队长 10-01 定）
    $('loginPwd').value = '';
    // 🔴 先落盘再取数：写失败不影响登录（本页仍用内存 token），只是刷新后要重登
    saveAuth();
    paintLoggedIn(name);
    hideLogin();
    delete $('groupSel').dataset.loaded;   // 换账号后授权组可能不同，强制重拉组列表
    ok = true;
  } catch (e) {
    if (epoch !== state.authEpoch) return;
    // 🔴 2026-11-10 文案纠正：原文写「连续失败 5 次将锁定 15 分钟」——
    //    实际实现是**组合锁 60 秒**（api_login._LOCK_SECONDS），不是 15 分钟。
    //    按错文案，老师会白等 15 分钟；且不该把限流阈值透露给使用者。
    //    统一口径：只提示「会短暂锁定 / 等 1 分钟」，与后端 message 一致。
    showBanner($('loginErr'), '登录失败：' + e.message +
      '（请检查大小写与首尾空格；多次失败会短暂锁定，等 1 分钟再试）');
    // runQn7：没登上就不该留着任何冲突数字
    state.conflicts = null;
    setStatus('unauth', '未登录：登录后显示本周冲突数');
  } finally {
    state.loggingIn = false;
    btn.disabled = false;
  }
  // 🔴 取数放在 try 之外：loadAll 失败不能显示成「登录失败」，
  //    否则会出现「提示登录失败，但本机其实已有有效凭证」的自相矛盾。
  if (ok) await loadAll();
}

/** 组切换器。只列有授权的组——后端不返回未授权的组，前端不做灰显（契约 §2）。 */
function renderGroupOptions(groups) {
  const sel = $('groupSel');
  clear(sel);
  // rTVvb8：顺手记住每组对我而言的**有效档位**（含继承与天花板）。
  // 公告的「编辑入口」就靠它判断 —— ⛔ 不要改用 global_role==='admin'：
  // 权限是按组的，而且教师即便被授 own 也封顶到 propose。
  state.groupLevels = {};
  const list = Array.isArray(groups) ? groups : [];
  if (!list.length) {
    sel.appendChild(el('option', { text: '（没有你有授权的组）', attrs: { value: '' } }));
    return;
  }
  list.forEach(function (g) {
    const id = str(g, 'group_id', '');
    // 左栏只有 260px：用「名称 · 权限 · 人数」的最短写法，长名称靠 select 自身截断，
    // 完整信息挂在 title 上（鼠标悬停可读，不占版面）。
    const bits = [str(g, 'name', '(未命名组)')];
    const parent = str(g, 'parent_name', '');
    if (parent) bits.push('隶属 ' + parent);
    const level = str(g, 'level', '');
    if (id && level) state.groupLevels[id] = level;   // rTVvb8：公告入口判据
    if (level) bits.push(level);   // 左栏窄：写 own 而不是「权限 own」，完整说明在 title 里
    const mc = intOrNull(g, 'member_count');
    if (mc !== null) bits.push(mc + '人');
    const o = el('option', { text: bits.join(' · '), attrs: { value: id, title: bits.join(' · ') } });
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

/**
 * 空位查询的对象下拉（secSlots 专用，与周视图的 objSel 互不影响）。
 * 数据源与 objSel 同源（/api/schedules 的 objects，契约 §4），不另打 /api/objects：
 * 现场只有一个真服务，少一次请求就少一处失败点。
 * 切换组 / 重新加载时**保留用户已选的对象**（若它还在列表里），避免手一抖被重置。
 */
function renderSlotObjectOptions(objects) {
  const sel = $('slotObjSel');
  if (!sel) return;
  const keep = sel.value;
  clear(sel);
  const list = Array.isArray(objects) ? objects : [];
  if (!list.length) {
    sel.appendChild(el('option', { text: '（无对象）', attrs: { value: '' } }));
    return;
  }
  list.forEach(function (o) {
    const id = str(o, 'id', '');
    const bits = [str(o, 'name', '(未命名)')];
    const k = str(o, 'kind', '');
    if (k) bits.push(kindLabel(k));
    sel.appendChild(el('option', { text: bits.join(' · '), attrs: { value: id } }));
  });
  // 优先级：上次选的（仍在列表里）> 当前组 > 第一个 —— 绝不设成不存在的 option
  const ids = list.map(function (o) { return str(o, 'id', ''); });
  if (keep && ids.indexOf(keep) >= 0) sel.value = keep;
  else if (state.groupId && ids.indexOf(state.groupId) >= 0) sel.value = state.groupId;
  else sel.value = ids[0];
}

/**
 * 空位表单的缺省值：日期取 /api/schedules 的 days（服务端事实），不自己算周几。
 * 🔴 用户已经填过的值不覆盖（否则每次重新加载都把手输的日期冲掉）。
 */
function syncSlotForm() {
  renderSlotObjectOptions(state.objects);
  const days = Array.isArray(state.days) ? state.days : [];
  if (days.length) {
    if (!$('slotFrom').value && typeof days[0] === 'string') $('slotFrom').value = days[0];
    const last = days[days.length - 1];
    if (!$('slotTo').value && typeof last === 'string') $('slotTo').value = last;
  }
}

/**
 * 403 只在「记住的组授权被收回」时回退，⛔ 绝不登出（401 才是登出）。
 * 回退目标：清掉 groupId 与组列表缓存 → 重跑后由 /api/groups 落到第一个有授权的组。
 * 提示在拿到组名之后发（`loadAll` 里），文案写**实际组名**——⛔ 不写「默认组」：
 * 服务端默认组与列表首项不保证同一个，写错就成了本项目最忌的「看着 A 实际 B」。
 * 一次性由 `loadAll(retried)` 形参控制，⛔ 不用模块级标志（否则换账号后永不回退）。
 */
function fallbackGroup403() {
  state.groupId = '';
  state.groupFallbackPending = true;
  const sel = $('groupSel');
  if (sel) { sel.value = ''; delete sel.dataset.loaded; }
}

/**
 * 一次拉齐三接口并渲染。任一失败 → 该区块显式报错，不白屏、不伪装成「无数据」。
 *
 * r4kJEe 追加三条边界：
 *   1) `retried`：403（组授权被收回）一次性回退，不把人踢下线；
 *   2) `sessionOk()` 会话门：退出 / 401 / 超时之后，在途响应的渲染与续期一律作废；
 *   3) 续期只在**取数成功**后做（失败续期会让废 token 无限续命）。
 * @param {boolean} [retried] 是否已因 403 回退过一次
 * @returns {Promise<boolean>} 本批次是否「仍是当前会话且取数成功」
 */
async function loadAll(retried) {
  const fatal = $('fatal');
  showBanner(fatal, null);
  // rrr4vM：loadAll 是「切组 / 切周 / 重新加载 / 登录 / 消解后」的共同入口，
  // 它会重建整张周视图与冲突列表 → 面板里的上下文（对象、日期、日程）当场作废，
  // 必须在这里统一关闭，而不是让 renderWeek 去关（成功路径用的是轻量刷新，不关面板）。
  closeExceptionPanel();

  // ---- runQn7：状态条进入「加载中」；seq 用来挡住「旧请求后返回覆盖新状态」----
  const seq = ++state.statusSeq;
  const batchToken = state.token;        // r4kJEe：本批次属于哪个会话
  const batchEpoch = state.authEpoch;
  const applyStatus = function (kind, lead, num, tail) {
    if (seq !== state.statusSeq) return;   // 已有更新的加载批次 → 本次结果作废
    setStatus(kind, lead, num, tail);
  };
  // 会话门：token 没被清、没换过账号、没退出过，且没有更新批次接管
  const sessionOk = function () {
    return !!batchToken && state.token === batchToken &&
      state.authEpoch === batchEpoch && seq === state.statusSeq;
  };
  if (state.token) applyStatus('loading', '正在检查本周冲突…');
  else applyStatus('unauth', '登录后显示本周冲突数');

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
      const remembered = state.groupId;
      if (ids.indexOf(remembered) < 0) {
        state.groupId = ids.length ? ids[0] : '';
        if (remembered) state.groupFallbackPending = true;   // 记住的组被收回 → 提示一次
      }
      $('groupSel').value = state.groupId;
      if (state.groupFallbackPending) {
        state.groupFallbackPending = false;
        const picked = list.filter(function (g) {
          return str(g, 'group_id', '') === state.groupId;
        })[0];
        showBanner($('excMsg'), '之前的组已无权限，已切回「' +
          (picked ? str(picked, 'name', state.groupId) : state.groupId) + '」。');
      }
    } catch (e) {
      // 401 已由 handle401 清场（屏已清、登录栏已弹）：⛔ 不再补一条红字
      if (!state.token) return false;
      // 🔴 原来这里直接 return，状态条会永远停在「加载中」——
      //    提前退出前必须把 error 态写出来（runQn7）。
      applyStatus('error', '组列表加载失败：' + e.message);
      showBanner(fatal, '组列表加载失败：' + e.message + '（无法切换组，请确认服务已启动）');
      renderGroupOptions([]);
      // r4kJEe：恢复路径必须有终态，⛔ 不许停在「正在恢复…」（断网启动必现）
      if (sessionOk()) paintLoggedIn();
      return false;
    }
  }
  state.groupId = $('groupSel').value || state.groupId;

  // rTVvb8：公告与周视图互不依赖。
  // 🔴 必须放在这里，不能放 loadAll 末尾 —— 后面的 schedules / conflicts 任一失败
  //    都会提前 return，放末尾就等于「公告永远不刷新」。
  // 不 await：公告失败不能拖垮主流程，函数内部自带错误态与代次守卫。
  refreshAnnouncement();

  // ---- 2. 周视图数据 ----
  let schedules = null;
  let conflicts = null;
  const params = withGroup({});
  const weekVal = $('weekSel').value.trim();
  if (weekVal) params.week = weekVal;

  try {
    schedules = await apiGet('/api/schedules', params);
  } catch (e) {
    // 401 已由 handle401 清场（token 已被清空）：本批次到此为止，
    // 不再对后面两个接口各吃一次 401、各弹一次登录框。
    if (!state.token) return false;
    if (!sessionOk()) return false;
    // 403 ＝ 这个组没权限（不是登出）：回退一次再重跑
    if (e.status === 403 && !retried) {
      fallbackGroup403();
      return loadAll(true);
    }
    showBanner($('weekErr'), '周视图加载失败：' + e.message);
    clear($('weekBox'));
    $('weekBox').appendChild(el('div', { cls: 'empty', text: '周视图数据未取到（不是「本周没课」）。' }));
  }

  try {
    conflicts = await apiGet('/api/conflicts', withGroup({}));
  } catch (e) {
    if (!state.token) return false;
    if (!sessionOk()) return false;
    if (e.status === 403 && !retried) {
      fallbackGroup403();
      return loadAll(true);
    }
    // 🔴 401 已在 handle401 里把状态条改成「未登录」并把 statusSeq +1；
    //    这里不能再写 error（会把「未登录」覆盖成「取数失败」）。
    applyStatus('error', '冲突没加载出来，请点「重新加载」');
    showBanner($('confErr'), '冲突列表加载失败：' + e.message);
    clear($('confBox'));
    $('confBox').appendChild(el('div', {
      cls: 'empty',
      text: '冲突没加载出来，请点「重新加载」。',
    }));
  }

  // ---- runQn7：状态条结论（数字只从响应读；两字段都没有 → error，不是 0）----
  if (conflicts) {
    const cnt = conflictCounts(conflicts);
    if (cnt) applyStatus('ok', '本周 ', cnt.n, cnt.unit);
    else applyStatus('error', '冲突数没取到（接口未返回 count / raw_count）');
  } else if (state.token) {
    applyStatus('error', '冲突没加载出来，请点「重新加载」');
  }

  // ---- 3. 渲染（有数据才渲染，缺数据保留上面的错误提示）----
  // 🔴 会话门也管渲染：退出后晚到的 200 响应绝不能把课表画回已清空的屏上。
  if (!sessionOk()) return false;
  if (schedules) {
    state.days = arr(schedules, 'days');
    renderObjectOptions(arr(schedules, 'objects'));
    // F7 secSlots：对象下拉同源，并给日期范围一个服务端给出的缺省值
    syncSlotForm();
  }
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

  // runQn7：两个「一键消解」入口共用同一套禁用规则（数字来自响应，0 则禁用）
  syncResolveDisabled();

  // ---- 4. 变更请求（F7/B8）：org 级列表，登录/切组/重载后都刷新 ----
  const reqOk = await loadRequests();

  const authOk = sessionOk();
  const dataOk = !!(schedules && conflicts);
  if (authOk) {
    // loadAll 可能把无效的 groupId 改成了授权列表首项 → 把修正值写回记录（不续期）
    saveAuth({ keepExpiry: true });
    // 恢复路径的终态：身份已知就转绿（取数失败也要转，⛔ 不许停在「正在恢复…」）
    paintLoggedIn();
  }
  // 🔴 只有真的取到数据才续期：断网 / 接口挂掉时续期 = 让废 token 无限续命
  if (authOk && dataOk && reqOk) touchAuth();
  return authOk && dataOk;
}

/* 求解等待态：loader 是 index.html 里内联的纯 SMIL 循环动画，这里只负责显示/隐藏。
   用 hidden 属性（页面末尾有 [hidden]{display:none !important} 兜底），不写 style.display——
   后者会被内联样式残留，且「设 '' 也不一定回到原来的 display」。
   元素缺失时不抛错：这属于纯装饰，不该让它把消解主流程带崩。 */
function showSolveWait(on) {
  const box = $('solveWait');
  if (box) { box.hidden = !on; }
}

/* runQn7：两个入口的原始文案（执行中统一显示「消解中…」，结束后各自还原）。 */
const RESOLVE_LABEL = { resolveBtn: '一键消解（当前组）', resolveBtnTop: '一键消解' };

function setResolveBusy(on) {
  Object.keys(RESOLVE_LABEL).forEach(function (id) {
    const b = $(id);
    if (!b) return;
    b.textContent = on ? '消解中…' : RESOLVE_LABEL[id];
    if (on) b.disabled = true;
  });
  if (!on) syncResolveDisabled();   // 结束后按「当前是否有冲突」决定能否再点
}

async function doResolve(btn) {
  // 两个入口 + 列表内按钮可能被连点：一把锁挡住重复提交
  if (state.resolving) return;
  state.resolving = true;

  const btns = document.querySelectorAll('#confBox button.danger');
  btns.forEach(function (b) { b.disabled = true; });
  setResolveBusy(true);
  showSolveWait(true);

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
    // runQn7：结果在折叠区内 → 必须展开 + 在标题行留一句结论，否则「消解完了看不见」
    openFold('resDetails');
    const after = intOrNull(body, 'remaining_after');
    const before = intOrNull(body, 'remaining_before');
    setFoldState('resSum',
      '已消解 ' + (before === null ? '?' : String(before))
        + ' → ' + (after === null ? '?' : String(after)) + ' 处',
      after === 0 ? 'ok' : 'warn');
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
    openFold('resDetails');   // 失败信息也在折叠区内 → 同样要展开
    setFoldState('resSum', '消解失败，见详情', 'error');
  } finally {
    // 最坏路径（抛错 / 限频 / 断网）也必然走到这里，等待态不会残留
    showSolveWait(false);
    state.resolving = false;
    // 🔴 不能无条件 disabled=false：消解到 0 冲突后按钮又被点亮 = 可重复提交。
    //    统一交给 syncResolveDisabled() 按「当前状态条 + 冲突数」判定。
    setResolveBusy(false);
    btns.forEach(function (b) { if (b.isConnected) b.disabled = false; });
  }
}

/**
 * runQn7：给「问一下」加一层只做展示的包装 —— 展开折叠区 + 更新标题行状态字。
 * 🔴 askChat() / renderChat() 一行都不改：取数（POST /api/chat）与渲染仍是原实现，
 *    这里只负责「结果在折叠区内，必须先展开」和「收起时标题行也得说清有没有答过」。
 */
async function askChatWithStatus() {
  openFold('chatDetails');
  setFoldState('chatSum', '正在查手册…', '');
  await askChat();
  const answered = !!($('chatBox') && $('chatBox').querySelector('.chat-ans'));
  setFoldState('chatSum', answered ? '已回答，展开查看' : '提问失败，见上方提示', answered ? 'ok' : 'error');
}

/* =============================================================================
 * 9. 启动
 * ========================================================================== */
function bindEvents() {
  $('groupSel').addEventListener('change', function () {
    state.groupId = $('groupSel').value;
    // r4kJEe：记住用户切过的组（只更新 groupId，沿用原 loginAt/exp —— 切组不算活跃信号）
    saveAuth({ keepExpiry: true });
    state.objectId = '';       //换组 → 对象重选，避免看到上一个组的周视图
    clear($('resBox'));
    $('resBox').appendChild(el('div', { cls: 'empty', text: '尚未执行消解。' }));
    // F7 secSlots：换组后旧候选属于别的组 / 别的对象 → 必须清掉，
    // 否则「看着 A 班的候选，点按此提议却按 B 班提交」这种错位不报错。
    $('slotObjSel').value = '';
    showBanner($('slotErr'), null);
    clear($('slotBox'));
    $('slotBox').appendChild(el('div', { cls: 'empty', text: '尚未查询。选好对象与日期范围后点「查空位」。' }));
    setFoldState('slotSum', '尚未查询', '');   // 换组后旧结论作废，标题行同步复位
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
  // runQn7：首屏状态条上的「一键消解」与冲突列表那个是同一个动作、同一条埋点
  $('resolveBtnTop').addEventListener('click', function () { doResolve(null); });
  // runQn7：预设问题芯片 —— 只做「预填 + 展开 + 调现有 askChat()」。
  // 🔴 不在前端拼答案、不内置答案文本；请求仍是同一条 POST /api/chat。
  const chipBox = $('chatChips');
  if (chipBox) {
    chipBox.addEventListener('click', async function (ev) {
      const b = (ev.target && ev.target.closest) ? ev.target.closest('button[data-q]') : null;
      if (!b) return;
      $('chatQ').value = b.getAttribute('data-q') || '';
      await askChatWithStatus();       // 取数与渲染逻辑原样复用，一行未改
    });
  }
  // F7 secSlots：查空位（GET /api/slots，只读）
  $('slotBtn').addEventListener('click', function () { querySlots(); });
  [$('slotFrom'), $('slotTo')].forEach(function (inp) {
    inp.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') querySlots(); });
  });
  // rn6JxF：问一下（POST /api/chat，只读问答；回车与按钮等效）
  $('chatBtn').addEventListener('click', function () { askChatWithStatus(); });
  $('chatQ').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') askChatWithStatus();
  });
  // F3b：登录栏（mock 模式下 loginSec 整体隐藏，绑定无害）
  $('loginBtn').addEventListener('click', function () { doLogin(); });  [$('loginName'), $('loginPwd')].forEach(function (inp) {
    inp.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') doLogin(); });
  });
  // r4kJEe：退出登录（⛔ 不叫「安全退出」——服务端令牌无吊销接口，只是本机丢弃）
  // 🔴 null 守卫：旧 HTML + 新 JS 时 $('logoutBtn') 为 null，不守卫会让 bindEvents
  //    在中途抛错 → 后面所有绑定（含登录按钮）全部失效，整页变砖。
  const logoutBtn = $('logoutBtn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', function () {
      endSession({ clearStorage: true, msg: '已退出登录（仅本机丢弃凭证，服务端令牌未吊销）。' });
    });
  }
  // 「关于」：页脚文字链 → 弹层。同样做 null 守卫，理由同上（旧 HTML + 新 JS）。
  const aboutBtn = $('aboutBtn');
  if (aboutBtn) {
    aboutBtn.addEventListener('click', function () { openAbout(); });
  }
  // rTVvb8：公告的「编辑 / 发布」与错误态「重新加载」。
  // null 守卫与上面同理：旧 HTML + 新 JS 时节点不存在，不守卫会让 bindEvents 中途抛错、
  // 整个页面变砖（比少一个按钮严重得多）。
  const annEditBtn = $('annEditBtn');
  if (annEditBtn) {
    annEditBtn.addEventListener('click', function () { openAnnouncementEditor(); });
  }
  const annRetryBtn = $('annRetryBtn');
  if (annRetryBtn) {
    annRetryBtn.addEventListener('click', function () { refreshAnnouncement(); });
  }
  // r4kJEe：跨标签同步退出 —— 别的标签清了 sk_auth_v1 → 本标签同步回未登录。
  // 🔴 处理器内**只读不写**：写入会触发对方 storage 事件，形成 A→B→A 风暴。
  window.addEventListener('storage', function (e) {
    if (USE_MOCK) return;
    const ls = _ls();
    if (e.storageArea && ls && e.storageArea !== ls) return;
    if (e.key !== AUTH_KEY && e.key !== null) return;   // e.key===null 是 localStorage.clear()
    if (e.newValue !== null) return;                    // 续期 / 切组写入 → 不处理
    if (!state.token) return;
    // 只有「被删掉的那条正是本标签在用的」才算同步退出；
    // 否则「别的标签用另一个账号登录后又退出」会把本标签的无辜会话踢掉。
    let oldToken = '';
    try { oldToken = (JSON.parse(e.oldValue || '{}') || {}).token || ''; } catch (err) { oldToken = ''; }
    if (oldToken && oldToken !== state.token) return;
    // 静默退出：这是别人主动做的动作，不是错误 → ⛔ 不弹红字，只把身份卡与内容清干净
    endSession({});
  });
  // F5 埋点：关页时若任务仍在进行，补 task_abandon（docs/08 §4.3 beforeunload 兜底）
  window.addEventListener('pagehide', function () {
    if (state.taskActive) {
      state.taskActive = false;
      ping('task_abandon', { reason: 'page_hide' });
    }
  });
}

/* =============================================================================
 * 页头品牌标记：时之交响图标
 * -----------------------------------------------------------------------------
 * 图标由 brand-icon.js 在 canvas 上逐帧绘制（千级粒子 / 光弧 / 三指针 / 中心光点），
 * 尺寸、粒子密度、发光半径都按画布实际像素自适应，这里是唯一的挂载点。
 * 🔴 三条边界：
 *   - 图形一律画在 brand-icon.js 里，app.js 不画任何一笔 —— 换图只改那一个文件；
 *   - 脚本没加载（或 2d 上下文拿不到）时静默跳过，页头只剩深色托盘，绝不阻塞 boot；
 *   - 标签页不可见时浏览器本就不派发 requestAnimationFrame，不另写暂停逻辑。
 * ========================================================================== */

function mountBrandIcon() {
  const mark = $('brandMark');
  const canvas = $('brandIcon');
  if (!mark || !canvas || !window.TimeSymphonyIcon) return null;
  try {
    return window.TimeSymphonyIcon.create(canvas, { stage: mark, quality: 'smooth' });
  } catch (e) {
    return null;   // 画不出来就当没有 logo，页头仍是完整可用的
  }
}

/**
 * 启动。r4kJEe 起的顺序（每一句都有理由，别随手调）：
 *   埋点串 ✓ → 首屏状态条 ✓ → **bindEvents 先绑**（否则首次 loadAll 期间
 *   退出按钮与跨标签监听还没生效）→ mock 直接取数 →
 *   有持久化记录就恢复（中性态「正在恢复…」→ loadAll）→ 否则画未登录。
 */
function boot() {
  mountBrandIcon();
  state.sessionId = newSessionId();   // F5：每次打开页面生成一个会话串（埋点时长统计用）
  // runQn7：首屏状态条初始态（未登录＝黄 Badge，不显示任何数字）
  setStatus('unauth', '登录后显示本周冲突数');
  const tag = $('srcTag');
  if (USE_MOCK) {
    tag.hidden = false;
    tag.textContent = '演示数据（mock）';
  } else {
    // F3b：真实接口须先登录（未登录一律 401）；下方若恢复成功会立刻覆盖成中性态
    showLogin(null);
  }
  bindEvents();

  if (USE_MOCK) { loadAll(); return; }

  // r4kJEe：恢复登录态。不合法 / 过期 / 版本不对 → readAuth 静默返回 null，
  // 页面就是「未登录」，⛔ 不弹「登录已过期」红字（自动恢复失败不该像报错）。
  const rec = readAuth();
  if (!rec) return;

  state.token = rec.token;
  state.user = rec.user;
  state.groupId = rec.groupId || '';
  state.authDeadline = rec.loginAt + AUTH_ABS_MS;
  state.authTouchedAt = 0;
  const sec = $('loginSec');
  if (sec) sec.hidden = false;
  hideLogin();
  paintRestoring();      // 🔴 中性态，⛔ 不许先亮「已登录」绿标
  scheduleAuthDeadline();
  loadAll();             // 成功 → 转绿并续期；401 → handle401 清盘；断网 → 保留凭证只报错
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

/**
 * 「关于」弹层（2026-11-13）。
 *
 * 为什么要有：试用老师打开页面，不知道这是谁的、出了问题找谁；页面上原本
 * 一条联系方式都没有，全靠队长在微信里逐个说。
 *
 * 🔴 版本从 `/api/health` **动态取**，⛔ 前端不再写第二份 —— 四处同源已经
 *    够容易错了（2026-11-13 就因为版本号撞号丢过一次缓存）。
 *    ⚠️ 取的是 `version` + `release_date`（给人看的 v1.0.0 / 2026-11-13），
 *    **不是** `asset_version`（那是 `app.js?v=` 的缓存键，形如 20261202，
 *    连日期都不是 —— 2026-11-13 曾把它当版本显示，属于给评委错误信息）。
 * 🔴 反馈只写「微信」，⛔ 不写手机号与邮箱：页面在公网，手机号是可被直接
 *    骚扰/诈骗的强标识；微信号需要好友验证，暴露面小得多，且入口页早已
 *    公开同一个号，两处必须一致。
 * 🔴 内容一律 textContent，⛔ 不拼 HTML 字符串。
 */
function openAbout() {
  const m = modalOverlay();
  const box = m.box;
  box.appendChild(el('h3', { text: '关于「时之交响」' }));

  const list = el('div', { cls: 'about-list' });
  const addRow = function (k, v) {
    const r = el('div', { cls: 'about-row' });
    r.appendChild(el('span', { cls: 'k', text: k }));
    const val = el('span', { cls: 'v', text: v });
    r.appendChild(val);
    list.appendChild(r);
    return val;
  };

  addRow('项目', '时之交响 TimeSymphony');
  addRow('定位', '课表与活动统一冲突检测 · 一键消解');
  const verVal = addRow('版本', '读取中…');
  const dateVal = addRow('发布', '读取中…');
  addRow('团队', '王思勋（队长 · 后端）· 张晋嘉（后端）· 汤涵洋溢（前端）');
  addRow('反馈', '王思勋（队长）· 微信 wsx130307');
  addRow('数据', '页面为脱敏的试用数据，不含任何真实学生信息');
  box.appendChild(list);

  const btnRow = el('div', { cls: 'field' });
  const closeBtn = el('button', { cls: 'primary block', text: '关闭', attrs: { type: 'button' } });
  btnRow.appendChild(closeBtn);
  box.appendChild(btnRow);

  function shutdown() {
    document.removeEventListener('keydown', onKey);
    m.ov.removeEventListener('click', onOverlay);
    closeModal(m.ov);
  }
  function onKey(e) { if (e.key === 'Escape') shutdown(); }
  function onOverlay(e) { if (e.target === m.ov) shutdown(); }
  closeBtn.addEventListener('click', shutdown);
  document.addEventListener('keydown', onKey);
  m.ov.addEventListener('click', onOverlay);
  closeBtn.focus();

  // 版本与发布日期：拿不到就显示「—」，⛔ 绝不编一个数字
  fetch('/api/health', { headers: { Accept: 'application/json' } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      verVal.textContent = (d && d.version) ? ('v' + d.version) : '—';
      dateVal.textContent = (d && d.release_date) ? d.release_date : '—';
    })
    .catch(function () {
      verVal.textContent = '—';
      dateVal.textContent = '—';
    });
}

/* =============================================================================
 * 10.5 组织公告（卡 rTVvb8）
 * -----------------------------------------------------------------------------
 * 🔴 正文是用户输入：只允许 textContent / mdToDom()，绝不 innerHTML。
 *    mdToDom 全程 createElement + textContent，所以 `<img src=x onerror=…>`
 *    只会变成一段**字面文本**，不会产生 img 节点、不会执行脚本。
 * 🔴 「没有公告」与「公告没取到」必须区分：
 *    前者 announcement=null（200），后者走错误态 —— 页面绝不把「接口失败」
 *    显示成「暂无公告」（本项目最痛的静默失效就是这一类）。
 * 🔴 权限判据用**当前组的有效档位**（/api/groups 给的 level，含继承与天花板），
 *    与后端 authz.can(..., 'own') 同源，不会「看得到按钮但一点就 403」。
 * ========================================================================== */

/** 当前组是否达到「修改组」（own）档 —— 决定公告入口是否出现。 */
function canEditAnnouncement() {
  return state.groupLevels[state.groupId] === 'own';
}

/** 取一次当前组的公告。不抛：失败走错误态（不拖垮 loadAll 主流程）。 */
async function refreshAnnouncement() {
  if (USE_MOCK) {
    // mockRoute 不认识 /api/announcements → 直接隐藏，离线演示不报错
    state.announcement = null;
    renderAnnouncement();
    return;
  }
  if (!state.token) {
    state.announcement = null;
    renderAnnouncement();
    return;
  }

  const seq = ++state.annSeq;
  const groupAtRequest = state.groupId;
  try {
    const body = await apiGet('/api/announcements', withGroup({}));
    if (seq !== state.annSeq || state.groupId !== groupAtRequest) return;  // 已有更新的请求/切了组
    state.announcement = (body && body.announcement) ? body.announcement : null;
    renderAnnouncement();
  } catch (e) {
    if (seq !== state.annSeq || state.groupId !== groupAtRequest) return;
    if (!state.token) return;              // 401 已由 handle401 清场，不必再补红字
    if (e.status === 403) {
      // 组刚被收回：loadAll 的 403 回退会重跑并刷新，这里不额外弹错避免噪音
      state.announcement = null;
      renderAnnouncement();
      return;
    }
    renderAnnouncementError(e);
  }
}

/** 画横幅：有公告 → 标题 + Markdown 正文；无公告 → 有 own 档的人看到「发布」入口。 */
function renderAnnouncement() {
  const bar = $('annBar');
  if (!bar) return;                        // 旧 HTML + 新 JS：静默降级，不抛
  if (USE_MOCK) { bar.hidden = true; return; }

  const titleEl = $('annTitle');
  const bodyEl = $('annBody');
  const metaEl = $('annMeta');
  const editBtn = $('annEditBtn');
  const retryBtn = $('annRetryBtn');
  if (!titleEl || !bodyEl || !metaEl || !editBtn) { bar.hidden = true; return; }

  bar.classList.remove('is-error', 'is-empty');
  if (retryBtn) retryBtn.hidden = true;

  const a = state.announcement;

  if (!a) {
    // 没有公告：无权写的人整条隐藏；own 档的人需要入口，否则没人能创建第一条
    if (!state.token || !canEditAnnouncement()) {
      bar.hidden = true;
      titleEl.textContent = '';
      metaEl.textContent = '';
      clear(bodyEl);
      editBtn.hidden = true;
      return;
    }
    bar.classList.add('is-empty');
    titleEl.textContent = '暂无公告';
    metaEl.textContent = '发布后本组织所有成员都能看到。';
    clear(bodyEl);
    editBtn.textContent = '发布公告';
    editBtn.hidden = false;
    bar.hidden = false;
    return;
  }

  titleEl.textContent = str(a, 'title', '');
  clear(bodyEl);
  // 🔴 正文一律走 mdToDom（纯 DOM 构建）。⛔ 不许自己写 Markdown 解析器，更不许 innerHTML。
  bodyEl.appendChild(mdToDom(str(a, 'body_md', '')));

  // 后端时间列是 UTC（datetime('now')）；原样显示并标注，⛔ 不在前端猜时区
  const at = str(a, 'updated_at', '');
  const by = str(a, 'updated_by_name', '');
  const bits = [];
  if (at) bits.push('更新于 ' + at + ' UTC');
  if (by) bits.push('· ' + by);
  metaEl.textContent = bits.join(' ');

  editBtn.textContent = '编辑公告';
  editBtn.hidden = !canEditAnnouncement();
  bar.hidden = false;
}

/** 取公告失败：红边 + 明确写「这不是没有公告」，并给「重新加载」。 */
function renderAnnouncementError(e) {
  const bar = $('annBar');
  if (!bar) return;
  const titleEl = $('annTitle');
  const bodyEl = $('annBody');
  const metaEl = $('annMeta');
  if (!titleEl || !bodyEl || !metaEl) { bar.hidden = true; return; }

  bar.hidden = false;
  bar.classList.add('is-error');
  bar.classList.remove('is-empty');
  titleEl.textContent = '公告加载失败';
  clear(bodyEl);
  bodyEl.appendChild(el('div', { cls: 'md-p', text: httpErrText(e) }));
  metaEl.textContent = '这不是「没有公告」—— 接口没取到。';
  if ($('annEditBtn')) $('annEditBtn').hidden = true;
  if ($('annRetryBtn')) $('annRetryBtn').hidden = false;
}

/** 关掉编辑弹层（幂等：未打开时直接返回）。 */
function closeAnnouncementEditor() {
  const h = state.annEditor;
  if (!h) return;
  state.annEditor = null;
  state.annSaving = false;
  if (state.annPreviewTimer) {
    clearTimeout(state.annPreviewTimer);
    state.annPreviewTimer = 0;
  }
  document.removeEventListener('keydown', h.onKey);
  h.ov.removeEventListener('click', h.onOverlay);
  closeModal(h.ov);
}

/** 打开编辑弹层：标题 input + 正文 textarea + 实时预览 + 停用/取消/保存。 */
function openAnnouncementEditor() {
  if (USE_MOCK || !state.token || !canEditAnnouncement()) return;
  if (state.annEditor) closeAnnouncementEditor();

  const m = modalOverlay();
  const box = m.box;
  const editing = !!state.announcement;

  box.appendChild(el('h3', { text: editing ? '编辑公告' : '发布公告' }));
  box.appendChild(el('p', {
    cls: 'note',
    text: '正文支持 Markdown（# 标题 / - 列表 / 表格 / **加粗**）。HTML 不解析，会原样当文本显示。',
  }));

  const titleInput = el('input', {
    attrs: { type: 'text', maxlength: '200', placeholder: '标题（必填，200 字以内）' },
  });
  titleInput.value = str(state.announcement || {}, 'title', '');
  const titleField = el('div', { cls: 'field' });
  titleField.appendChild(el('label', { text: '标题' }));
  titleField.appendChild(titleInput);
  box.appendChild(titleField);

  const bodyInput = el('textarea', { attrs: { rows: '8', placeholder: '正文（Markdown 原文）' } });
  bodyInput.value = str(state.announcement || {}, 'body_md', '');
  const bodyField = el('div', { cls: 'field' });
  bodyField.appendChild(el('label', { text: '正文' }));
  bodyField.appendChild(bodyInput);
  box.appendChild(bodyField);

  box.appendChild(el('p', { cls: 'note', text: '预览（仅本人可见，点保存后才对全组生效）' }));
  const preview = el('div', { cls: 'md ann-preview' });   // md 类不能少
  box.appendChild(preview);

  const errBox = el('div', { cls: 'banner error' });
  errBox.hidden = true;
  box.appendChild(errBox);

  const actions = el('div', { cls: 'actions' });
  const delBtn = editing
    ? el('button', { cls: 'danger', text: '停用公告', attrs: { type: 'button' } })
    : null;
  const cancelBtn = el('button', { cls: 'secondary', text: '取消', attrs: { type: 'button' } });
  const saveBtn = el('button', { cls: 'primary', text: '保存', attrs: { type: 'button' } });
  if (delBtn) actions.appendChild(delBtn);
  actions.appendChild(cancelBtn);
  actions.appendChild(saveBtn);
  box.appendChild(actions);

  function paintPreview() {
    clear(preview);
    // 🔴 与横幅同一条渲染路径：mdToDom 纯 DOM，无 innerHTML
    preview.appendChild(mdToDom(bodyInput.value));
  }
  function schedulePreview() {
    // 180ms 防抖的取舍：不防抖时每键全量重解析（长表格会卡）；
    // 防抖太长（>300ms）中文输入法逐字上屏会「跟不上」。180ms 低于人的即时感知阈值。
    if (state.annPreviewTimer) clearTimeout(state.annPreviewTimer);
    state.annPreviewTimer = setTimeout(function () {
      state.annPreviewTimer = 0;
      paintPreview();
    }, 180);
  }
  function onKey(e) { if (e.key === 'Escape') closeAnnouncementEditor(); }
  function onOverlay(e) { if (e.target === m.ov) closeAnnouncementEditor(); }

  bodyInput.addEventListener('input', schedulePreview);
  cancelBtn.addEventListener('click', closeAnnouncementEditor);
  document.addEventListener('keydown', onKey);
  m.ov.addEventListener('click', onOverlay);

  state.annEditor = { ov: m.ov, onKey: onKey, onOverlay: onOverlay };

  saveBtn.addEventListener('click', function () {
    saveAnnouncement(titleInput, bodyInput, errBox, saveBtn);
  });
  if (delBtn) {
    delBtn.addEventListener('click', function () {
      deactivateAnnouncement(delBtn, errBox);
    });
  }

  paintPreview();
  titleInput.focus();
}

/** 保存（发布或覆盖）。 */
async function saveAnnouncement(titleInput, bodyInput, errBox, saveBtn) {
  if (state.annSaving) return;
  const title = String(titleInput.value || '').trim();
  if (!title) { showBanner(errBox, '标题不能为空。'); return; }
  const bodyMd = String(bodyInput.value || '');   // 正文不 trim：原文保存（后端也不转义）

  state.annSaving = true;
  saveBtn.disabled = true;
  showBanner(errBox, null);
  try {
    const r = await apiPost('/api/announcements', {}, {
      group: state.groupId,
      title: title,
      body_md: bodyMd,
    });
    if (!state.annEditor) return;    // 401 已把弹层拆掉，别再碰已移除的节点
    state.announcement = (r && r.announcement) ? r.announcement : null;
    renderAnnouncement();
    closeAnnouncementEditor();
  } catch (e) {
    if (!state.annEditor) return;
    showBanner(errBox, '保存失败：' + httpErrText(e));
  } finally {
    state.annSaving = false;
    if (state.annEditor) saveBtn.disabled = false;
  }
}

/** 停用（软删）。两步内联确认，不用 window.confirm（与本文件其它弹层风格一致）。 */
async function deactivateAnnouncement(btn, errBox) {
  if (state.annSaving || !state.announcement) return;

  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1';
    btn.textContent = '再点一次确认停用';
    return;
  }

  state.annSaving = true;
  btn.disabled = true;
  showBanner(errBox, null);
  try {
    await apiDelete('/api/announcements', withGroup({ id: state.announcement.id }));
    if (!state.annEditor) return;
    state.announcement = null;
    renderAnnouncement();
    closeAnnouncementEditor();
  } catch (e) {
    if (!state.annEditor) return;
    showBanner(errBox, '停用失败：' + httpErrText(e));
  } finally {
    state.annSaving = false;
    if (state.annEditor) btn.disabled = false;
  }
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

/** 弹层填「日程 + 新起始日 + 理由」，提交 POST /api/approvals。
 *
 * opts（可选，F7 secSlots 用；不传时行为与原来完全一致）：
 *   · note        弹层顶部的一句说明（如「候选空位：2026-11-17 第 3~4 节」）
 *   · prefillDate 预填 valid_from（YYYY-MM-DD）
 *   · emptyText   targets 为空时的占位选项文案（默认是冲突场景的措辞）
 * 🔴 这里**不放任何新的审批逻辑**：校验、POST、状态流转全都还是原来那套。
 */
function openProposeModal(targets, opts) {
  const o = opts || {};
  const md = modalOverlay();
  md.box.appendChild(el('h3', { text: '提议改期（需批准才生效）' }));
  if (o.note) md.box.appendChild(el('p', { cls: 'note', text: o.note }));

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
    selTarget.appendChild(el('option', {
    text: o.emptyText || '（这条冲突里没有可改期的日程）',
      attrs: { value: '' },
    }));
  }
  fTarget.appendChild(selTarget);
  md.box.appendChild(fTarget);

  const fDate = el('div', { cls: 'field' });
  fDate.appendChild(el('label', { text: '新的开始日期' }));
  const inpDate = el('input', { attrs: { type: 'date' } });
  if (o.prefillDate) inpDate.value = o.prefillDate;
  fDate.appendChild(inpDate);
  md.box.appendChild(fDate);

  const fReason = el('div', { cls: 'field' });
  fReason.appendChild(el('label', { text: '理由（必填）' }));
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
      showBanner($('reqMsg'), '提议已提交，等批准后才生效，课表暂时不变。');
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

/** @returns {Promise<boolean>} 是否成功取到（r4kJEe：续期判据之一，失败不许续期） */
async function loadRequests() {
  const errBox = $('reqErr');
  try {
    const body = await apiGet('/api/approvals', withGroup({}));
    renderRequests(body);
    // runQn7：收起时也要能看见「有没有请求、有几条待审批」
    const items = arr(body, 'requests');
    const pending = items.filter(function (r) { return str(r, 'status', '') === 'pending'; }).length;
    setFoldState('reqSum',
      items.length
        ? ('共 ' + items.length + ' 条' + (pending ? ' · 待审批 ' + pending : ''))
        : '暂无变更请求',
      pending ? 'warn' : '');
    return true;
  } catch (e) {
    // 401 已由 handle401 清场（屏已清空、登录栏已弹）→ 不再补一条「加载失败」红字
    if (!state.token) return false;
    // 403 = 该组没权限：交给 loadAll 的回退路径统一提示，这里不写红条（否则两条自相矛盾）
    if (e && e.status === 403) {
      setFoldState('reqSum', '当前组无权限查看变更请求', 'warn');
      return false;
    }
    showBanner(errBox, '变更请求加载失败：' + httpErrText(e));
    clear($('reqBox'));
    $('reqBox').appendChild(el('div', {
      cls: 'empty',
      text: '请求没加载出来，请点「刷新请求」。',
    }));
    setFoldState('reqSum', '加载失败，见上方提示', 'error');
    return false;
  }
}

async function doApprove(req, btn) {
  btn.disabled = true;
  try {
    const body = await apiPost('/api/approvals/approve', { id: req.id }, {});
    showBanner($('reqMsg'),
      '已批准，课表已更新（' + reqStatusLabel(str(body, 'status', 'applied')) + '）。');
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
    showBanner($('reqMsg'), '已撤回，课表不变。');
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
  fReason.appendChild(el('label', { text: '拒绝理由（必填，会被记录）' }));
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
    if (!reason) { showBanner(errBox, '拒绝必须填写理由。'); return; }
    btnOk.disabled = true;
    try {
      const body = await apiPost('/api/approvals/reject', { id: req.id }, { reason: reason });
      closeModal(md.ov);
      showBanner($('reqMsg'), '已拒绝，课表不变。');
      await loadRequests();
    } catch (e) {
      btnOk.disabled = false;
      showBanner(errBox, '拒绝失败：' + httpErrText(e));
    }
  });

  md.ov.addEventListener('click', function (ev) { if (ev.target === md.ov) closeModal(md.ov); });
}

/* =============================================================================
 * 11. 空位查询（F7 前端 / B 批 GET /api/slots）
 * -----------------------------------------------------------------------------
 * 端点（后端卡 r76Zu4 已定死；路由精确匹配、无 path 参数）：
 *   GET /api/slots?target=&from=YYYY-MM-DD&to=YYYY-MM-DD&duration=1|2|3&limit=
 *   200 {"ok":true,"target":..,"duration":n,"count":N,
 *        "candidates":[{date,day_of_week,periods,mask_hex,cost,reasons[,as_of_dow]}]}
 *   401 未登录 / 403 跨组织 / 400 参数非法或对象不存在 / 全满 = 200 + count=0
 *
 * 🔴 三条铁律（答辩会被追问）：
 *   1) **空结果必须说话**：count=0 时明确写「这段时间没有连排 N 节的空位」。
 *      留白会被当成「没问题」，这是本项目最痛的静默失效形态。
 *   2) **400 / 403 一律原文展示后端 message**（showBanner(e.message)），
 *      前端不自己编文案 —— 编出来的话和后端真实原因会分叉。
 *      401 走 apiGet → handle401（清 token + 弹登录栏），不在这里另写一套。
 *   3) **不做位运算、不自己算节次**：periods 是后端按当天生效作息（冬季作息 /
 *      调休 as_of）判定的 1-based 节次，原样展示；mask_hex 只透传不解析。
 *
 * 「按此提议」复用第 10 节的 openProposeModal()，不新写审批逻辑：
 *   候选空位只给「日期 + 节次」，不给 schedule_id；要改期的对象只能是该查询对象
 *   已有冲突里的日程（/api/conflicts 的 a/b 两侧，前端唯一拿得到 schedule_id 的地方）。
 *   因此：targets = 该对象在冲突列表里的日程（去重），候选日期预填 valid_from。
 *   提交后仍是 pending，课表零变化 —— 与冲突卡片那条路径完全一样。
 * ========================================================================== */

/** 连排节次 → 「第 3 ~ 4 节」。periods 由后端给（1-based），缺失如实说缺失。 */
function slotPeriodsText(periods) {
  const list = Array.isArray(periods)
    ? periods.filter(function (n) { return typeof n === 'number' && isFinite(n); })
    : [];
  if (!list.length) return '节次未给出';
  const a = list[0];
  const b = list[list.length - 1];
  return a === b ? ('第 ' + a + ' 节') : ('第 ' + a + ' ~ ' + b + ' 节');
}

/** 候选的「周几」说明；调休日（as_of_dow）额外标出按哪天的课表排。 */
function slotWhenText(c) {
  const date = str(c, 'date', '');
  const dow = intOrNull(c, 'day_of_week');
  const asOf = intOrNull(c, 'as_of_dow');
  let t = (date || '日期未给出') + '（' + dowLabel(dow) + '）';
  if (asOf !== null && dow !== null && asOf !== dow) {
    t += ' · 调休：按' + dowLabel(asOf) + '的课表排';
  }
  return t;
}

/**
 * 从当前冲突列表里收集「可改期日程」（targets 形如 [{schedule_id,title}]，与 conflictTargets 同形）。
 * 🔴 为什么必须从 /api/conflicts 拿：/api/slots 的候选只有 {date,periods,...}，
 *    **不带 schedule_id**；前端唯一能拿到 schedule_id 的接口就是冲突列表的 a / b 两侧。
 *    pred 给定时只取满足条件的冲突组。
 */
function conflictSchedulesFiltered(pred) {
  const items = arr(state.conflicts, 'items');
  const out = [];
  const seen = {};
  items.forEach(function (it) {
    if (pred && !pred(it)) return;
    conflictTargets(it).forEach(function (t) {
      if (!t.schedule_id || seen[t.schedule_id]) return;
      seen[t.schedule_id] = true;
      out.push(t);
    });
  });
  return out;
}

/** 与本次查询对象直接相关的冲突日程（object_id 命中）。可能为空 —— 如实返回 []。 */
function slotProposeTargets(target) {
  return conflictSchedulesFiltered(function (it) {
    return !target || str(it, 'object_id', '') === target;
  });
}

/** 候选条目 → 一个「按此提议」按钮（点击进入已有审批弹窗，不直接写库）。 */
function slotPropose(target, cand) {
  const date = str(cand, 'date', '');
  const periods = slotPeriodsText((cand && cand.periods) || []);

  // 优先用与该对象直接相关的冲突日程；拿不到就退回本组织全部冲突日程，
  // 并在弹层里**如实标注来源**——绝不假装那是「该对象的日程」。
  let targets = slotProposeTargets(target);
  let from = '该对象在冲突列表里的日程';
  if (!targets.length) {
    targets = conflictSchedulesFiltered(null);
    from = '当前组织的冲突日程（空位只给位置，不给具体日程）';
  }

  openProposeModal(targets, {
    prefillDate: date,
    note: '来自「查空位」：候选 ' + (date || '（日期未给出）') + ' ' + periods
      + '，对象 ' + target + '。可改期日程取自' + from
      + '；提交后仍为待审批，课表数据零变化。',
    emptyText: '（当前组织没有可改期的冲突日程 —— 空位只给位置，不给具体日程）',
  });
}

/** 渲染候选列表；count=0 必须有话说，绝不留白。 */
function renderSlots(body) {
  const box = $('slotBox');
  clear(box);
  const cands = arr(body, 'candidates');
  const count = intOrNull(body, 'count');
  const n = (count === null) ? cands.length : count;   // count 缺失 → 用实际条数
  const dur = intOrNull(body, 'duration');
  const durText = (dur === null) ? (str(body, 'duration', '') || 'N') : String(dur);

  if (!n || !cands.length) {
    box.appendChild(el('div', {
      cls: 'empty',
      text: '这段时间没有连排 ' + durText + ' 节的空位。'
        + '（该对象在这段时间里找不到连排 ' + durText + ' 节全空的位置。）',
    }));
    return;
  }

  box.appendChild(el('p', {
    cls: 'slot-sum',
    text: '共 ' + n + ' 个位置，越靠前越省事。',
  }));

  const ul = el('ul', { cls: 'items slots' });
  cands.forEach(function (c) {
    const li = el('li');

    const top = el('div', { cls: 'slot-top' });
    top.appendChild(el('span', {
      cls: 'slot-when',
      text: slotWhenText(c) + ' · ' + slotPeriodsText(c.periods),
    }));
    // 代价：没给出来就不显示这一段（不把「字段缺失」这类开发期信息写上屏幕）
    const cost = (c && typeof c.cost === 'number' && isFinite(c.cost)) ? String(c.cost) : '';
    if (cost) {
      const costWrap = el('span', { cls: 'slot-cost' });
      costWrap.appendChild(el('span', { text: '代价 ' }));
      costWrap.appendChild(el('b', { text: cost }));
      top.appendChild(costWrap);
    }
    li.appendChild(top);

    const reasons = Array.isArray(c && c.reasons) ? c.reasons : [];
    if (reasons.length) {
      const rl = el('div', { cls: 'slot-reasons' });
      reasons.forEach(function (r) { rl.appendChild(el('span', { cls: 'chip', text: String(r) })); });
      li.appendChild(rl);
    }

    const acts = el('div', { cls: 'acts' });
    const btn = el('button', { text: '按此提议', attrs: { type: 'button' } });
    btn.addEventListener('click', function () { slotPropose(str(state, 'slotTarget', ''), c); });
    acts.appendChild(btn);
    li.appendChild(acts);

    ul.appendChild(li);
  });
  box.appendChild(ul);
}

/**
 * 点「查空位」：GET /api/slots。
 * 🔴 失败时把 #slotBox 换成「未取到结果」，绝不保留上一次的候选 ——
 *    旧候选留在屏幕上会被当成这次查询的结果（错位且不报错）。
 */
async function querySlots() {
  const btn = $('slotBtn');
  const errBox = $('slotErr');
  const box = $('slotBox');

  const target = $('slotObjSel').value;
  const from = $('slotFrom').value.trim();
  const to = $('slotTo').value.trim();
  const duration = $('slotDur').value;

  showBanner(errBox, null);
  if (!target) {
    // 本地前置校验：没选对象连请求都发不出去，直接说清楚
    showBanner(errBox, '请先选择查询对象（下拉为空说明该组织当前没有可查对象）。');
    return;
  }

  btn.disabled = true;
  btn.textContent = '查询中…';
  try {
    const body = await apiGet('/api/slots', {
      target: target, from: from, to: to, duration: duration,
    });
    state.slotTarget = target;   // 记下这次查询的对象，供「按此提议」对应用
    renderSlots(body);
    // runQn7b：结果在折叠区内 → 必须展开；标题行常显结论（空结果也要说话）
    openFold('slotDetails');
    const n = intOrNull(body, 'count');
    const cnt = (n === null) ? arr(body, 'candidates').length : n;
    setFoldState('slotSum', cnt > 0 ? ('共 ' + cnt + ' 个候选') : '没有空位', cnt > 0 ? 'ok' : 'warn');
  } catch (e) {
    // 401 已在 handle401 里清 token 并弹出登录栏；403 / 400 原文展示后端 message
    showBanner(errBox, (e && e.message) ? e.message : String(e));
    clear(box);
    box.appendChild(el('div', { cls: 'empty', text: '未取到空位结果（原因见上方提示条）。' }));
    openFold('slotDetails');
    setFoldState('slotSum', '查询失败，见提示', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '查空位';
  }
}

/* =============================================================================
 * 10.9 Markdown 轻渲染（零依赖、纯 DOM 构建）
 * -----------------------------------------------------------------------------
 * 为什么要有这一层：后端的答案有两类来源 ——
 *   ① 手册原文摘录（`_retrieval_answer` 拼出的段落，**本身就是 Markdown**；
 *      实测会把「| 项 | 内容 |\n|---|---|」这种表格原样吐到屏幕上）；
 *   ② 模型润色后的回答（模型天生爱回 Markdown 的列表与加粗）。
 * 不渲染的话老师看到的就是一堆 `**` 和竖线。
 *
 * 🔴 三条硬约束：
 *   1. **绝不用 innerHTML**（项目铁律）。这里全程 createElement + textContent，
 *      Markdown 里的任何 HTML 都会被当纯文本，天然免疫注入。
 *   2. **不联网、不引三方库**（零外链 / 断网可跑）。只支持够用的一小撮语法，
 *      不做完整 CommonMark。
 *   3. **解析失败不许把内容吞掉**：认不出的行一律落到段落，宁可排版丑，
 *      不能出现「答案少了一段」。
 *
 * 支持：# 标题（1–4 级）· - / 1. 列表 · 表格 · > 引用 · ``` 代码块 · --- 分隔线
 *       行内 `代码` · **加粗** · *斜体*
 * ========================================================================== */

const MD_HEADING = /^(#{1,4})\s+(.*)$/;
const MD_UL = /^\s*[-*+]\s+(.*)$/;
const MD_OL = /^\s*\d+[.)]\s+(.*)$/;
const MD_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const MD_QUOTE = /^\s*>\s?(.*)$/;
const MD_FENCE = /^\s*```/;
const MD_TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/;

/** 行内标记 → 节点数组（`代码` / **加粗** / *斜体*）。 */
function mdInline(text) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    if (tok.slice(0, 2) === '**') out.push(el('strong', { text: tok.slice(2, -2) }));
    else if (tok.charAt(0) === '`') out.push(el('code', { text: tok.slice(1, -1) }));
    else out.push(el('em', { text: tok.slice(1, -1) }));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(document.createTextNode(text.slice(last)));
  return out;
}

/** 一行文本填进节点（行内标记照常解析）。 */
function mdFill(node, text) {
  mdInline(text).forEach(function (n) { node.appendChild(n); });
  return node;
}

/** 拆表格行：`| a | b |` → ['a', 'b']（忽略首尾空列，保留中间空串）。 */
function mdCells(line) {
  let s = line.trim();
  if (s.charAt(0) === '|') s = s.slice(1);
  if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
  return s.split('|').map(function (c) { return c.trim(); });
}

/**
 * Markdown 文本 → DocumentFragment。永不抛：任何意外都退化成段落。
 * 调用方拿到 fragment 直接 appendChild 即可。
 */
function mdToDom(src) {
  const frag = document.createDocumentFragment();
  const lines = String(src === undefined || src === null ? '' : src)
    .replace(/\r\n?/g, '\n')
    .split('\n');
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i += 1; continue; }

    // ---- 代码块 ----
    if (MD_FENCE.test(line)) {
      const buf = [];
      i += 1;
      while (i < lines.length && !MD_FENCE.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1; // 跳过收尾 ```
      const pre = el('pre', { cls: 'md-pre' });
      pre.appendChild(el('code', { text: buf.join('\n') }));
      frag.appendChild(pre);
      continue;
    }

    // ---- 分隔线 ----
    if (MD_HR.test(line)) {
      frag.appendChild(el('hr', { cls: 'md-hr' }));
      i += 1;
      continue;
    }

    // ---- 标题 ----
    const h = MD_HEADING.exec(line);
    if (h) {
      const level = Math.min(h[1].length + 2, 6); // # → h3（区块里 h1/h2 留给页面骨架）
      frag.appendChild(mdFill(el('h' + level, { cls: 'md-h' }), h[2]));
      i += 1;
      continue;
    }

    // ---- 表格：当前行以 | 开头且下一行是分隔行 ----
    if (line.trim().charAt(0) === '|' && i + 1 < lines.length && MD_TABLE_SEP.test(lines[i + 1])) {
      const head = mdCells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim().charAt(0) === '|') {
        rows.push(mdCells(lines[i]));
        i += 1;
      }
      const table = el('table', { cls: 'md-table' });
      const thead = el('thead');
      const htr = el('tr');
      head.forEach(function (c) { htr.appendChild(mdFill(el('th'), c)); });
      thead.appendChild(htr);
      table.appendChild(thead);
      const tbody = el('tbody');
      rows.forEach(function (r) {
        const tr = el('tr');
        head.forEach(function (_, ci) { tr.appendChild(mdFill(el('td'), r[ci] === undefined ? '' : r[ci])); });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      // 窄屏（768px 不横滚铁律）：表格自己横向滚，页面不滚
      const wrap = el('div', { cls: 'md-tablewrap' });
      wrap.appendChild(table);
      frag.appendChild(wrap);
      continue;
    }

    // ---- 引用 ----
    if (MD_QUOTE.test(line)) {
      const buf = [];
      while (i < lines.length && MD_QUOTE.test(lines[i])) {
        buf.push(MD_QUOTE.exec(lines[i])[1]);
        i += 1;
      }
      frag.appendChild(mdFill(el('blockquote', { cls: 'md-quote' }), buf.join(' ')));
      continue;
    }

    // ---- 无序 / 有序列表 ----
    const isUl = MD_UL.test(line);
    const isOl = !isUl && MD_OL.test(line);
    if (isUl || isOl) {
      const re = isUl ? MD_UL : MD_OL;
      const list = el(isUl ? 'ul' : 'ol', { cls: 'md-list' });
      while (i < lines.length) {
        const m = re.exec(lines[i]);
        if (!m) break;
        list.appendChild(mdFill(el('li'), m[1]));
        i += 1;
      }
      frag.appendChild(list);
      continue;
    }

    // ---- 段落：吃到空行或下一个块起始行为止 ----
    const para = [];
    while (i < lines.length) {
      const cur = lines[i];
      if (!cur.trim()) break;
      if (para.length && (
        MD_HEADING.test(cur) || MD_UL.test(cur) || MD_OL.test(cur) ||
        MD_QUOTE.test(cur) || MD_FENCE.test(cur) || MD_HR.test(cur) ||
        cur.trim().charAt(0) === '|'
      )) break;
      para.push(cur);
      i += 1;
    }
    const p = el('p', { cls: 'md-p' });
    para.forEach(function (text, idx) {
      if (idx) p.appendChild(el('br'));
      mdInline(text).forEach(function (n) { p.appendChild(n); });
    });
    frag.appendChild(p);
  }

  return frag;
}

/* =============================================================================
 * 11. 问一下（rn6JxF / B-6 POST /api/chat）
 * -----------------------------------------------------------------------------
 * 契约（src/server/api_chat.py 写死）：
 *   POST /api/chat  body {question, group?}
 *     200 → {answer, sources:[{doc,section}], mode, degraded}
 *     400 缺 question / 非字符串 / 超 500 字；401 未登录；403 group 越权
 *   🔴 「手册里没有这条」是 200 + mode="template"，不是 4xx —— 正常业务态，照常渲染。
 *
 * 🔴 本卡最关键的一条：**mode 必须显式显示，不许含糊**。
 *   llm                → 「AI 回答」        （这句是模型润色过的）
 *   retrieval/template → 「手册原文（未启用 AI）」（手册原文 / 固定文案，无模型参与）
 *   未知 mode          → 原样透出 mode 值并标「来源未知」，**绝不默认说成 AI**
 *   让老师以为「所有回答都是 AI 写的」属虚假陈述 —— 本项目 AI 只用于自然语言理解
 *   与结果解释，冲突检测与消解由自建求解器完成（AGENTS §9）。
 *
 * 🔴 前端不拼 prompt、不碰 key：问题原文直接 POST，是否调模型只由后端 env 决定。
 * 🔴 问答只读：不据此发起任何变更，改课走审批流（「变更请求」区块）。
 * ========================================================================== */

/**
 * mode → 界面标识。未知枚举原样显示（§12.2 第 2 条：不内置白名单、不拦截渲染）。
 * 返回 {text, cls}：cls 只决定颜色（mode-llm 蓝 / mode-ref 橙），文案才是判据。
 *
 * 🔴 2026-11-13 改：原来的「手册原文（未启用 AI）」把两件事混成一句 ——
 *    · 常见问题命中速查表（本来就不该调模型）
 *    · AI 配了但这回没调成
 *    老师看到前者会以为「AI 坏了」。现在按 degraded 分开说：
 *    · 没配 / 不需要模型 → 「手册速查」
 *    · 配了但没调成       → 「手册原文（AI 这次没响应）」
 */
function chatModeTag(mode, degraded) {
  if (mode === 'llm') return { text: 'AI 回答', cls: 'mode-llm' };
  if (mode === 'retrieval') {
    return degraded
      ? { text: '手册原文（AI 这次没响应）', cls: 'mode-ref' }
      : { text: '手册速查', cls: 'mode-ref' };
  }
  // 🔴 2026-11-13 第二次改：`template` 原标签是「手册里没有这条」——
  //    和正文一样像报错。改成中性说法，且不再出现「手册」二字。
  if (mode === 'template') return { text: '暂时答不上来', cls: 'mode-ref' };
  return { text: '来源未知（mode=' + (mode || '缺失') + '）', cls: 'mode-ref' };
}

/** 渲染一次问答结果：来源标识在最前，正文其次，出处最后（老师要能回查）。 */
function renderChat(body) {
  const box = $('chatBox');
  clear(box);

  const card = el('div', { cls: 'chat-ans' });

  // ---- ① 来源标识：必须与答案同一屏可见，不折叠不隐藏 ----
  const head = el('div', { cls: 'chat-head' });
  const tag = chatModeTag(str(body, 'mode', ''), body && body.degraded === true);
  head.appendChild(el('span', { cls: 'chat-mode ' + tag.cls, text: tag.text }));
  if (body && body.degraded === true) {
    // 后端「本可以调 AI 但没调成」→ 已降级为手册原文；如实说，别让人误以为是 AI 答的
    head.appendChild(el('span', { cls: 'chip', text: '本次 AI 调用未成功，已降级为手册原文' }));
  }
  card.appendChild(head);

  // ---- ② 正文：Markdown 轻渲染（表格/列表/加粗），全程 DOM 构建不走 innerHTML ----
  const answer = str(body, 'answer', '');
  const textBox = el('div', { cls: 'chat-text md' });
  textBox.appendChild(mdToDom(answer || '（没拿到答案，请重试）'));
  card.appendChild(textBox);

  // ---- ③ 出处：哪份文档哪一节，供回查；缺失就说缺失，不编文档号 ----
  const sources = arr(body, 'sources');
  // 🔴 2026-11-13 改：只显示给老师看的出处。`docs/07` 是接口契约（开发文档），
  //    在老师面前出现「docs/07 §11」只会让人困惑，改为不显示；
  //    `docs/30` 换成可读名「教师手册」。全部被过滤时整块不渲染，不留空壳。
  const DOC_NAME = { 'docs/30': '教师手册' };
  const shown = sources.filter(function (s) { return str(s, 'doc', '') !== 'docs/07'; });
  if (shown.length) {
    const src = el('div', { cls: 'chat-src' });
    src.appendChild(el('span', { text: '出处：' }));
    shown.forEach(function (s) {
      const doc = str(s, 'doc', '');
      const label = [DOC_NAME[doc] || doc, str(s, 'section', '')]
        .filter(function (x) { return !!x; }).join(' ');
      src.appendChild(el('span', { cls: 'chip', text: label }));
    });
    card.appendChild(src);
  }

  box.appendChild(card);
}

/**
 * 点「问一下」：POST /api/chat。
 * 🔴 发请求前先把上一次的答案清掉 —— 留在屏幕上会被当成这次问题的回答（错位且不报错）。
 * 🔴 失败文案一律用后端 message 原文（apiPost 已把它放进 e.message），不自己编。
 */
async function askChat() {
  const btn = $('chatBtn');
  const errBox = $('chatErr');
  const box = $('chatBox');
  const q = $('chatQ').value.trim();

  showBanner(errBox, null);
  if (!q) {
    // 本地前置校验：空问题连请求都不发（后端也会 400，但不必打扰网络）
    showBanner(errBox, '请先输入问题（例如：我想把周三下午的课调开）。');
    return;
  }

  btn.disabled = true;
  btn.textContent = '提问中…';
  clear(box);
  const wait = el('div', { cls: 'empty', text: '正在查手册…' });
  box.appendChild(wait);
  // 🔴 2026-11-13：模型侧最长可等 15 秒（线上实测 8 秒不够）。
  //    4 秒还没回就把话说清楚，别让老师以为界面卡死了。
  const slowTimer = setTimeout(function () {
    wait.textContent = '正在查手册，AI 组织语言中（最多十几秒）…';
  }, 4000);
  try {
    const payload = { question: q };
    // 与其它接口同口径：只传当前已授权的组（后端会校验越权 → 403）
    if (state.groupId) payload.group = state.groupId;
    const body = await apiPost('/api/chat', {}, payload);
    renderChat(body);
  } catch (e) {
    // 401 已在 handle401 里清 token 并弹出登录栏；400/403/断网等一律原文展示
    showBanner(errBox, (e && e.message) ? e.message : String(e));
    clear(box);
    box.appendChild(el('div', { cls: 'empty', text: '未取到回答（原因见上方提示条）。' }));
  } finally {
    clearTimeout(slowTimer);
    // 最坏路径（抛错 / 断网）也必然走到这里，按钮不会永久卡在「提问中…」
    btn.disabled = false;
    btn.textContent = '问一下';
  }
}

/* =============================================================================
 * 12. 临时微调（B9 · 卡 rrr4vM）
 * -----------------------------------------------------------------------------
 * 老师的一句话需求：「这节课今天不上了 / 挪到第 N 节」。
 * 后端 `POST/DELETE/GET /api/exceptions` 早已全实现（B9），本段只补**界面入口**。
 *
 * 数据从哪来（🔴 三条都不能猜）：
 *   · 这一格到底有哪几节日程 → `GET /api/schedules/detail`（docs/07 §4.1）
 *     ⛔ 不能用 /api/schedules 的 grid：它是**按位或合并**后的掩码，
 *        拿不到 schedule_id，也不知道一格里有几条日程。
 *   · 「改到第 N 节」有哪些节次可选 → 同一个接口的 `periods[]`（已过
 *     effective_slot_config，只含当天真正启用的节次）。
 *     ⛔ 不查 period_slots / set_slots，⛔ 不硬编码 1..10。
 *   · 当天有没有已经建好的例外 → `GET /api/exceptions?schedule_id=`（逐条查）。
 *
 * 掩码口径（错一位就整体错位且不报错）：
 *   第 N 节 → 位 N，`0x000002` = 第 1 节。目标掩码**直接取** `periods[].new_mask`
 *   （服务端算好的单日掩码，与 cells[].mask 同格式），前端一个字都不自己算。
 *
 * 错误文案一律用后端 message 原文（apiGet/apiPost/apiDelete 已把它塞进 e.message），
 * 前端不改写、不兜底成别的操作。唯一的例外是 `effective=false`：后端不返回 message，
 * 那条必须由前端说清楚「写了但没生效」，见 _resultToast()。
 * 409 的结构化 `exception_id` 从 `e.body` 读（正则抠字符串只作兜底，见 _excIdFromError）。
 * ========================================================================== */

/** 冲突条目 → 面板定位参数 {objectId, date, idx}。定位不到返回 null（不猜一个假 id）。 */
function conflictAnchor(item) {
  if (!item || typeof item !== 'object') return null;
  const date = str(item, 'date', '');
  if (!date) return null;
  // 顶层 object_id 恒有（b3_kernels 的 rep.object_id）；对象行被删过时才可能为空，
  // 那时退回 a/b 两侧。两侧在 `_enrich` 里用的是同一个 obj，所以正常情况下相等。
  let oid = str(item, 'object_id', '');
  if (!oid && item.a) oid = str(item.a, 'object_id', '');
  if (!oid && item.b) oid = str(item.b, 'object_id', '');
  if (!oid) return null;
  return { objectId: oid, date: date, idx: _firstReadablePeriod(item) };
}

/**
 * 冲突点落在第几节。取 `readable` 里第一个合法节次；取不到返回 null。
 * ⚠️ 一条冲突可能横跨两节（连堂），这里只取第一节用于**定位面板**，
 *    面板内部一律以 detail 的 cells 为准，不依赖这个值。
 */
function _firstReadablePeriod(item) {
  let rs = arr(item, 'readable');
  if (!rs.length && item.a) rs = arr(item.a, 'readable');
  for (let i = 0; i < rs.length; i++) {
    const p = readablePeriod(rs[i]);
    if (p !== null && p >= 1) return p;
  }
  return null;
}

/** ISO 日期 → 周几(1-7)。用本地日历算，不查后端（后端给的是「生效周几」，调休日会不同）。 */
function _dowOfDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  if (!m) return null;
  const dt = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const w = dt.getDay();                 // 0 = 周日
  return w === 0 ? 7 : w;
}

function _shortDate(iso) { return String(iso || '').slice(5); }

function _dowName(iso) {
  const d = _dowOfDate(iso);
  return d ? dowLabel(d) : '';
}

/** 对象 id → 显示名（来自 /api/schedules 的 objects，不另打接口）。 */
function _objectName(id) {
  const list = Array.isArray(state.objects) ? state.objects : [];
  for (let i = 0; i < list.length; i++) {
    if (str(list[i], 'id', '') === id) return str(list[i], 'name', '');
  }
  return '';
}

/** 节次下拉里的一行文案。start/end 可能缺失 → 只拼存在的部分，绝不出现 null-null。 */
function _periodLabel(p) {
  const name = str(p, 'name', '') || ('第' + str(p, 'idx', '') + '节');
  const st = str(p, 'start', '');
  const en = str(p, 'end', '');
  return (st && en) ? (name + '（' + st + '-' + en + '）') : name;
}

/** 冲突数 → 「23 处」/「3 组」。取不到返回 null（⛔ 不返回 0，0 会被读成「没有冲突」）。 */
function _countText(body) {
  const c = conflictCounts(body);
  if (!c) return null;
  return c.n + (c.unit.indexOf('处') >= 0 ? ' 处' : ' 组');
}

/**
 * 取 409 的 exception_id。
 *
 * 🔴 优先读响应体的**结构化字段** `exception_id`（后端 `ApiError(..., extra=...)`
 *    平铺进 JSON，docs/07 §11）。正则从 message 里抠只是兜底——留给「后端还没升级」
 *    或将来别的接口仍在文案里带 id 的情形；字符串一改正则就静默失配，不能当主路径。
 */
function _excIdFromError(err) {
  const body = err && err.body;
  if (body && typeof body.exception_id === 'string' && body.exception_id) {
    return body.exception_id;
  }
  const m = /exception_id=([A-Za-z0-9_\-]+)/.exec(String((err && err.message) || ''));
  return m ? m[1] : null;
}

/* ---------------------------------------------------------------------------
 * 面板生命周期
 * ------------------------------------------------------------------------- */

function closeExceptionPanel() {
  const p = state.excPanel;
  if (!p) return;
  p.closed = true;
  state.excPanel = null;
  state.excBusy = false;
  if (p.keyHandler) {
    document.removeEventListener('keydown', p.keyHandler);
    p.keyHandler = null;
  }
  closeModal(p.ov);
}

/**
 * 打开临时微调面板。
 * @param ctx {objectId, date, idx}  idx 可为 null（冲突条目定位不到节次时按「日期级」打开）
 */
async function openExceptionPanel(ctx) {
  if (USE_MOCK) return;                       // mockRoute 不支持 detail / DELETE
  if (!ctx || !ctx.objectId || !ctx.date) return;
  closeExceptionPanel();

  const md = modalOverlay();
  md.box.className = 'modal exc';
  const panel = {
    ov: md.ov,
    box: md.box,
    objectId: String(ctx.objectId),
    date: String(ctx.date),
    idx: (ctx.idx === null || ctx.idx === undefined) ? null : Number(ctx.idx),
    gid: state.groupId,          // 打开时的组：提交前比对，防止「开着面板切了组」
    closed: false,
    detail: null,
    pairs: [],                   // [{schedule, periods[], exceptions[], excErr}]
    err: null,                   // 加载/操作失败的原文
    conflictExcId: null,         // 409 里抠出来的 exception_id（给「一键撤销」）
    toast: null,                 // {kind:'ok'|'warn', text}
    errBox: null,
    keyHandler: null,
  };
  state.excPanel = panel;

  // 点遮罩关闭（与 openProposeModal 同款：只在点到遮罩本身时关，点内容不关）
  panel.ov.addEventListener('click', function (ev) {
    if (ev.target === panel.ov) closeExceptionPanel();
  });
  // Esc 关闭。监听挂 document，所以关闭时必须显式移除，否则切组后仍会触发旧闭包。
  panel.keyHandler = function (ev) {
    if (ev.key === 'Escape') closeExceptionPanel();
  };
  document.addEventListener('keydown', panel.keyHandler);

  _paintPanel(panel);          // 先出骨架（「正在读取…」）
  await _loadPanelData(panel);
  if (panel.closed) return;    // 加载途中被关掉：不要再往已 detach 的节点里画
  _paintPanel(panel);
}

/** 面板骨架：标题 / 提示条 / 日程行 / 底部按钮。可反复重画。 */
function _paintPanel(panel) {
  if (panel.closed) return;
  const box = panel.box;
  clear(box);

  box.appendChild(el('h3', { text: _panelTitle(panel) }));

  // 结果提示（成功 / 写入未生效）
  const toast = el('div', {
    cls: 'banner ' + ((panel.toast && panel.toast.kind === 'warn') ? 'warn' : 'ok'),
    attrs: { role: 'status', 'aria-live': 'polite' },
  });
  if (panel.toast && panel.toast.text) {
    toast.appendChild(el('span', { text: panel.toast.text }));
    toast.hidden = false;
  } else {
    toast.hidden = true;
  }
  box.appendChild(toast);

  // 错误提示：后端 message 原文，一个字都不改
  const errBox = el('div', { cls: 'banner error', attrs: { role: 'alert' } });
  errBox.hidden = true;
  if (panel.err) {
    showBanner(errBox, panel.err);
    if (panel.conflictExcId) {
      const undo = el('button', { text: '撤销这条例外', attrs: { type: 'button' } });
      undo.addEventListener('click', function () {
        _doRevokeException(panel, null, panel.conflictExcId, undo);
      });
      errBox.appendChild(undo);
    }
  }
  box.appendChild(errBox);
  panel.errBox = errBox;

  // 正文
  if (!panel.detail) {
    if (!panel.err) box.appendChild(el('p', { cls: 'note', text: '正在读取这一格的日程…' }));
  } else {
    _paintPanelBody(panel, box);
  }

  // 底部
  const actions = el('div', { cls: 'actions' });
  const refresh = el('button', { text: '刷新', attrs: { type: 'button' } });
  refresh.addEventListener('click', function () { _reloadPanel(panel); });
  actions.appendChild(refresh);
  const closeBtn = el('button', { cls: 'primary', text: '关闭', attrs: { type: 'button' } });
  closeBtn.addEventListener('click', closeExceptionPanel);
  actions.appendChild(closeBtn);
  box.appendChild(actions);
}

function _panelTitle(panel) {
  const d = panel.detail;
  const iso = str(d, 'date', '') || panel.date;
  const realDow = _dowOfDate(iso);
  const parts = [];
  parts.push((_dowName(iso) ? _dowName(iso) + ' ' : '') + iso);
  if (panel.idx !== null && isFinite(panel.idx)) parts.push('第' + panel.idx + '节');
  const name = _objectName(panel.objectId);
  if (name) parts.push(name);
  let title = parts.join(' · ');
  // 调休日：detail.day_of_week 是「生效周几」（as_of_dow），与日期本身的周几不同。
  // 不标出来，老师会以为面板搞错了日子。
  const effDow = d ? Number(d.day_of_week) : 0;
  if (d && effDow && realDow && effDow !== realDow) {
    title += '（按' + dowLabel(effDow) + '课表·调休）';
  }
  return title;
}

function _paintPanelBody(panel, box) {
  // 显示规则：本次格子里有这个节次的日程 + 打开时就在这一格、现在却消失了的日程
  // （后者正是「刚被取消 / 刚被改走」的那条，必须留着才撤得回去）。
  const pairs = (panel.pairs || []).filter(function (p) {
    if (p.wasShown) return true;
    if (panel.idx === null) return (p.periods || []).length > 0;
    return (p.periods || []).indexOf(panel.idx) >= 0;
  });
  if (!pairs.length) {
    box.appendChild(el('div', {
      cls: 'empty',
      text: panel.idx === null
        ? '这一天没有任何日程占用（没有可调整的课）。'
        : '这一节现在没有日程（可能已被取消或撤销）。点「刷新」重新读取。',
    }));
    return;
  }
  box.appendChild(el('p', {
    cls: 'note',
    text: pairs.length > 1
      ? '这一格有 ' + pairs.length + ' 条日程同时占用 —— 这就是冲突本身。请在下面选定要动的那一条。'
      : '这一格有 1 条日程。',
  }));

  const ul = el('ul', { cls: 'exc-list' });
  pairs.forEach(function (pair) { ul.appendChild(_paintExcRow(panel, pair)); });
  box.appendChild(ul);
}

function _paintExcRow(panel, pair) {
  const s = pair.schedule || {};
  const sid = str(s, 'id', '');
  const kind = str(s, 'kind', '');
  const li = el('li', { cls: 'exc-row' });

  const head = el('div', { cls: 'exc-title' });
  head.appendChild(el('span', { text: str(s, 'title', '') || '（无标题）' }));
  head.appendChild(el('span', {
    cls: 'exc-kind' + (kind === 'periodic' ? '' : ' off'),
    text: kind === 'periodic' ? '周期课' : (kind === 'once' ? '一次性活动' : '类型未知'),
  }));
  li.appendChild(head);

  // ---- 这一天的占用状态 ----
  if (pair.missing) {
    // 取消之后这条日程当天不再产生占用，cells 里就没有它了。
    // 但用户此刻最需要「撤销」，所以行要留着并说明原因。
    li.appendChild(el('p', {
      cls: 'note',
      text: '这一天已无占用（整条已取消，或已改到别的节次）。下面是它当前的临时例外。',
    }));
  } else if (pair.periods && pair.periods.length) {
    let line = '这一天占 ' + pair.periods.map(function (p) { return '第' + p + '节'; }).join('、');
    // 连堂提示只对周期课有意义：一次性活动本来就不能取消/改点。
    if (pair.periods.length > 1 && kind === 'periodic') {
      line += '（连堂：取消会把这几节一起取消）';
    }
    if (panel.idx !== null && pair.periods.indexOf(panel.idx) < 0) {
      line += '——已不在第' + panel.idx + '节（改点已生效）';
    }
    li.appendChild(el('p', { cls: 'note', text: line }));
  }

  // ---- 当天已有例外 ----
  if (pair.excErr) {
    // 🔴 查询失败绝不能当成「没有例外」：那样撤销按钮会静默消失，
    //    用户只能靠再点一次取消拿到 409，而 409 里的 id 只在一句话里。
    li.appendChild(el('p', { cls: 'exc-state bad', text: '当天例外没查到：' + pair.excErr }));
  } else if (pair.exceptions && pair.exceptions.length) {
    const ex = pair.exceptions[0];
    let txt = '当天已有例外：';
    if (str(ex, 'mode', '') === 'cancel') {
      txt += '已取消';
    } else {
      const rs = arr(ex, 'readable');
      txt += '已改到' + (rs.length
        ? rs.map(function (r) {
          const p = readablePeriod(r);
          return p === null ? '（节次未知）' : ('第' + p + '节');
        }).join('、')
        : '其他节次');
    }
    li.appendChild(el('p', { cls: 'exc-state', text: txt }));
  }

  // ---- 操作按钮 ----
  const acts = el('div', { cls: 'exc-acts' });
  if (!sid) {
    acts.appendChild(el('span', { cls: 'exc-hint', text: '这条日程没有 id，无法操作。' }));
  } else if (kind !== 'periodic') {
    // 后端对 kind != 'periodic' 直接 400（b9_kernels.py:234-239）：
    // 前端不把「点了必炸」的按钮摆出来。
    acts.appendChild(el('span', {
      cls: 'exc-hint',
      text: kind === 'once'
        ? '一次性活动不支持临时取消/改点，请直接修改活动本身。'
        : '日程类型未知（kind 为空），未提供临时微调。',
    }));
  } else if (pair.excErr) {
    acts.appendChild(el('span', {
      cls: 'exc-hint',
      text: '当天例外没查到，先不提供操作（否则可能撤不回去）。',
    }));
  } else {
    const cancelBtn = el('button', { text: '取消今天这节课', attrs: { type: 'button' } });
    cancelBtn.addEventListener('click', function () {
      _doCancelException(panel, pair, cancelBtn);
    });
    acts.appendChild(cancelBtn);

    // 改到第 N 节：只列 detail.periods 里当天真正启用的节次，
    // 并排除这条日程当天已经占着的节次（选它等于「原地不动」，后端会返回 effective=false）。
    // 🔴 option 的 value 直接用服务端给的 new_mask（不是 idx）——
    //    前端不再自己算掩码；服务端没给就不列这一项，绝无「前端兜底造 mask」的后路。
    const sel = el('select', { attrs: { 'aria-label': '改到第几节' } });
    sel.appendChild(el('option', { text: '改到哪一节…', attrs: { value: '' } }));
    const per = arr(panel.detail, 'periods');
    let usable = 0;
    per.forEach(function (p) {
      const idx = Number(p.idx);
      const mask = str(p, 'new_mask', '');
      if (!isFinite(idx) || idx < 1 || !mask) return;   // 没有 new_mask = 服务端不支持改点
      if (pair.periods && pair.periods.indexOf(idx) >= 0) return;
      usable += 1;
      sel.appendChild(el('option', {
        text: _periodLabel(p),
        attrs: { value: mask, 'data-idx': String(idx) },
      }));
    });
    const moveBtn = el('button', { text: '改到', attrs: { type: 'button' } });
    // 🔴 2026-11-13：原来只在未选节次时 disabled，视觉上是个灰白按钮，和「取消」
    //    并排看不出它能用 —— 老师因此以为「只能取消、不能改点」。现在选中节次后
    //    按钮变主色（primary），形成「先选节次 → 按钮亮起 → 点它」的明确路径。
    const syncMove = function () {
      const on = sel.value !== '';
      moveBtn.disabled = !on;
      moveBtn.className = on ? 'primary' : '';
      moveBtn.setAttribute('title', on ? '把这节课改到选中的节次' : '先在上面的下拉里选一节');
    };
    sel.addEventListener('change', syncMove);
    moveBtn.addEventListener('click', function () {
      const opt = sel.options[sel.selectedIndex];
      const mask = sel.value;
      const idx = opt ? Number(opt.getAttribute('data-idx')) : NaN;
      if (!mask || !isFinite(idx)) return;
      _doMoveException(panel, pair, idx, mask, moveBtn);
    });
    syncMove();
    if (!usable) {
      moveBtn.disabled = true;
      sel.disabled = true;
      acts.appendChild(el('span', {
        cls: 'exc-hint',
        text: per.length ? '这一天的其它启用节次都已被这条日程占着，无法改点。' : '当天没有启用节次，无法改点。',
      }));
    }
    acts.appendChild(sel);
    acts.appendChild(moveBtn);

    if (pair.exceptions && pair.exceptions.length) {
      const exId = str(pair.exceptions[0], 'id', '');
      const undoBtn = el('button', { cls: 'primary', text: '撤销', attrs: { type: 'button' } });
      undoBtn.addEventListener('click', function () {
        _doRevokeException(panel, pair, exId, undoBtn);
      });
      acts.appendChild(undoBtn);
    }
  }
  li.appendChild(acts);
  return li;
}

/** 面板数据：detail + 每条日程当天的例外。 */
async function _loadPanelData(panel) {
  panel.err = null;
  panel.detail = null;
  try {
    panel.detail = await apiGet('/api/schedules/detail',
      withGroup({ object_id: panel.objectId, date: panel.date }));
  } catch (e) {
    if (e && e.status === 401) return;      // handle401 已拆掉面板
    panel.err = httpErrText(e);
    return;
  }
  if (panel.closed) return;

  const pairs = _mergePairs(panel, _panelPairs(panel));

  await Promise.all(pairs.map(function (p) {
    p.exceptions = p.exceptions || [];
    const sid = str(p.schedule, 'id', '');
    const kind = str(p.schedule, 'kind', '');
    // 🔴 非周期日程**不查**例外：GET /api/exceptions 的 load_target 对
    //    kind!='periodic' 直接 400（b9_kernels.load_target），那不是「查询失败」，
    //    是这类日程本来就不能挂例外。照查会把一条必然的 400 显示成红字故障。
    if (kind !== 'periodic') { p.excErr = null; p.exceptions = []; return null; }
    if (!sid) { p.excErr = '日程 id 缺失'; return null; }
    return apiGet('/api/exceptions', { schedule_id: sid }).then(function (body) {
      p.exceptions = arr(body, 'items').filter(function (it) {
        return str(it, 'date', '') === panel.date;
      });
      p.excErr = null;
    }).catch(function (e) {
      if (e && e.status === 401) return;    // 面板已被 401 拆掉
      p.excErr = httpErrText(e);
    });
  }));
  if (panel.closed) return;
  panel.pairs = pairs;
}

/**
 * detail.cells → 按日程去重的列表（连堂会出现在多格里，合并成一条并记录全部节次）。
 * 这里**不按 panel.idx 过滤**：取消/改点之后，那条日程会从原格消失或挪到别的格，
 * 若按格过滤，它连同「撤销」按钮一起从面板上消失 —— 用户再也撤不回去。
 */
function _panelPairs(panel) {
  const cells = arr(panel.detail, 'cells');
  const map = {};
  const order = [];
  cells.forEach(function (c) {
    const idx = Number(c.idx);
    if (!isFinite(idx)) return;
    arr(c, 'schedules').forEach(function (s) {
      const sid = str(s, 'id', '');
      const key = sid || ('__noid_' + order.length);
      if (!map[key]) {
        map[key] = { schedule: s, periods: [] };
        order.push(key);
      }
      if (map[key].periods.indexOf(idx) < 0) map[key].periods.push(idx);
    });
  });
  return order.map(function (k) {
    const p = map[k];
    p.periods.sort(function (a, b) { return a - b; });
    return p;
  });
}

/**
 * 把「本次读到的占用」并进面板的**稳定快照** `panel.seen`。
 *
 * 🔴 为什么不能直接用这一次的 cells：cells 只反映**当前占用**。
 *    取消一节课之后，那条日程当天不再产生占用 → 从 cells 消失 →
 *    面板上它的行（以及撤销按钮）一起没了。而那一刻恰恰是用户最需要「撤销」的时候。
 *    所以：第一次见到的行一直留在面板上，只更新它的状态（missing / 已改到第N节）。
 */
function _mergePairs(panel, present) {
  if (!panel.seen) panel.seen = {};
  if (!panel.seenOrder) panel.seenOrder = [];
  const presentKeys = {};
  present.forEach(function (p) {
    const sid = str(p.schedule, 'id', '');
    const key = sid || ('__noid_' + panel.seenOrder.length);
    presentKeys[key] = true;
    let rec = panel.seen[key];
    if (!rec) {
      rec = { schedule: p.schedule, periods: [], wasShown: false };
      panel.seen[key] = rec;
      panel.seenOrder.push(key);
    }
    rec.schedule = p.schedule;               // 标题/kind 以最新为准
    rec.periods = p.periods.slice();
    rec.missing = false;
    if (!rec.wasShown) {
      rec.wasShown = (panel.idx === null) || (p.periods.indexOf(panel.idx) >= 0);
    }
  });
  panel.seenOrder.forEach(function (key) {
    if (presentKeys[key]) return;
    panel.seen[key].missing = true;
    panel.seen[key].periods = [];
  });
  return panel.seenOrder.map(function (k) {
    const rec = panel.seen[k];
    rec.exceptions = rec.exceptions || [];
    rec.excErr = rec.excErr || null;
    return rec;
  });
}

/** 用户点「刷新」：先重画（清掉旧错误），再重读。 */
async function _reloadPanel(panel) {
  if (panel.closed || state.excBusy) return;
  panel.toast = null;
  panel.err = null;
  panel.conflictExcId = null;
  _paintPanel(panel);
  await _loadPanelData(panel);
  if (panel.closed) return;
  _paintPanel(panel);
}

/* ---------------------------------------------------------------------------
 * 三个动作
 * ------------------------------------------------------------------------- */

async function _doCancelException(panel, pair, btn) {
  if (state.excBusy) return;
  const payload = {
    schedule_id: str(pair.schedule, 'id', ''),
    date: panel.date,
    mode: 'cancel',
  };
  // 🔴 group 放 **body**：api_exceptions 从 data.get("group") 取（api_exceptions.py:106）。
  //    放 query 会被静默忽略 → 多组织账号落到默认组，写进别的组织。
  if (state.groupId) payload.group = state.groupId;

  const title = str(pair.schedule, 'title', '');
  await _runPanelAction(panel, btn, {
    headline: '已取消 ' + _shortDate(panel.date) + ' ' + _dowName(panel.date) + ' 第'
      + (pair.periods && pair.periods.length ? pair.periods.join('、') + ' 节' : '') +
      (title ? '：' + title : ''),
    request: function () { return apiPost('/api/exceptions', {}, payload); },
  });
}

async function _doMoveException(panel, pair, idx, mask, btn) {
  if (state.excBusy) return;
  if (!mask) {
    // 服务端没给目标掩码就不发请求：绝不在这里自己造一个（那正是 2026-11-13 删掉的做法）。
    panel.err = '服务端没有给出第 ' + idx + ' 节的目标掩码（periods[].new_mask 缺失），未发送请求。';
    _paintPanel(panel);
    return;
  }
  const payload = {
    schedule_id: str(pair.schedule, 'id', ''),
    date: panel.date,
    mode: 'move',
    new_mask: mask,          // 🔴 绝不能漏：漏了后端 400，前端也不许兜底成 cancel
  };
  if (state.groupId) payload.group = state.groupId;

  const title = str(pair.schedule, 'title', '');
  await _runPanelAction(panel, btn, {
    headline: '已把 ' + _shortDate(panel.date) + ' ' + _dowName(panel.date) + ' 的'
      + (title ? '「' + title + '」' : '这节课') + '改到第' + idx + '节',
    request: function () { return apiPost('/api/exceptions', {}, payload); },
  });
}

async function _doRevokeException(panel, pair, excId, btn) {
  if (state.excBusy) return;
  if (!excId) {
    panel.err = '这条例外没有 id，无法撤销（请点「刷新」重新读取）。';
    _paintPanel(panel);
    return;
  }
  const title = pair ? str(pair.schedule, 'title', '') : '';
  await _runPanelAction(panel, btn, {
    headline: '已撤销 ' + _shortDate(panel.date) + ' ' + _dowName(panel.date) + ' 的临时例外'
      + (title ? '（' + title + '）' : '') + '，课表已恢复',
    exemptEffective: true,      // DELETE 不返回 effective，不能因此说「没生效」
    request: function () { return apiDelete('/api/exceptions', { id: excId }); },
  });
}

/**
 * 三个动作共用的执行壳：加锁 → 发请求 → **重拉接口** → 重读面板 → 写结果。
 *
 * 🔴 顺序不能省：先 `refreshWeekAndConflicts()` 再写数字。
 *    只改前端状态、不重拉接口，是最坏的一种假成功 —— 页面说「已取消」，
 *    而冲突列表还是旧的，老师会以为系统没反应。
 */
async function _runPanelAction(panel, btn, opts) {
  if (panel.closed) return;
  if (panel.gid !== state.groupId) {
    panel.err = '打开面板后切换了组，请关闭后重新打开（避免在 A 组发的请求落到 B 组）。';
    _paintPanel(panel);
    return;
  }

  // 动作前先记数（只在确实取到数时才算）
  const before = _countText(state.conflicts);

  state.excBusy = true;
  _setPanelBusy(panel, true);
  panel.err = null;
  panel.toast = null;
  panel.conflictExcId = null;

  let body = null;
  try {
    body = await opts.request();
  } catch (e) {
    state.excBusy = false;
    if (e && e.status === 401) return;        // handle401 已清场并拆面板
    if (panel.closed) return;
    _setPanelBusy(panel, false);
    panel.err = httpErrText(e);               // 后端 message 原文
    panel.conflictExcId = _excIdFromError(e);
    _paintPanel(panel);
    return;
  }
  state.excBusy = false;
  if (panel.closed) return;
  _setPanelBusy(panel, false);

  // 1) 重拉接口（数字与周视图都必须来自接口）
  const r = await refreshWeekAndConflicts();
  if (panel.closed) return;

  // 2) 重读面板（撤销按钮立即可用 —— 取消后该日程当天不再出现在 cells 里，
  //    只有重读例外列表才拿得到撤销入口）
  await _loadPanelData(panel);
  if (panel.closed) return;

  // 3) 文案
  const after = r.ok ? _countText(state.conflicts) : null;
  panel.toast = _resultToast(opts, body, before, after, r.ok);
  panel.err = null;
  panel.conflictExcId = null;
  _paintPanel(panel);

  // 4) 页面级一句话（面板可能在视口外，用户从冲突列表点进来的）
  _showExcMsg(panel.toast.kind, panel.toast.text);
}

function _setPanelBusy(panel, on) {
  if (!panel || !panel.box) return;
  const nodes = panel.box.querySelectorAll('button, select');
  for (let i = 0; i < nodes.length; i++) nodes[i].disabled = !!on;
}

/**
 * 结果文案。
 *
 * 🔴 `effective=false` 是本功能唯一必须由前端自己措辞的地方 —— 后端这时
 *    返回 200 且**没有** message，只说「写进去了但当天占用没变」
 *    （周末 / 假期 / term 外 / 单双周不匹配 / 与原掩码相同）。
 *    这里如果写「已取消」，就是彻底的假成功：页面说取消了，课表一点没动。
 */
function _resultToast(opts, body, before, after, ok) {
  const eff = opts.exemptEffective
    ? true
    : ((body && typeof body.effective === 'boolean') ? body.effective : null);

  if (eff === false) {
    return {
      kind: 'warn',
      text: '例外已记录，但系统回报这一天课表没有变化（effective=false）：'
        + '可能这一天不在这条日程的有效期内、单双周不匹配，或当天本来就没有这节课。'
        + '可在这条日程上点「撤销」取消这次操作。',
    };
  }

  let text = opts.headline + '。';
  if (eff === null) {
    text += '（接口没有回报是否生效，请核对课表。）';
  }
  if (ok && before && after) {
    text += '本组冲突 ' + before + ' → ' + after + '。';
  } else if (!ok) {
    text += '冲突列表没刷新出来（原因见周视图上方的提示），上面的数字仍是旧的。';
  } else {
    text += '冲突数取不到，请看下方冲突列表。';
  }
  return { kind: eff === null ? 'warn' : 'ok', text: text };
}

/** 页面级一句话。节点自带 class，但 kind 会变，所以这里按 kind 改 className 再显示。 */
function _showExcMsg(kind, text) {
  const okNode = $('excMsg');
  const errNode = $('excErr');
  if (!okNode || !errNode) {
    // 典型原因：浏览器还在用旧 index.html（版本号没改会这样）。至少不静默。
    console.warn('[rrr4vM] 缺少 #excMsg/#excErr 节点，提示无法显示：', text);
    return;
  }
  if (kind === 'error') {
    errNode.className = 'banner error';
    showBanner(errNode, text);
    showBanner(okNode, null);
    return;
  }
  okNode.className = 'banner ' + (kind === 'warn' ? 'warn' : 'ok');
  showBanner(okNode, text);
  showBanner(errNode, null);
}

/**
 * 轻量刷新：只重拉 /api/schedules + /api/conflicts 并重画周视图与冲突列表。
 *
 * 🔴 为什么不用现有的 loadAll()：
 *    1) loadAll 会调 renderObjectOptions，把 state.objectId **重置成当前组**
 *       （app.js 的 renderObjectOptions 没有 keep 逻辑）→ 用户正看着某个班的格子，
 *       操作完页面自己跳回组视图。
 *    2) loadAll 内部把每个接口的失败都吞掉、恒返回 undefined，
 *       调用方无法区分「刷新成功」和「刷新失败」→ 会拿旧 state 拼出假数字。
 *    3) loadAll 还会多拉一次 /api/approvals，对一次「取消一节课」是多余的。
 *
 * @returns {Promise<{ok:boolean, schedules:object|null, conflicts:object|null}>}
 */
async function refreshWeekAndConflicts() {
  const out = { ok: false, schedules: null, conflicts: null };
  const seq = ++state.statusSeq;
  const epoch = state.authEpoch;   // r4kJEe：退出/超时后晚到的响应不许画回屏上
  const params = withGroup({});
  const weekInput = $('weekSel');
  const w = weekInput ? weekInput.value.trim() : '';
  if (w) params.week = w;

  try {
    const both = await Promise.all([
      apiGet('/api/schedules', params),
      apiGet('/api/conflicts', withGroup({})),
    ]);
    out.schedules = both[0];
    out.conflicts = both[1];
  } catch (e) {
    // 🔴 401 由 handle401 清场（它抛的是裸 Error，没有 .status）→ 只能靠 token 判
    if (!state.token) return out;
    showBanner($('weekErr'),
      '刷新失败：' + ((e && e.message) ? e.message : String(e))
      + '（下面的冲突数与周视图可能还是操作前的，请点「重新加载」）');
    return out;
  }

  if (epoch !== state.authEpoch) return out;

  state.schedules = out.schedules;
  state.conflicts = out.conflicts;
  state.days = arr(out.schedules, 'days');

  if (seq === state.statusSeq) {
    const cnt = conflictCounts(out.conflicts);
    if (cnt) setStatus('ok', '本周 ', cnt.n, cnt.unit);
    else setStatus('error', '冲突数没取到（接口未返回 count / raw_count）');
  }
  // 顺序有讲究：renderConflicts 填 state.hitDates，周视图的撞车标红依赖它。
  renderConflicts(readCounts(out.conflicts));
  renderWeek(out.schedules, out.conflicts);
  syncResolveDisabled();
  out.ok = true;
  return out;
}

document.addEventListener('DOMContentLoaded', boot);
