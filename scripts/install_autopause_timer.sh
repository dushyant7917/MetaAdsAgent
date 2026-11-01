#!/usr/bin/env bash
# Installs the systemd timer that runs the auto-pause job (metaads.autopause) every 2 hours on even hours (server
# time, UTC). Safe to re-run; run it again after changing the schedule or switching between dry run and live.
#   scripts/install_autopause_timer.sh           dry run: reports what it would pause, pauses nothing
#   scripts/install_autopause_timer.sh --apply   live: really pauses losing ad sets
# Code, config/settings.yaml and .env changes need no reinstall: every run is a fresh process that reads them.
# Re-run this script if uv moves or is upgraded (the unit stores its absolute path).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# systemd has no login PATH, so the unit needs uv's absolute path: PATH first, else the copy Hermes installed
UV="$(command -v uv || ls -d "$HOME"/.hermes/tools/uv-*/uv 2>/dev/null | sort -V | tail -1 || true)"
UNIT=metaads-autopause
ARGS=""
MODE="dry run (pauses nothing)"
if [ "${1:-}" = "--apply" ]; then ARGS=" --apply"; MODE="live (pauses for real)"; fi

[ -n "$UV" ] && [ -x "$UV" ] || { echo "uv not found (not on PATH, not in ~/.hermes/tools)." >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "Run as root (writes /etc/systemd/system)." >&2; exit 1; }

cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=metaads auto-pause of losing Meta ad sets, $MODE
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO
ExecStart=$UV run --frozen python -m metaads.autopause$ARGS
TimeoutStartSec=600
EOF

cat > "/etc/systemd/system/$UNIT.timer" <<EOF
[Unit]
Description=Run metaads auto-pause every 2 hours

[Timer]
OnCalendar=*-*-* 00/2:00:00
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
