# API 契约（v4.6）

> **用途**：冻结 6 个接口的 URL / 方法 / 请求体 / 响应体 / 错误码。
> **负责人**：独此一嘉（T1 阶段填写，约 9/27 完成）。
> **为什么必须先冻结它**：前端（T4）要用 **mock JSON** 并行开发，不必等后端 T3 完成；
> 契约不冻结，三人并行就是空谈。

## 填写规则

1. 每个接口写清：**路径、方法、请求体字段（含类型）、响应体字段（含类型）、可能的错误码**。
2. 字段一旦定稿，**变更必须更新本文件并在群里说明**，不允许口头改。
3. 拿不准的字段先写 `TBD` 并注明原因，别瞎填——前端会照着写死。

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
| 响应 429 | 连续失败 5 次后锁定 15 分钟 |

🔴 **响应中绝不回传 `password_hash`。**

待填：`default_group_id` 的取值规则（admin=根组 / teacher=其 manage 的第一个组？）

---

## 2. GET /api/groups

| 项 | 内容 |
|---|---|
| 请求 | 无参 |
| 响应 200 | `[{"group_id","name","level","member_count","parent_name"}]` |
| 说明 | **只返回我有授权（含继承）的组**；未授权的组**根本不列出**（不是灰掉） |

待填：`member_count` 是否本期实现（§7.4 裁剪顺序第 1 位，可砍）

---

## 3. GET /api/objects?group=

| 项 | 内容 |
|---|---|
| 请求 | `group`（可选） |
| 响应 200 | `[{"id","kind","name","parent_ids":[...]}]` |
| 说明 | 只返回**当前组闭包内**的对象 |

待填：`kind` 的取值域（organization/group/user/resource）

---

## 4. GET /api/schedules?group=&week=

| 项 | 内容 |
|---|---|
| 请求 | `group`（可选）、`week`（ISO 日期，缺省为本周） |
| 响应 200 | `{"objects":[...], "days":["2026-09-21",...], "grid": {"<object_id>|<date>": "<mask_hex>"}}` |
| 说明 | **前端只渲染，不做位运算**；mask 必须与 `slots.decode_mask` 一致 |

待填：grid 的 key 拼接格式；`week` 缺省取值规则

---

## 5. GET /api/conflicts?group=

| 项 | 内容 |
|---|---|
| 请求 | `group`（可选） |
| 响应 200 | `{"count": 23, "items":[{"a":{...},"b":{...},"date":"2026-09-23","slots":"0x0006","readable":[{"day":3,"period":1}]}]}` |
| 说明 | 🔴 **必须服务端聚合**：按「哪两条日程 × 哪天 × 哪几节」聚合，否则同班 40 名学生会把一处冲突刷成 40 条 |

待填：`a` / `b` 的字段结构

---

## 6. GET|POST /api/resolve?group=&mode=

| 项 | 内容 |
|---|---|
| GET（预览） | 返回前 3 个方案；经可动集合过滤后为空 → 返回**空数组 + "无可行方案"**，**绝不 500** |
| POST（应用） | 执行 `resolve_and_apply()`（迭代：找冲突→应用最优→重扫） |
| 请求体（POST） | `{"plan_index": 0}` |
| 响应 200 | `{"applied":N,"remaining_before":23,"remaining_after":0,"rounds":3,"filtered_out_count":2}` |
| 限频 | 每 token 每分钟 10 次，超限 **429** |
| `mode` | `minimal`（**默认，行为与改动前完全一致**）/ `balanced` / `optimal` |

待填：方案（plan）的字段结构（`moves` 数组每项：schedule_id / title / old / new）

---

## 7. POST /api/demands（S17 新增）

| 项 | 内容 |
|---|---|
| 请求 | `{"object_id":"...","start":"...","end":"...","hard":true}` |
| 响应 200 | `{"ok":true,"conflicts":[...],"message":"该需求与现有 3 条课表冲突，无法完全满足，已避开 2 条"}` |
| 说明 | 🔴 **绝不静默失败**：与现有日程冲突时必须明确反馈 |

待填：字段名定稿

---

## 8. POST /api/nl（S19 新增）

| 项 | 内容 |
|---|---|
| 请求 | `{"text":"把周三第1节的物理调到周三第3节","group":"..."}` |
| 响应 200 | `{"intent":"reschedule","params":{...},"source":"local\|cache\|cloud","validated":true}` |
| 说明 | 🔴 `validated` 必须为 true 才展示——**所有解析结果必须回送求解器校验** |
| 说明 | `source=cloud` 仅在本地层解析不了时出现；失败自动静默回落 `local` |

待填：`intent` 的枚举值（reschedule / trigger_m2 / add_once / ...）

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
| 请求 | `{"task_id":"...","event":"resolve_preview\|resolve_apply","ok":true}` |
| 说明 | 前端每 60 秒打一次用于计算**平均使用时长**；**不依赖 logout**（关浏览器不发） |

---

## 变更记录

| 日期 | 变更 | 谁改的 |
|---|---|---|
| 2026-09-25 | 建骨架（字段标 `待填`，由 T1 补齐） | 队长（AI 辅助） |
</content>
