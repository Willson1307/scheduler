#!/usr/bin/env bash
# sync_to_server.sh —— 把本地 main 代码同步到腾讯云试用服务器（TRIAL-SYNC-2）
#
# 做五件事（幂等，可重复跑）：
#   1. 备份服务器 data/（data.bak.<时间戳>），失败可回滚
#   2. tar over ssh 同步代码（src web scripts tests docs deploy + 根文档；不含 .git / data）
#   3. 重建试用库（--per-teacher，一师一组织）+ 重置 12 位教师口令为随机串
#   4. 写 systemd user unit（--today 走 Environment=SCHEDULER_TODAY）+ reload/restart
#   5. enable-linger + 复验 + 服务器侧自检 + 外网连通性（3 次 curl）
#
# 用法：
#   bash deploy/sync_to_server.sh
# 可覆盖：
#   SCHEDULER_HOST=106.53.214.131  SCHEDULER_SSH_KEY=$HOME/.ssh/wb_deploy
#   SCHEDULER_TODAY=2026-10-12     （重开试用日期，队长拍板后只改这一个值）
#
# 通道：tar over ssh（唯一实测可用）；rsync 本机未装、服务器私有库无凭据、不 git clone。
set -euo pipefail

# ── 可配置 ──────────────────────────────────────────────
HOST="${SCHEDULER_HOST:-106.53.214.131}"
SSH_USER="ubuntu"
KEY="${SCHEDULER_SSH_KEY:-$HOME/.ssh/wb_deploy}"
REMOTE_ROOT="/home/ubuntu/scheduler"
TRIAL_TODAY="${SCHEDULER_TODAY:-2026-10-12}"
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
log "重建试用库（build_trial_org.py --per-teacher）"
"${SSH[@]}" "cd '$REMOTE_ROOT' && python3 scripts/build_trial_org.py --per-teacher" \
  || { rollback_data; die "重建试用库失败（已回滚 data/）"; }

# ── 4. 重置 12 位教师口令（各自随机串，旧共享口令作废）─────
log "重置 12 位教师口令（生成 + 应用）"
"$PY_LOCAL" "$REPO/deploy/reset_trial_passwords.py" generate 2>/dev/null \
  | "${SSH[@]}" "cd '$REMOTE_ROOT' && python3 deploy/reset_trial_passwords.py apply --db data/trial.db" \
  || { rollback_data; die "重置教师口令失败（已回滚 data/）"; }

# ── 5. 写 systemd user unit（--today 走 Environment）──────
log "写 systemd unit（SCHEDULER_TODAY=$TRIAL_TODAY）"
UNIT_CONTENT="$(cat <<EOF
[Unit]
Description=Scheduler Trial Server (TRIAL-SYNC-2)
After=network.target

[Service]
WorkingDirectory=$REMOTE_ROOT
Environment=SCHEDULER_TODAY=$TRIAL_TODAY
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
