#!/usr/bin/env bash
# sync_to_server.sh —— 把本地 main 代码同步到腾讯云试用服务器（TRIAL-SYNC-2）
#
# 做五件事（幂等，可重复跑）：
#   1. 备份服务器 data/（data.bak.<时间戳>），失败可回滚
#   2. tar over ssh 同步代码（src web scripts tests docs deploy + 根文档；不含 .git / data）
#   3. 重建试用库（--per-teacher，一师一组织）+ 重置 13 个账号口令为随机串
#      🔴 重建前查 event_log：已有真实试用数据则拒绝（须 FORCE_REBUILD=1）
#      🔴 admin 口令经 stdin 灌入远端建库命令（服务器无口令表）
#   4. 写 systemd user unit（--today 走 Environment=SCHEDULER_TODAY）+ reload/restart
#   5. enable-linger + 复验 + 服务器侧自检 + 外网连通性（3 次 curl）
#
# 用法：
#   bash deploy/sync_to_server.sh
# 可覆盖：
#   SCHEDULER_HOST=106.53.214.131  SCHEDULER_SSH_KEY=$HOME/.ssh/wb_deploy
#   SCHEDULER_TODAY=YYYY-MM-DD     （🔴 缺省=运行当日，写进 systemd unit 的
#                                   SCHEDULER_TODAY，并把试用学期起点锚定到
#                                   该日所在周周一；只有要复现旧日期时才显式传。
#                                   旧注释教人写死 2026-10-12 —— 谁忘了传，老师
#                                   看到的就是过期的课表，且没有任何报错）
#
# 通道：tar over ssh（唯一实测可用）；rsync 本机未装、服务器私有库无凭据、不 git clone。
set -euo pipefail

# ── 可配置 ──────────────────────────────────────────────
HOST="${SCHEDULER_HOST:-106.53.214.131}"
SSH_USER="ubuntu"
KEY="${SCHEDULER_SSH_KEY:-$HOME/.ssh/wb_deploy}"
REMOTE_ROOT="/home/ubuntu/scheduler"
TRIAL_TODAY="${SCHEDULER_TODAY:-$(date +%F)}"
UNIT_NAME="scheduler-trial"
TRIAL_PORT="8000"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SSH=(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -i "$KEY" "$SSH_USER@$HOST")

PY_LOCAL="python3"; command -v python3 >/dev/null 2>&1 || PY_LOCAL="python"

log()  { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

BACKUP_DIR=""

rollback_data() {
  [ -n "$BACKUP_DIR" ] || return 0
  warn "回滚 data/（$BACKUP_DIR → data）"
  "${SSH[@]}" "cd '$REMOTE_ROOT' && rm -rf data && mv '$BACKUP_DIR' data" \
    || warn "回滚失败，请手工：cd $REMOTE_ROOT && rm -rf data && mv $BACKUP_DIR data"
}

# ── 0. 预检 ─────────────────────────────────────────────
log "预检：SSH 通道"
"${SSH[@]}" "echo SSH_OK_\$(whoami)" | grep -q "SSH_OK_$SSH_USER" \
  || die "SSH 不通（检查 KEY=$KEY / 用户 $SSH_USER / 主机 $HOST）"

cd "$REPO"
BRANCH="$(git branch --show-current 2>/dev/null || echo '(no-git)')"
log "预检：本地分支 = $BRANCH"
[ "$BRANCH" = "main" ] || warn "当前分支非 main，同步的是本地工作区内容（如需 main，先 git checkout main）"
git diff --quiet && git diff --cached --quiet || warn "工作区有未提交改动，仍会同步当前文件内容"

# ── 1. 备份服务器 data/ ──────────────────────────────────
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="data.bak.$TS"
log "备份服务器 data/ → $BACKUP_DIR"
"${SSH[@]}" "cd '$REMOTE_ROOT' && cp -a data '$BACKUP_DIR'" || die "备份 data/ 失败，中止"

# ── 2. 同步代码（不含 .git / data）────────────────────────
log "同步代码（tar over ssh）"
tar -czf - -C "$REPO" src web scripts tests docs deploy AGENTS.md README.md requirements.txt \
  | "${SSH[@]}" "cd '$REMOTE_ROOT' && tar -xzf -" \
  || { rollback_data; die "代码同步失败（已回滚 data/）"; }

# ── 3. 重建试用库（--per-teacher，一师一组织）──────────────
# 🔴 2026-10-25 TRIAL-AUTH-KIT-2 两条护栏：
#   ① admin 口令经 stdin 喂进远端建库命令 —— 服务器上没有 data/trial-passwords.txt
#      （data/ 不进 tar），写死「读表」会让本步骤必然 FAIL。
#   ② trial.db 的 event_log 是章程第八条(六)「真实用户体验数据」的唯一证据；
#      已有数据时默认拒绝重建，必须先导出或显式 FORCE_REBUILD=1。
ADMIN_PW="$(awk '$1 == "trial_admin" { print $2; exit }' "$REPO/data/trial-passwords.txt" 2>/dev/null || true)"
[ -n "$ADMIN_PW" ] || die "本机口令表里没有 trial_admin 口令（先跑 deploy/reset_trial_passwords.py generate）"

EXISTING_EVENTS="$("${SSH[@]}" "cd '$REMOTE_ROOT' && python3 -c \"import sqlite3, os; p = 'data/trial.db'; print(sqlite3.connect(p).execute('select count(*) from event_log').fetchone()[0] if os.path.exists(p) else 0)\" 2>/dev/null" || echo 0)"
EXISTING_EVENTS="$(printf '%s' "$EXISTING_EVENTS" | tr -dc '0-9')"
EXISTING_EVENTS="${EXISTING_EVENTS:-0}"
log "服务器 trial.db 现有真实埋点：$EXISTING_EVENTS 行"

if [ "$EXISTING_EVENTS" -gt 0 ] && [ "${FORCE_REBUILD:-0}" != "1" ]; then
  die "已有 $EXISTING_EVENTS 行真实试用数据（资格项证据），重建会清空 → 已中止。
     先导出：ssh -i $KEY $SSH_USER@$HOST 'cd $REMOTE_ROOT && python3 scripts/telemetry_report.py --db data/trial.db'
     确认要重建再加 FORCE_REBUILD=1 重跑本脚本。"
fi

# 🔴 2026-10-26（TRIAL-REANCHOR）：学期起点锚定 TRIAL_TODAY 所在周周一。
#   旧版重建不带 --term-start → 永远用 build 脚本里 2026-10-12 的默认学期 →
#   试用日一到、一次性「示例活动」过期、冲突 30→3 且接口不报错（本卡要修的病灶）。
TRIAL_TERM_START="$(date -d "$TRIAL_TODAY -$(( $(date -d "$TRIAL_TODAY" +%u) - 1 )) days" +%F)" || true
[ -n "$TRIAL_TERM_START" ] || die "无法由 TRIAL_TODAY=$TRIAL_TODAY 计算学期起点（需 YYYY-MM-DD）"
log "重建试用库（term-start=$TRIAL_TERM_START ← TRIAL_TODAY=$TRIAL_TODAY 所在周周一，admin 口令经 stdin）"
printf '%s\n' "$ADMIN_PW" \
  | "${SSH[@]}" "cd '$REMOTE_ROOT' && python3 scripts/build_trial_org.py --per-teacher --term-start '$TRIAL_TERM_START' --admin-password-stdin" \
  || { rollback_data; die "重建试用库失败（已回滚 data/）"; }

# ── 4. 重置 13 个试用账号口令（admin + 12 教师，各自随机串，旧共享口令作废）
log "重置试用账号口令（admin + 12 教师）"
"$PY_LOCAL" "$REPO/deploy/reset_trial_passwords.py" generate 2>/dev/null \
  | "${SSH[@]}" "cd '$REMOTE_ROOT' && python3 deploy/reset_trial_passwords.py apply --db data/trial.db" \
  || { rollback_data; die "重置教师口令失败（已回滚 data/）"; }

# ── 5. 写 systemd user unit（--today 走 Environment）──────
# 🔴 写 unit 之前先把最终 TRIAL_TODAY 打出来——这是老师将要看到的「今天」，
#    锚错了当场可见，不等试用当天才发现（TRIAL-REANCHOR）。
log "最终 TRIAL_TODAY=$TRIAL_TODAY（即将写入 unit 的 SCHEDULER_TODAY；学期起点=$TRIAL_TERM_START）"

# 🔴 2026-11-11：把「服务器上有没有 .env.local」打出来。
#    没有 = 问答走手册原文（retrieval），属**正常**状态，不是故障；
#    有 = 问答会走 LLM 润色（mode="llm"）。两句话写清楚，避免把
#    「没配」误判成「配了但坏了」，也避免把「没配」当成缺陷去排查。
ENV_STATE="$("${SSH[@]}" "[ -f '$REMOTE_ROOT/.env.local' ] && echo present || echo absent" 2>/dev/null || echo unknown)"
log "服务器 .env.local: $ENV_STATE（absent = 问答只答手册原文，正常；present = 会走 LLM 润色）"
UNIT_CONTENT="$(cat <<EOF
[Unit]
Description=Scheduler Trial Server (TRIAL-SYNC-2)
After=network.target

[Service]
WorkingDirectory=$REMOTE_ROOT
Environment=SCHEDULER_TODAY=$TRIAL_TODAY
# 🔴 2026-11-11（feat/llm-config）：LLM 问答层的三件套（BASE/KEY/MODEL）只从
#    os.environ 读（b_llm_client.py）。没有这一行，写在 .env.local 里的配置
#    进程根本看不到 → 问答永远停在 mode="retrieval" 且**不报任何错**
#    （典型静默失效：功能在、只是变笨）。
#    前面的 `-` 是「文件不存在也不报错」：没配 LLM 的机器必须仍能起服，
#    不配 = 不启用（AGENTS §4.1 跨人契约）。
EnvironmentFile=-$REMOTE_ROOT/.env.local
ExecStart=/usr/bin/python3 $REMOTE_ROOT/scripts/run_server.py --host :: --port $TRIAL_PORT --db $REMOTE_ROOT/data/trial.db
Restart=always
RestartSec=5
StandardOutput=append:/home/ubuntu/server.log
StandardError=append:/home/ubuntu/server.log

[Install]
WantedBy=default.target
EOF
)"
printf '%s\n' "$UNIT_CONTENT" \
  | "${SSH[@]}" "mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/$UNIT_NAME.service" \
  || die "写 unit 失败"

# ── 6. reload + restart（SSH 进去就是 ubuntu，走 user bus）──
log "daemon-reload + restart"
"${SSH[@]}" "systemctl --user daemon-reload && systemctl --user restart $UNIT_NAME" \
  || die "restart 失败（若 Failed to connect to bus，检查是否非 ubuntu 身份）"

# ── 7. enable-linger + 复验 ──────────────────────────────
log "enable-linger + 复验"
"${SSH[@]}" "sudo loginctl enable-linger $SSH_USER 2>/dev/null || true; loginctl show-user $SSH_USER | grep -i linger" \
  || warn "linger 复验失败"

# ── 8. 等 health + 服务器侧自检 ───────────────────────────
log "等待服务就绪"
for i in $(seq 1 15); do
  code="$("${SSH[@]}" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$TRIAL_PORT/api/health" 2>/dev/null || echo 000)"
  [ "$code" = "200" ] && break
  sleep 2
done
"${SSH[@]}" "curl -s http://127.0.0.1:$TRIAL_PORT/api/health" || true
echo

log "服务器侧自检：trial_smoke.py"
"${SSH[@]}" "cd '$REMOTE_ROOT' && python3 deploy/trial_smoke.py" || warn "trial_smoke 有红项（见上）"

log "服务器侧自检：doctor.py"
"${SSH[@]}" "cd '$REMOTE_ROOT' && python3 scripts/doctor.py" || warn "doctor 有红项"

log "服务器侧自检：smoke.py --offline"
"${SSH[@]}" "cd '$REMOTE_ROOT' && python3 scripts/smoke.py --offline" || warn "smoke 有红项"

# ── 9. 外网连通性（本机 → 公网 IP，3 次）──────────────────
log "外网连通性（本机 → $HOST:$TRIAL_PORT，3 次）"
ok=0
for i in 1 2 3; do
  code="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "http://$HOST:$TRIAL_PORT/api/health" 2>/dev/null || echo 000)"
  [ "$code" = "200" ] && ok=$((ok + 1))
  log "第 $i 次 http_code=$code"
done
[ "$ok" = "3" ] || warn "外网 3 次非全 200（$ok/3），请检查防火墙/端口，勿只看 systemctl status"

log "完成。口令表位置（本地，不入库）：$REPO/data/trial-passwords.txt"
log "服务器备份：$REMOTE_ROOT/$BACKUP_DIR（确认无误后可手工删除）"
