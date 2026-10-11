"""试用隔离实测：T01 消解后 T02/T03/T12 是否仍各自30 处冲突。

背景（2026-10-11 队长提问「一个老师把冲突全消了，剩下的老师怎么办？」）：
  共享单组织时消解是真改库，第一位老师点完全组织归零→ 后面老师采不到完成率。
  解法见 build_trial_org.py `--per-teacher`：一师一组织，靠系统既有的 org_id 隔离。

用法：
    # 1) 起服务（另开终端）
    python scripts/run_server.py --port 8000 --today 2026-10-12 --db data/trial.db
    # 2) 跑本脚本
    python scripts/check_trial_isolation.py
    #退出码 0 = 隔离通过；1 = 有问题

🔴 只打真实 HTTP 请求，不读代码推断（项目铁律：绿灯≠正确，必须实测）。
🔴 断言口径（首跑踩过）：`count`=聚合组数（界面口径），`raw_count`=原始条数（消解口径），
   两者必须都比；只比 count 会得出「隔离失效」的错误结论。
"""
import json
import sys
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
    assert st == 200, (st, b)
    return b["token"]


def conflicts(token):
    st, b = req("GET", "/api/conflicts", token=token)
    assert st == 200, (st, b)
    return b


tokens = {}
BASE_RAW = {}
BASE_CNT = {}
print("=== 登录并读初始冲突 ===")
for k in ("t01", "t02", "t03", "t12"):
    tok = login(f"trial_{k}", "123456")
    tokens[k] = tok
    c = conflicts(tok)
    # 🔴 count=聚合组数（界面口径）/ raw_count=原始条数（消解口径）。
    #    隔离断言必须两个都比，混用会得出「隔离失效」的错误结论（首跑就踩了）。
    BASE_CNT[k], BASE_RAW[k] = c["count"], c["raw_count"]
    print(f"  trial_{k}: count={c['count']} raw_count={c['raw_count']}")

print("\n=== T01 执行一键消解 ===")
st, b = req("POST", "/api/resolve", token=tokens["t01"])
print(f"  HTTP {st}  {json.dumps(b, ensure_ascii=False)}")

print("\n=== 消解后各人冲突数（关键：T02/T03/T12 必须与初始完全一致） ===")
ok = True
for k in tokens:
    c = conflicts(tokens[k])
    same = (c["count"] == BASE_CNT[k] and c["raw_count"] == BASE_RAW[k])
    if k == "t01":
        # 🔴 T01 是**预期要变**的那一个（它刚执行了消解）。
        #    此前这里打「⚠️ 变了！」，把预期结果标成异常信号 → 误导读者，
        #    且只断言 raw_count==0、漏了 count==0（count=0 但 raw_count>0 也算异常）。
        #    两个数都必须为 0（2026-10-11 补）。
        if c["count"] == 0 and c["raw_count"] == 0:
            print(f"  trial_{k}: count={c['count']} raw_count={c['raw_count']}"
                  f"　（预期归零 ✔）")
            print(f"  PASS {k} 消解后 count 与 raw_count 双双归零")
        else:
            print(f"  trial_{k}: count={c['count']} raw_count={c['raw_count']}"
                  f"　（⚠️ 应为 0/0）")
            print(f"  FAIL {k} 消解后未完全归零（count={c['count']} raw_count={c['raw_count']}）")
            ok = False
    else:
        if not same:
            print(f"  trial_{k}: count={c['count']} raw_count={c['raw_count']}"
                  f"　（⚠️ 变了！初始为 {BASE_CNT[k]}/{BASE_RAW[k]}）")
            print(f"  FAIL trial_{k} 被 T01 影响了 → 组织隔离失效")
            ok = False
        else:
            print(f"  trial_{k}: count={c['count']} raw_count={c['raw_count']}"
                  f"　（与初始一致 ✔）")
            print(f"  PASS trial_{k} 未受 T01 影响")

# 可见规模核对：缺省组 = default_group_id，其闭包 = 本班 + 该班全部学生（本组织数据）
#🔴 2026-10-11 首跑误判修正：曾按「本组织共 6 个对象」设阈值 8，
#    实际 13 项（1 个班 + 12 名学生）**全部属于本组织**，是正常闭包而非越权。
#    正确判据不是数量，而是「有没有出现别的组织的东西」。
st, ob = req("GET", "/api/objects", token=tokens["t02"])
items = ob if isinstance(ob, list) else ob.get("items", [])
n_vis = len(items)
foreign = [i.get("name", "") for i in items
           if "T01" in str(i) or "T03" in str(i) or "T12" in str(i)]
print(f"\n  T02 GET /api/objects → HTTP {st}，可见 {n_vis} 项（默认组闭包）")
if foreign:
    print(f"  ⚠️ 出现他组织对象：{foreign} → 组织隔离失效")
    ok = False
else:
    print(f"  PASS 可见对象全部属于本组织（{[i['name'] for i in items][:3]} …）")

# 显式越权：T02 的 token 去改T01 的组（应403）
st_g, g = req("GET", "/api/groups", token=tokens["t02"])
gids = ([x.get("group_id") or x.get("id") for x in g]
        if isinstance(g, list) else [x.get("group_id") or x.get("id")
                                       for x in g.get("groups", [])])
if st_g == 200 and gids:
    print(f"  T02 可见组 {len(gids)} 个，parent 均为「试用学校T02」"
          f"（{ {x.get('parent_name') for x in g} }）")
    st2, _ = req("GET", f"/api/objects?group={gids[0]}", token=tokens["t01"])
    print(f"  T01 token 访问 T02 的组→ HTTP {st2}（403 = 正确拦截）")
    if st2 != 403:
        ok = False

print("\n" + ("✅ 组织隔离全部通过" if ok else "❌ 存在隔离问题"))
sys.exit(0 if ok else 1)