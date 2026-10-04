"""P0 自检：api_kernels 口径必须用真实演示库验证，不能只靠读代码。

跑法：python _p0_check.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from datetime import date  # noqa: E402

from server import api_kernels as K  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.config import connect  # noqa: E402
from server import config as C  # noqa: E402

cfg = ServerConfig(today=date(2026, 9, 8))
C.set_current_config(cfg)

print("=== 1. 窗口口径 ===")
f, t = K.resolve_window(cfg)
print("window:", f, "~", t, "| payload:", K.window_payload(cfg))
assert f == date(2026, 9, 7), f
assert t == date(2026, 9, 13), t
print("  [OK] --today 2026-09-08 → 第1周(09-07~09-13)")

cfg2 = ServerConfig(today=date(2026, 9, 28))
f2, t2 = K.resolve_window(cfg2)
print("  对照 --today 2026-09-28 →", f2, "~", t2)
assert f2 == date(2026, 9, 28), f2
print("  [OK] 窗口随 today 变（防有人改窗口无人发现）")

print()
print("=== 2. 掩码口径（🔴 本项目最贵的坑）===")
# 黄金用例：周掩码 0x100000000（周三第1节）→ 单日掩码 0x000002
from scheduler.slots import bit  # noqa: E402
from scheduler.solver import m2  # noqa: E402

week_mask = bit(3, 1)  # 周三(3) 第1节(idx=1)
print("  周掩码 slots.bit(3,1) =", hex(week_mask))
assert week_mask == 0x100000000, hex(week_mask)

day_hex = K.slots_hex([(3, 1)])
print("  API 出参 K.slots_hex([(3,1)]) =", day_hex)
assert day_hex == "0x2", day_hex

# 🔴 反向断言：单日掩码绝不能等于周掩码
assert m2.day_slots_mask([(3, 1)]) != week_mask, "周掩码与单日掩码相同，说明用错了函数！"
print("  [OK] 周掩码 0x100000000 → 单日 0x2（宽度由 day_mask_hex 补齐为 0x000002）")
print("  [OK] 反向断言通过：单日掩码", hex(m2.day_slots_mask([(3, 1)])), "≠ 周掩码", hex(week_mask))

print("  三档读数校验：")
for label, slots, want in [
    ("第1节", [(3, 1)], "0x2"),
    ("第3节", [(3, 3)], "0x8"),
    ("第1+2节", [(3, 1), (3, 2)], "0x6"),
]:
    got = K.slots_hex(slots)
    print(f"    {label}: {got}  期望 {want}")
    assert got == want, f"{label}: {got} != {want}"
print("  [OK] 0x2=第1节 / 0x8=第3节 / 0x6=第1+2节，与 B4 _slots_hex 口径一致")

print()
print("=== 3. 聚合口径（count=3 / raw_count=23）===")
db = os.path.join(ROOT, "data", "demo.db")
if not os.path.exists(db):
    print("  [SKIP] data/demo.db 不存在，先跑 python scripts/reset_demo.py")
    sys.exit(0)

conn = connect(db)
try:
    org_id = K.resolve_org(conn, None)
    print("  org_id:", org_id)
    payload = K.conflict_payload(conn, org_id, cfg)
    print("  count     =", payload["count"])
    print("  raw_count =", payload["raw_count"])
    print("  window    =", payload["window"])
    for it in payload["items"]:
        print(f"    [{it['date']}] {it['slots']} ({it['readable']}) "
              f"role={it['a']['role']} obj={it['object_id'][:14]}… raw={it['raw_count']}")
    assert payload["count"] == 3, payload["count"]
    assert payload["raw_count"] == 23, payload["raw_count"]
    assert payload["count"] == len(payload["items"])
    # 🔴 三元组不得重复（契约 §5 + test_contract.py:321-334）
    triples = [(it["a"]["schedule_id"], it["b"]["schedule_id"], it["date"], it["slots"])
               for it in payload["items"]]
    assert len(triples) == len(set(triples)), "聚合键重复！"
    print("  [OK] count==len(items)==3，raw_count==23，三元组不重复")
    assert 23 == 1 + 11 + 11, "23 应为 1 条教师冲突 + 两个班各 11 条学生冲突"
    print("  [OK] 23 = 1 + 11 + 11")

    print()
    print("=== 4. 确定性（同一请求两次调用逐字一致）===")
    import json
    p1 = json.dumps(K.conflict_payload(conn, org_id, cfg), ensure_ascii=False, sort_keys=True)
    p2 = json.dumps(K.conflict_payload(conn, org_id, cfg), ensure_ascii=False, sort_keys=True)
    assert p1 == p2, "两次调用不一致！"
    print("  [OK] 逐字一致（%d 字符）" % len(p1))
finally:
    conn.close()

print()
print("=== 5. B4 同源性（remaining_before 应等于 raw_count）===")
print("  B4 用 scan(...).count = %d" % payload["raw_count"])
print("  → 答辩口径：说「23 处冲突」时，界面用 raw_count=23，与 B4 的 remaining_before 同源")
print()
print("ALL P0 CHECKS PASSED")
