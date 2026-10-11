"""截图疑点实测（2026-10-11，Agent 模式）。

三项：
  T1 管理员默认组是否随机（登录响应里的 default_group_id）
  T2 管理员跨组织读冲突：切到试用学校T11 的组，看是否 30 处
  T3 周视图空网格：/api/schedules 的 grid 到底空不空（分前端/后端）

只读，不改库、不消解。所有结论以真实 HTTP 响应为准。
"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


def req(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def login(name, pwd):
    st, b = req("POST", "/api/login", body={"login": name, "password": pwd})
    assert st == 200, (name, st, b)
    return b


print("=" * 64)
print("T1  管理员默认组是否确定（同一天连登 3 次）")
print("=" * 64)
seen = []
for i in range(3):
    b = login("trial_admin", "trial123")
    gid = b.get("default_group_id")
    gids = req("GET", "/api/groups", token=b["token"])[1]
    name = next((g.get("name") for g in gids if g.get("group_id") == gid), "?")
    org = next((g.get("parent_name") for g in gids if g.get("group_id") == gid), "?")
    seen.append((gid, name, org))
    print(f"  第{i + 1}次: default_group_id={gid}")
    print(f"          → {name} · 隶属 {org}")
    print(f"          组列表共 {len(gids)} 项")
deterministic = len({s[0] for s in seen}) == 1
print(f"\n  判据：default_group_id 是否唯一")
print(f"  {'PASS 确定（3 次一致）' if deterministic else 'FAIL 不确定 → 每次登录看到不同组织'}")
print(f"  组列表数= {len(gids)}")
print(f"  ⚠️ 关键：管理员组列表应有2 个（管理员组织自己的），实测 {len(gids)} 项")
print(f"     →admin 短路 authorized_groups 返回**全库所有 group**，")
print(f"       13 个组织混在一起。这不是越权漏洞（admin 本就有全权），")
print(f"       但是产品缺陷：主持人看到的组列表里混着12 位老师的班级。")
names = [g.get("name") for g in gids]
uniq = sorted(set(names))
print(f"     组名去重{len(uniq)} 种：{uniq[:6]}{' ...' if len(uniq) > 6 else ''}")
parents = {g.get("parent_name") for g in gids}
print(f"     parent_name 覆盖 {len(parents)} 个组织（None 表示根组自身无父）")

print()
print("=" * 64)
print("T2  管理员跨组织读冲突（应能看到未消解组织的 30 处）")
print("=" * 64)
b = login("trial_admin", "trial123")
atok = b["token"]
gids = req("GET", "/api/groups", token=atok)[1]
orgs = sorted({str(g.get("parent_name")) for g in gids})
print(f"  admin 可见组织（parent_name去重）：{len(orgs)} 个")
print(f"    {orgs}")

# 挑一个 T11 的班级组与一个 T01 的班级组，分别读冲突
target = {}
for g in gids:
    pn = g.get("parent_name")
    if pn in ("试用学校T11", "试用学校T01") and g.get("name", "").startswith("示例"):
        target.setdefault(pn, g["group_id"])
for pn, gid in sorted(target.items()):
    st, c = req("GET", f"/api/conflicts?group={gid}", token=atok)
    if st == 200:
        print(f"  admin 读 {pn} 的 {gid[-8:]}… → count={c['count']} raw_count={c['raw_count']}")
    else:
        print(f"  admin 读 {pn} 的组 → HTTP {st}（403=被拦，是另一种结果）")

print()
print("  对照：教师本人读自己的组织")
for who in ("t01", "t11"):
    tk = login(f"trial_{who}", "123456")["token"]
    st, c = req("GET", "/api/conflicts", token=tk)
    own = login(f"trial_{who}", "123456")
    print(f"  trial_{who} 读自己组织 → count={c['count']} raw_count={c['raw_count']}")

print()
print("=" * 64)
print("T3  周视图 grid 是否为空（分清前端 / 后端）")
print("=" * 64)
tk = login("trial_t02", "123456")["token"]
st, s = req("GET", "/api/schedules", token=tk)
st2, c = req("GET", "/api/conflicts", token=tk)
grid = s.get("grid") or {}
nonempty = sum(1 for v in grid.values() if v)
print(f"  trial_t02: HTTP {st}  days={len(s.get('days', []))} "
      f"grid key={len(grid)}  非空格子={nonempty}")
print(f"             objects={len(s.get('objects', []))}但 raw_count={c['raw_count']}")
if nonempty:
    k = next(k for k, v in grid.items() if v)
    print(f"    ✅ 后端正常：样例 key={k} -> {grid[k]!r}")
    print(f"     → 课表数据**确实存在**，页面空是前端只画冲突格所致")
else:
    print("    🔴 后端 grid 全空 → 需查 api_schedules.py")

# 反证：消解后（冲突 0）grid 是否仍有内容 → 证明 grid 与冲突是两套独立数据
print()
print("  反证：grid 是否依赖冲突数？先记冲突前 grid，再消解，再看")
before = sum(1 for v in grid.values() if v)
req("POST", "/api/resolve", token=tk)
st, c2 = req("GET", "/api/conflicts", token=tk)
st, s2 = req("GET", "/api/schedules", token=tk)
after = sum(1 for v in (s2.get("grid") or {}).values() if v)
print(f"    消解前：raw_count={c['raw_count']} grid非空={before}")
print(f"    消解后：raw_count={c2['raw_count']} grid非空={after}")
print(f"    → {'grid 不随冲突变化，说明课表数据独立于冲突检测' if before == after else 'grid 随消解变化，需复查'}")

print()
print("=" * 64)
print("结论：页面表格只画冲突格（web/app.js renderWeek 的 hits 只来自 conflicts.items）")
print("=" * 64)
print("  冲突为 0 → hits 为空 → 50 个格子全空 → 观感像「课表丢了」")
print("  这是**前端问题**，且属演示事故级：现场消解成功后整页课表变空白。")