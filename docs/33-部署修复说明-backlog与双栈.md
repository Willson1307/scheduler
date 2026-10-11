# fix(deploy): backlog 放大到 128 + 双栈监听（10/12 试用部署实测发现）

> 卡号：无（部署线突发修复，由 WorkBuddy 发现并提交）
> 发现时间：2026-10-11 22:30–23:00
> 部署背景：10/12 教师试用，腾讯云轻量`106.53.214.131`
> 关联文档：`docs/32-云服务器部署记录-1012试用.md`

## 一句话

两处都是**「所有自检全绿，但真实用户打不开」**的静默失效 —— 不是逻辑错，是网络层默认值太小。

## 问题1：`request_queue_size` 默认只有 5

### 现象
防火墙规则生效后，外部访问**第1 次成功（HTTP=200），紧接着 15 连测全部 `Connection refused`**。

### 真凶
`SchedulerHTTPServer` 未设 `request_queue_size`，`socketserver.TCPServer` 类**默认值就是 5**。
并发/探测连接把accept 队列塞满 → 后续请求直接被拒。

### 为什么算静默失效（本项目已有 7 处同类，这是第 8 处）

| 检查 | 结果 |
|---|---|
| 服务端日志 | **完全看不到被拒的请求** |
| `/api/health` |200 ✅ |
| `doctor.py` | 15/15 ✅ |
| 契约测试 | 17/17 ✅ |
| 单元测试 | 全绿 ✅ |

全绿，但 12 位老师里只要有人同时开页面就可能打不开。

### 修法

```python
class SchedulerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128
```

实测 `ss -tln` 的 `Send-Q` 由 **5 → 128**。

## 问题 2：只监听 IPv4，纯 IPv6 用户打不开

### 现象
云主机同时持有 IPv4 与 IPv6。原启动参数 `--host 0.0.0.0` **只绑 IPv4**：

```bash
$ curl -6 http://[::1]:8000/api/health
HTTP=000        # ← IPv6 回环都测不通
```

### 后果
**纯 IPv6 网络的用户（部分校园网 / 移动网络）会完全打不开**。
这类缺陷最阴险 —— 开发机是 IPv4 出口，本地测不出来，只能等真实用户踩。

### 修法

`http_api.py` 支持指定地址族：

```python
def __init__(self, address, cfg, family: int = socket.AF_INET):
    self.cfg = cfg
    self.address_family = family
    super().__init__(address, RequestHandler)
```

`run_server.py` 按 host 推断：

```python
def _bind_family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET
```

启动参数改 `--host ::`。Linux `bindv6only=0`（实测）时，同一 AF_INET6 套接字同时接受 IPv4 映射地址。

### 平台差异（实测，勿在Windows 上验证）

| 平台 | `net.ipv6.bindv6only` | `--host ::` 结果 |
|---|---|---|
| Windows | `1` | IPv6 通，**IPv4 反而挂** |
| Linux 服务器 | `0` | 双栈均通 ✅ |

**双栈必须在目标服务器上验证**，在 Windows 本地测会得出反向结论。

## 实测证据（Linux 服务器，非本地推断）

```
监听           LISTEN 0  128  *:8000  *:*
IPv4 公网自测   200 200 200 200 200
IPv6 公网自测   200 200 200 200 200
首页字节数      15338
端到端验收      17 项全绿（含组织隔离、T01 消解 30→0、T02 不受影响）
全量单测        143 tests OK / 0 FAIL
代码一致性      diff 服务器版 → 完全一致（杜绝第二份实现）
```

## 变更清单

| 文件 | 改动 |
|---|---|
| `src/server/http_api.py` | +`import socket`；`+request_queue_size = 128`；`__init__` 加 `family` 参数 |
| `scripts/run_server.py` | +`import socket`；`+_bind_family()`；传 `family=` |

## 🔴 遗留风险（合入后仍需处理）

1. **IPv6 入站规则未单独确认**：腾讯云控制台的 IPv6 规则可能与 IPv4 分开管理。
   若纯 IPv6 用户打不开 → 检查控制台 IPv6 侧规则，或让其改用 IPv4 地址。
2. **明文端口、无HTTPS**：8000 对公网全开。试用可接受（数据全为虚构），
   **不可用于生产**。答辩口径：内网定位的校园工具。
3. **试用结束需回收**：试用库口令硬编码在 `build_trial_org.py`，虽已换成随机强口令，
   但仓库仍留明文结构，应在赛后改为环境变量注入。

## AI 生成部分说明（AGENTS 要求）

两个修复均由 AI 定位并实现。定位依据是**实测现象与源码默认值比对**，不是猜测：

- backlog：读`http.server` / `socketserver` 源码确认默认值为 5，再由 `ss -tln` 的
  `Send-Q=5` 反向印证
- 双栈：读 `SocketServer.__init__` 确认 `address_family` 可由子类覆写，
  再在服务器上用 `curl -6` 实测验证

判断难点在于「两者都不报错」，AI 的作用是把「自检全绿」与「用户打不开」这对矛盾
摆到台面上，而不是继续加深测试覆盖率 —— 加测试发现不了网络层默认值问题。
