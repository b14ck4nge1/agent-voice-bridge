#!/usr/bin/env bash
# Keep the server up to date with the branch it was installed from.
#
#   sudo bash deploy/auto-update.sh enable    # check every 10 minutes (systemd timer)
#   sudo bash deploy/auto-update.sh disable
#   sudo bash deploy/auto-update.sh now       # check once, right now
#   sudo bash deploy/auto-update.sh status    # timer state and the latest log lines
#
# On a new commit it pulls (fast-forward only), rebuilds, restarts, and checks
# /healthz; Caddy is restarted only when the Caddyfile changed. If the new
# version doesn't come up healthy, it goes back to the previous commit.
# Note: whoever can push to this branch can change what runs on this server.
set -euo pipefail
DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DEPLOY/.." && pwd)"
LOG=/var/log/avb-update.log
BAD=/var/lib/avb-update-bad-commit  # a commit that failed the health check: not retried
UNIT=avb-auto-update
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 2; }

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

healthy() {
  local domain; domain="$(grep -E '^AVB_DOMAIN=' "$DEPLOY/.env" | cut -d= -f2-)"
  for _ in $(seq 1 30); do
    curl -fsS --max-time 5 "https://$domain/healthz" >/dev/null 2>&1 && return 0
    sleep 5
  done
  return 1
}

rebuild() {
  (cd "$DEPLOY" && docker compose --env-file .env up -d --build bridge) >>"$LOG" 2>&1
}

update() {
  cd "$REPO"
  local owner; owner="$(stat -c %U "$REPO")"
  as_owner() { sudo -u "$owner" "$@"; }
  local branch; branch="$(as_owner git rev-parse --abbrev-ref HEAD)"
  as_owner git fetch -q origin "$branch" || { log "fetch failed"; return 1; }
  local old new; old="$(as_owner git rev-parse HEAD)"; new="$(as_owner git rev-parse "origin/$branch")"
  if [[ "$old" == "$new" ]]; then
    [[ "${1:-}" == verbose ]] && log "up to date (${old:0:7})"
    return 0
  fi
  if [[ "$new" == "$(cat "$BAD" 2>/dev/null)" ]]; then
    [[ "${1:-}" == verbose ]] && log "${new:0:7} failed before; waiting for a newer commit"
    return 0
  fi
  log "updating ${old:0:7} -> ${new:0:7}: $(as_owner git log --format=%s -1 "$new")"
  as_owner git merge -q --ff-only "origin/$branch" || { log "can't fast-forward; local changes? skipped"; return 1; }
  rebuild || true
  if as_owner git diff --quiet "$old" "$new" -- deploy/Caddyfile deploy/docker-compose.yml; then :; else
    (cd "$DEPLOY" && docker compose --env-file .env up -d && docker compose restart caddy) >>"$LOG" 2>&1
  fi
  if healthy; then
    log "updated to ${new:0:7}, healthy"
  else
    log "NOT healthy after update; going back to ${old:0:7}"
    echo "$new" > "$BAD"
    as_owner git reset -q --hard "$old"
    rebuild || true
    healthy && log "back on ${old:0:7}, healthy" || log "still not healthy: check docker compose logs"
    return 1
  fi
}

case "${1:-}" in
  enable)
    cat > /etc/systemd/system/$UNIT.service <<UNITEOF
[Unit]
Description=Agent Voice Bridge auto-update
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/bash $DEPLOY/auto-update.sh run
UNITEOF
    cat > /etc/systemd/system/$UNIT.timer <<UNITEOF
[Unit]
Description=Check for Agent Voice Bridge updates every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
UNITEOF
    systemctl daemon-reload
    systemctl enable --now $UNIT.timer >/dev/null
    echo "Auto-update on: checks every 10 minutes. Log: $LOG"
    ;;
  disable)
    systemctl disable --now $UNIT.timer 2>/dev/null || true
    echo "Auto-update off."
    ;;
  run) update ;;
  now) update verbose ;;
  status)
    systemctl list-timers $UNIT.timer --no-pager 2>/dev/null || true
    tail -n 10 "$LOG" 2>/dev/null || echo "(no updates logged yet)"
    ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
