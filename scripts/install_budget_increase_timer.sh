#!/usr/bin/env bash
# Installs the systemd timer that runs the budget-increase job (metaads.budget) every day at 5:00 AM IST (30 min away
# from the auto-pause runs at 3:30, 5:30, 7:30 ... IST, so their Meta calls never overlap). Safe to re-run; run it again after changing the schedule or switching between dry
# run and live.
#   scripts/install_budget_increase_timer.sh           dry run: reports what it would raise, changes nothing
#   scripts/install_budget_increase_timer.sh --apply   live: really raises budgets
# Code, config/settings.yaml and .env changes need no reinstall: every run is a fresh process that reads them.
# Re-run this script if uv moves or is upgraded (the unit stores its absolute path).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# systemd has no login PATH, so the unit needs uv's absolute path: PATH first, else the copy Hermes installed
UV="$(command -v uv || ls -d "$HOME"/.hermes/tools/uv-*/uv 2>/dev/null | sort -V | tail -1 || true)"
UNIT=metaads-budget-increase
ARGS=""
MODE="dry run (changes nothing)"
if [ "${1:-}" = "--apply" ]; then ARGS=" --apply"; MODE="live (raises budgets for real)"; fi

[ -n "$UV" ] && [ -x "$UV" ] || { echo "uv not found (not on PATH, not in ~/.hermes/tools)." >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "Run as root (writes /etc/systemd/system)." >&2; exit 1; }

cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=metaads budget increase of well-performing Meta ad sets, $MODE
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO
ExecStart=$UV run --frozen python -m metaads.budget$ARGS
TimeoutStartSec=600
EOF

cat > "/etc/systemd/system/$UNIT.timer" <<EOF
[Unit]
Description=Run metaads budget increase daily at 5:00 AM IST

[Timer]
OnCalendar=*-*-* 05:00:00 Asia/Kolkata
AccuracySec=1s
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "$UNIT.timer"
systemctl list-timers "$UNIT.timer" --no-pager
echo
echo "Mode: $MODE"
echo "Run now:  systemctl start $UNIT.service    Logs: journalctl -u $UNIT -n 50 --no-pager"
